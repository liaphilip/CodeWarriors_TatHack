from unittest.mock import MagicMock, patch

from google.genai import errors, types
import pytest

from app.agent import ProviderError, run_agent
from app.config import Settings
from app.provider import GeminiProvider


def response(name=None, arguments=None):
    part = (types.Part(function_call=types.FunctionCall(name=name, args=arguments, id="call-1"),
                       thought_signature=b"preserve-this-signature")
            if name else types.Part.from_text(text="Done"))
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[part]))])


def test_official_sdk_adapter_manual_round_trip():
    client = MagicMock()
    replies = [response("search_documents", {"query": "quarterly"}),
               response("read_document", {"document_id": "quarterly-report-q3"}),
               response("propose_send_document", {"document_id": "quarterly-report-q3", "recipient": "Alice"}),
               response()]
    client.models.generate_content.side_effect = replies
    with patch("app.provider.genai.Client", return_value=client):
        provider = GeminiProvider(Settings("test-key", "test-model"))
        result = run_agent("Find quarterly report for Alice", provider)
        assert len(result["proposals"]) == 1
        assert provider.history[1] is replies[0].candidates[0].content
        assert provider.history[1].parts[0].thought_signature == b"preserve-this-signature"
        function_response = provider.history[2].parts[0].function_response
        assert function_response.id == "call-1"
        assert function_response.response["documents"][0]["document_id"] == "quarterly-report-q3"
        assert provider.config.automatic_function_calling.disable is True
        assert len(provider.config.tools[0].function_declarations) == 3
        assert client.models.generate_content.call_count == 4
        assert client.models.generate_content.call_args.kwargs["model"] == "test-model"
        provider.close()
        client.close.assert_called_once()


@pytest.mark.parametrize("failure", [RuntimeError("secret-key"),
                                      errors.ClientError(429, {"error": {"message": "secret-key"}})])
def test_sdk_errors_are_sanitized(failure):
    with patch("app.provider.genai.Client") as factory:
        factory.return_value.models.generate_content.side_effect = failure
        provider = GeminiProvider(Settings("test-key", "test-model"))
        with pytest.raises(ProviderError) as caught:
            provider.next_turn()
        assert "secret-key" not in str(caught.value)


def test_empty_sdk_response():
    with patch("app.provider.genai.Client") as factory:
        factory.return_value.models.generate_content.return_value = types.GenerateContentResponse()
        with pytest.raises(ProviderError):
            GeminiProvider(Settings("test-key", "test-model")).next_turn()

@pytest.mark.parametrize('failure', [errors.ServerError(504, {'error': {'message': 'private'}}),
                                     errors.ServerError(503, {'error': {'message': 'private'}})])
def test_transient_failure_after_search_retries_same_turn_without_replaying_tools(failure):
    client = MagicMock()
    replies = [response('search_documents', {'query': 'quarterly'}), failure,
               response('read_document', {'document_id': 'quarterly-report-q3'}),
               response('propose_send_document', {'document_id': 'quarterly-report-q3', 'recipient': 'Alice'}), response()]
    client.models.generate_content.side_effect = replies
    with patch('app.provider.genai.Client', return_value=client), patch('app.provider.time.sleep') as sleep:
        provider = GeminiProvider(Settings('test', 'test'))
        result = run_agent('Find quarterly report for Alice', provider)
    assert result['status'] == 'completed' and len(result['proposals']) == 1
    assert [a['tool'] for a in result['activity']] == ['search_documents', 'read_document', 'propose_send_document']
    assert result['sent'] is False
    assert client.models.generate_content.call_count == 5
    sleep.assert_called_once()
    assert len(provider.history) == 7


def test_timeout_retries_exhausted_are_bounded_and_safe(caplog):
    import httpx
    for failure in [errors.ServerError(504, {'error': {'message': 'secret-key'}}), httpx.ReadTimeout('secret-key')]:
        with patch('app.provider.genai.Client') as factory, patch('app.provider.time.sleep') as sleep:
            factory.return_value.models.generate_content.side_effect = failure
            provider = GeminiProvider(Settings('test', 'test'))
            result = run_agent('Find quarterly report for Alice', provider)
            assert factory.return_value.models.generate_content.call_count == 3
            assert sleep.call_count == 2
            assert result['status'] == 'error' and 'timed out' in result['message']
            assert result['proposals'] == [] and not result['sent']
            assert 'secret-key' not in result['message'] and 'secret-key' not in caplog.text
            assert len(provider.history) == 1


@pytest.mark.parametrize('code', [400, 401, 403, 404, 429])
def test_permanent_and_quota_errors_not_retried(code):
    with patch('app.provider.genai.Client') as factory, patch('app.provider.time.sleep') as sleep:
        factory.return_value.models.generate_content.side_effect = errors.ClientError(code, {'error': {'message': 'private'}})
        with pytest.raises(ProviderError):
            GeminiProvider(Settings('test', 'test')).next_turn()
        assert factory.return_value.models.generate_content.call_count == 1
        sleep.assert_not_called()
