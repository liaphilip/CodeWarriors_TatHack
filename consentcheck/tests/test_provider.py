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
