import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch
from google.genai import types

from app.agent import Turn
from app.config import Settings
from app.main import create_app
from app.provider import GeminiProvider
from test_agent import FakeProvider, successful_turns


@pytest.fixture
def chat(tmp_path):
    settings = Settings("test-key", "test-model", tmp_path / "chat.db")
    providers = []

    def factory(_):
        provider = FakeProvider(successful_turns())
        provider.set_context = lambda context: setattr(provider, "context", context)
        providers.append(provider)
        return provider

    with TestClient(create_app(settings, factory)) as client:
        yield client, providers, settings


def proposal(client):
    result = client.post("/api/run", json={"task": "Prepare the report for Alice"}).json()
    return result["conversation_id"], result["messages"][-1]["proposals"][0]["id"]


@pytest.mark.parametrize("decision,status", [("approve", "approved"), ("reject", "rejected")])
def test_decisions_persist_and_only_approval_simulates_send(chat, decision, status):
    client, _, settings = chat
    identifier, action = proposal(client)
    payload = {"conversation_id": identifier, "proposal_id": action, "decision": decision}
    response = client.post("/api/decisions", json=payload)
    assert response.status_code == 200
    assert response.json()["proposal"]["status"] == status
    assert response.json()["sent"] is (decision == "approve")
    assert response.json()["simulated"] is True
    card = response.json()["messages"][1]["proposals"][0]
    assert card == response.json()["proposal"]
    assert card["execution_status"] == ("completed" if decision == "approve" else "not_executed")
    assert ("no real email was sent" if decision == "approve" else "nothing was sent") in response.json()["message"].lower()
    assert client.post("/api/decisions", json=payload).status_code == 409
    assert client.get("/health").json()["outbox_count"] == (1 if decision == "approve" else 0)
    with TestClient(create_app(settings)) as restarted:
        history = restarted.get(f"/api/conversations/{identifier}").json()
        assert history["messages"][1]["proposals"][0]["status"] == status
        assert history["messages"][1]["proposals"][0]["sent"] is (decision == "approve")
        assert history["messages"][1]["proposals"][0] == card
        assert history["messages"][-1]["text"] == response.json()["message"]


@pytest.mark.parametrize("text,status", [("Yes, send it", "approved"), ("No, cancel that", "rejected")])
def test_natural_language_confirmation(chat, text, status):
    client, providers, _ = chat
    identifier, _ = proposal(client)
    result = client.post("/api/run", json={"task": text, "conversation_id": identifier}).json()
    assert result["messages"][1]["proposals"][0]["status"] == status
    assert result["sent"] is (status == "approved")
    assert client.get("/health").json()["outbox_count"] == (1 if status == "approved" else 0)
    assert len(providers) == 1  # No LLM interprets or executes consent.


@pytest.mark.parametrize("reply", ["maybe", "yes, but change the recipient", "sure"])
def test_ambiguous_reply_keeps_proposal_pending(chat, reply):
    client, _, _ = chat
    identifier, _ = proposal(client)
    result = client.post("/api/run", json={"task": reply, "conversation_id": identifier}).json()
    assert "Do you want to approve or reject" in result["message"]
    assert result["messages"][1]["proposals"][0]["status"] == "proposed"


def test_multiple_pending_actions_need_explicit_card_selection(chat):
    client, _, _ = chat
    identifier, _ = proposal(client)
    client.post("/api/run", json={"task": "Prepare another for Bob", "conversation_id": identifier})
    result = client.post("/api/run", json={"task": "Yes, send it", "conversation_id": identifier}).json()
    assert "several pending actions" in result["message"]
    assert all(p["status"] == "proposed" for m in result["messages"] for p in m["proposals"])


def test_followup_context_is_server_owned(chat):
    client, providers, _ = chat
    identifier, _ = proposal(client)
    client.post("/api/run", json={"task": "Summarize that report", "conversation_id": identifier})
    context = providers[-1].context
    assert context[0]["text"] == "Prepare the report for Alice"
    assert "quarterly-report-q3" in context[1]["text"]
    assert "Alice" in context[1]["text"]
    assert client.post("/api/run", json={"task": "test", "history": []}).status_code == 422


def test_proposal_cannot_be_reviewed_from_another_conversation(chat):
    client, _, _ = chat
    _, action = proposal(client)
    other, _ = proposal(client)
    assert client.post("/api/decisions", json={"conversation_id": other,
                       "proposal_id": action, "decision": "approve"}).status_code == 404
    assert client.post("/api/run", json={"task": "test", "conversation_id": "0" * 32}).status_code == 404


def test_readonly_response_has_no_approval_card(tmp_path):
    with TestClient(create_app(Settings("test", "test", tmp_path / "test.db"),
                              lambda _: FakeProvider([Turn([], text="Revenue was $120,000.")]))) as client:
        result = client.post("/api/run", json={"task": "Summarize the report"}).json()
        assert result["message"] == "Revenue was $120,000."
        assert result["messages"][-1]["proposals"] == []


def test_ordinary_yes_without_pending_action_reaches_model(tmp_path):
    with TestClient(create_app(Settings("test", "test", tmp_path / "test.db"),
                              lambda _: FakeProvider([Turn([], text="Here is the summary.")]))) as client:
        conversation = client.post("/api/conversations").json()
        assert conversation["messages"] == []
        result = client.post("/api/run", json={"task": "Yes please",
                             "conversation_id": conversation["conversation_id"]}).json()
        assert result["message"] == "Here is the summary."


def test_combined_answer_and_proposal_are_preserved(tmp_path):
    turns = successful_turns()
    turns[-1] = Turn([], text="The report shows a profit of $35,000.")
    with TestClient(create_app(Settings("test", "test", tmp_path / "test.db"),
                              lambda _: FakeProvider(turns))) as client:
        result = client.post("/api/run", json={"task": "Calculate profit and prepare the report"}).json()
        assert "$35,000" in result["message"]
        assert "Approve or Reject" in result["message"]


def test_gemini_receives_context_and_returns_visible_text():
    with patch("app.provider.genai.Client") as factory:
        factory.return_value.models.generate_content.return_value = types.GenerateContentResponse(candidates=[
            types.Candidate(content=types.Content(role="model", parts=[
                types.Part(text="Private reasoning", thought=True), types.Part(text="Here is the summary.")]))])
        provider = GeminiProvider(Settings("test", "test"))
        provider.set_context([{"role": "user", "text": "Find the report"},
                              {"role": "assistant", "text": "Found the Q3 report"}])
        provider.start("Summarize it")
        assert [item.role for item in provider.history] == ["user", "model", "user"]
        assert provider.next_turn().text == "Here is the summary."
