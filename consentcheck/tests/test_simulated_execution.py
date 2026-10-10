import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import pytest

from app.conversations import Conversations
from app.database import initialize_database
from app.documents import read_document
from app.execution import simulate_send
from test_conversations import chat, proposal


def decision_payload(identifier, action):
    return {"conversation_id": identifier, "proposal_id": action, "decision": "approve"}


def test_approved_outbox_contains_exact_action_and_snapshot(chat):
    client, _, settings = chat
    identifier, action = proposal(client)
    result = client.post("/api/decisions", json=decision_payload(identifier, action)).json()
    with sqlite3.connect(settings.database) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM outbox").fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    document = read_document("quarterly-report-q3")
    assert row["proposal_id"] == action
    assert row["document_id"] == document["document_id"]
    assert row["document_title"] == document["title"]
    assert row["document_content"] == document["content"]
    assert row["recipient"] == "Alice"
    assert row["action_type"] == "send_document"
    assert row["status"] == "simulated_sent"
    assert row["created_at"]
    assert result["proposal"]["outbox_id"] == row["id"]
    assert result["proposal"]["executed_at"] == row["created_at"]
    assert result["proposal"]["execution_status"] == "completed"


def test_rejection_never_calls_executor(chat):
    client, _, _ = chat
    identifier, action = proposal(client)
    with patch("app.conversations.simulate_send") as execute:
        result = client.post("/api/decisions", json={**decision_payload(identifier, action), "decision": "reject"})
        execute.assert_not_called()
    assert result.json()["proposal"]["status"] == "rejected"
    assert client.post("/api/decisions", json=decision_payload(identifier, action)).status_code == 409
    assert client.get("/health").json()["outbox_count"] == 0


def test_nonexistent_proposal_cannot_execute(chat):
    client, _, _ = chat
    identifier, _ = proposal(client)
    assert client.post("/api/decisions", json=decision_payload(identifier, "0" * 32)).status_code == 404
    assert client.get("/health").json()["outbox_count"] == 0


@pytest.mark.parametrize("change", [
    {"document_id": "missing"}, {"recipient": " "}, {"recipient": 7},
    {"recipient": "Alice\nBcc: Bob"}, {"recipient": "x" * 255},
    {"title": "Unknown report"}, {"action_type": "delete_document"},
])
def test_invalid_stored_details_cannot_execute(chat, change):
    client, _, settings = chat
    identifier, action = proposal(client)
    with sqlite3.connect(settings.database) as db:
        details = json.loads(db.execute("SELECT details FROM proposals WHERE id = ?", (action,)).fetchone()[0])
        details.update(change)
        db.execute("UPDATE proposals SET details = ? WHERE id = ?", (json.dumps(details), action))
    with patch("app.conversations.simulate_send") as execute:
        response = client.post("/api/decisions", json=decision_payload(identifier, action))
        execute.assert_not_called()
    assert response.status_code == 422
    assert client.get("/health").json()["outbox_count"] == 0


@pytest.mark.parametrize("raw", ["not-json", "[]", "null"])
def test_malformed_proposal_cannot_execute(chat, raw):
    client, _, settings = chat
    identifier, action = proposal(client)
    with sqlite3.connect(settings.database) as db:
        db.execute("UPDATE proposals SET details = ? WHERE id = ?", (raw, action))
    assert client.post("/api/decisions", json=decision_payload(identifier, action)).status_code == 422
    assert client.get("/health").json()["outbox_count"] == 0


def test_concurrent_approvals_across_independent_connections_execute_once(chat):
    client, _, settings = chat
    identifier, action = proposal(client)
    ready = Barrier(2)

    def approve():
        ready.wait(timeout=5)
        try:
            return Conversations(settings.database).decide(identifier, action, "approve")[0]["status"]
        except ValueError:
            return "already_reviewed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: approve(), range(2)))
    assert sorted(results) == ["already_reviewed", "approved"]
    assert client.get("/health").json()["outbox_count"] == 1
    history = client.get(f"/api/conversations/{identifier}").json()
    assert sum(message["text"].startswith("Done!") for message in history["messages"]) == 1


@pytest.mark.parametrize("via_chat", [False, True])
def test_partial_execution_failure_rolls_back_and_persists_failure(chat, via_chat):
    client, _, settings = chat
    identifier, action = proposal(client)

    def fail_after_insert(db, proposal_id, details):
        simulate_send(db, proposal_id, details)
        raise RuntimeError("private internal details must not be shown")

    with patch("app.conversations.simulate_send", side_effect=fail_after_insert):
        response = (client.post("/api/run", json={"task": "Yes, send it", "conversation_id": identifier})
                    if via_chat else client.post("/api/decisions", json=decision_payload(identifier, action)))
    assert response.status_code == 503
    assert "failed" in response.json()["detail"]
    assert "private internal" not in response.text
    assert client.get("/health").json()["outbox_count"] == 0
    history = client.get(f"/api/conversations/{identifier}").json()
    saved = history["messages"][1]["proposals"][0]
    assert saved["status"] == "failed"
    assert saved["execution_status"] == "failed"
    assert saved["sent"] is False
    assert not any(message["text"].startswith("Done!") for message in history["messages"])
    assert "failed" in history["messages"][-1]["text"]
    assert client.post("/api/decisions", json=decision_payload(identifier, action)).status_code == 409


def test_failure_to_save_confirmation_rolls_back_entire_approval(chat):
    client, _, settings = chat
    identifier, action = proposal(client)
    with sqlite3.connect(settings.database) as db:
        db.execute("""CREATE TRIGGER fail_confirmation BEFORE INSERT ON messages
                      WHEN NEW.text LIKE 'Done!%'
                      BEGIN SELECT RAISE(ABORT, 'test write failure'); END""")
    response = client.post("/api/decisions", json=decision_payload(identifier, action))
    assert response.status_code == 503
    assert client.get("/health").json()["outbox_count"] == 0
    history = client.get(f"/api/conversations/{identifier}").json()
    assert history["messages"][1]["proposals"][0]["status"] == "proposed"
    assert not any(message["text"].startswith("Done!") for message in history["messages"])


def test_additive_migration_preserves_legacy_rows_and_is_repeatable(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE outbox (id INTEGER PRIMARY KEY, document_id TEXT NOT NULL,
                   recipient TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        db.execute("INSERT INTO outbox (document_id, recipient) VALUES ('old-doc', 'Old recipient')")
    initialize_database(path)
    initialize_database(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT id, document_id, recipient, proposal_id, status FROM outbox").fetchall() == [
            (1, "old-doc", "Old recipient", None, "legacy")]


def test_previous_approval_is_not_retroactively_executed(chat):
    client, _, settings = chat
    identifier, action = proposal(client)
    with sqlite3.connect(settings.database) as db:
        db.execute("UPDATE proposals SET status = 'approved' WHERE id = ?", (action,))
    initialize_database(settings.database)
    assert client.post("/api/decisions", json=decision_payload(identifier, action)).status_code == 409
    history = client.get(f"/api/conversations/{identifier}").json()
    assert history["messages"][1]["proposals"][0]["sent"] is False
    assert client.get("/health").json()["outbox_count"] == 0
