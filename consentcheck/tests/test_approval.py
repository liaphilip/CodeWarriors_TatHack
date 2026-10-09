from datetime import datetime, timedelta

import pytest

from approval_store import record_decision
from database import transaction


def test_create_approval_has_server_id_and_timestamps(api, proposal):
    status, first = api("POST", "/api/approvals", proposal.model_dump())
    _, second = api("POST", "/api/approvals", proposal.model_dump())
    assert status == 201
    assert first["status"] == "APPROVED"
    assert len(first["approval_id"]) >= 32
    assert first["approval_id"] != second["approval_id"]
    assert datetime.fromisoformat(first["expires_at"]) - datetime.fromisoformat(first["created_at"]) == timedelta(minutes=10)
    status, stored = api("GET", f'/api/approvals/{first["approval_id"]}')
    assert status == 200
    assert stored == first


@pytest.mark.parametrize("field,value", [
    ("approval_id", "client-chosen"), ("status", "EXECUTED"),
    ("created_at", "2000-01-01"), ("expires_at", "2099-01-01"),
])
def test_client_cannot_supply_server_owned_fields(api, proposal, field, value):
    status, _ = api("POST", "/api/approvals", {**proposal.model_dump(), field: value})
    assert status == 422


@pytest.mark.parametrize("field,value", [
    ("tool", "shell"), ("recipient", "not-an-email"), ("actor_id", " "),
    ("task_id", ""), ("document_name", "../secret.txt"), ("document_content", ""),
])
def test_invalid_proposal_rejected(api, proposal, field, value):
    status, _ = api("POST", "/api/approvals", {**proposal.model_dump(), field: value})
    assert status == 422


def test_dashboard_demo_and_assets(api):
    status, page = api("GET", "/")
    assert status == 200 and "ConsentCheck" in page
    for path in ["/static/style.css", "/static/app.js"]:
        assert api("GET", path)[0] == 200
    status, demo = api("POST", "/api/proposal")
    assert status == 200 and demo["recipient"] == "mentor@example.com"


def test_initial_proposal_can_be_rejected_without_approval(api, proposal):
    status, rejected = api("POST", "/api/proposal/reject", proposal.model_dump())
    assert status == 201 and rejected["status"] == "REJECTED"
    assert api("POST", f'/api/execute/{rejected["approval_id"]}')[0] == 403
    assert api("GET", "/api/outbox")[1] == []


def test_only_pending_or_approved_can_be_rejected(api, db_path, proposal):
    approval = record_decision(proposal, db_path)
    approval_id = approval["approval_id"]
    with transaction(db_path) as connection:
        connection.execute("UPDATE approvals SET status='PENDING' WHERE id=?", (approval_id,))
    status, rejected = api("POST", f"/api/approvals/{approval_id}/reject")
    assert status == 200 and rejected["status"] == "REJECTED"
    assert api("POST", f"/api/approvals/{approval_id}/reject")[0] == 409


def test_reset_api_restores_default_proposal(api, proposal):
    _, approval = api("POST", "/api/approvals", proposal.model_dump())
    api("POST", f'/api/execute/{approval["approval_id"]}')
    assert api("POST", "/api/reset") == (200, proposal.model_dump())
    assert api("GET", "/api/outbox") == (200, [])
    assert api("GET", f'/api/approvals/{approval["approval_id"]}')[0] == 404
