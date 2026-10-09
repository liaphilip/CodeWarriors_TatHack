import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from approval_store import ApprovalError, get_approval, record_decision
from database import list_outbox, transaction
from executor import execute_approval


def test_execute_approved_action_once(api, proposal):
    _, approval = api("POST", "/api/approvals", proposal.model_dump())
    approval_id = approval["approval_id"]
    status, evidence = api("POST", f"/api/execute/{approval_id}")
    assert status == 200 and evidence["status"] == "SENT"
    assert evidence["approval_id"] == approval_id
    assert api("GET", "/api/outbox") == (200, [evidence])
    assert api("GET", f"/api/approvals/{approval_id}")[1]["status"] == "EXECUTED"
    status, error = api("POST", f"/api/execute/{approval_id}")
    assert status == 409 and "already been executed" in error["detail"]
    assert len(api("GET", "/api/outbox")[1]) == 1
    assert api("POST", f"/api/approvals/{approval_id}/reject")[0] == 409


def test_execute_ignores_all_client_action_overrides(api, proposal):
    _, approval = api("POST", "/api/approvals", proposal.model_dump())
    status, evidence = api("POST", f'/api/execute/{approval["approval_id"]}', {
        "recipient": "attacker@example.com", "document_name": "stolen.txt",
        "document_content": "malicious replacement", "tool": "shell", "status": "APPROVED",
    })
    assert status == 200
    for field in ["recipient", "document_name", "document_content"]:
        assert evidence[field] == getattr(proposal, field)
    assert api("GET", "/api/outbox")[1] == [evidence]


def test_expired_approval_is_persistently_blocked(api, db_path, proposal):
    approval_id = record_decision(proposal, db_path)["approval_id"]
    with transaction(db_path) as connection:
        connection.execute("UPDATE approvals SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (approval_id,))
    status, error = api("POST", f"/api/execute/{approval_id}")
    assert status == 403 and error["detail"] == "Approval expired."
    assert get_approval(approval_id, db_path)["status"] == "EXPIRED"
    assert api("POST", f"/api/approvals/{approval_id}/reject")[0] == 403
    assert list_outbox(db_path) == []


def test_rejected_approval_cannot_execute(api, proposal):
    _, approval = api("POST", "/api/approvals", proposal.model_dump())
    approval_id = approval["approval_id"]
    assert api("POST", f"/api/approvals/{approval_id}/reject")[0] == 200
    assert api("POST", f"/api/execute/{approval_id}")[0] == 403
    assert api("GET", "/api/outbox")[1] == []


def test_pending_approval_cannot_execute(db_path, proposal):
    approval_id = record_decision(proposal, db_path)["approval_id"]
    with transaction(db_path) as connection:
        connection.execute("UPDATE approvals SET status='PENDING' WHERE id=?", (approval_id,))
    with pytest.raises(ApprovalError) as error:
        execute_approval(approval_id, db_path)
    assert error.value.status_code == 403
    assert list_outbox(db_path) == []


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/execute/missing"), ("GET", "/api/approvals/missing"),
    ("POST", "/api/approvals/missing/reject"),
])
def test_unknown_approval_returns_404(api, method, path):
    assert api(method, path)[0] == 404


def test_execution_rolls_back_outbox_when_status_update_fails(db_path, proposal):
    approval_id = record_decision(proposal, db_path)["approval_id"]
    with transaction(db_path) as connection:
        connection.execute("""CREATE TRIGGER fail_execution BEFORE UPDATE ON approvals
            WHEN NEW.status = 'EXECUTED'
            BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        execute_approval(approval_id, db_path)
    assert list_outbox(db_path) == []
    assert get_approval(approval_id, db_path)["status"] == "APPROVED"


def test_concurrent_execution_creates_exactly_one_record(db_path, proposal):
    approval_id = record_decision(proposal, db_path)["approval_id"]

    def attempt(_):
        try:
            execute_approval(approval_id, db_path)
            return 200
        except ApprovalError as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert sorted(results) == [200, 409, 409, 409]
    assert len(list_outbox(db_path)) == 1
