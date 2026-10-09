"""Exercise the simulator and existing execution boundary through real HTTP."""

import pytest

from attack_engine import AttackRunError, run_attack
from mutations import MUTATIONS, mutate


@pytest.mark.parametrize("attack_type", list(MUTATIONS))
def test_attack_endpoint_blocks_and_preserves_evidence(api, attack_type):
    status, result = api("POST", f"/api/attacks/{attack_type}")
    assert status == 200
    assert result["status"] == "PASS"
    assert result["expected_blocked"] and result["blocked"]
    assert not result["unauthorized_effect"]
    assert not result["side_effect_created"]
    assert result["http_status"] == (409 if attack_type == "replay" else 403)
    assert result["outbox_before"] == result["outbox_after"]
    assert len(result["outbox_after"]) == (1 if attack_type == "replay" else 0)
    stored = api("GET", f'/api/approvals/{result["approval_id"]}')[1]
    for key, value in result["original_approved_action"].items():
        assert stored[key] == value
    assert stored["status"] == ("EXECUTED" if attack_type == "replay" else "APPROVED")


def test_legitimate_envelope_executes_exactly_once(api, proposal):
    _, approval = api("POST", "/api/approvals", proposal.model_dump())
    path = f'/api/execute/{approval["approval_id"]}'
    status, row = api("POST", path, {"action": proposal.model_dump()})
    assert status == 200
    assert row["recipient"] == proposal.recipient
    assert row["document_content"] == proposal.document_content
    assert api("GET", "/api/outbox") == (200, [row])
    assert api("POST", path, {"action": proposal.model_dump()})[0] == 409
    assert api("GET", "/api/outbox") == (200, [row])


@pytest.mark.parametrize("field,value", [
    ("recipient", "attacker@example.com"),
    ("document_name", "substituted.pdf"),
    ("document_content", "This is malicious substituted content."),
    ("document_content", "This is a simulated project report for the ConsentCheck MVP. "),
    ("task_id", "different_task"),
    ("actor_id", "different_actor"),
])
def test_assertion_mismatch_creates_no_outbox_and_keeps_approval(api, proposal, field, value):
    _, approval = api("POST", "/api/approvals", proposal.model_dump())
    attack = {"action": {**proposal.model_dump(), field: value}}
    path = f'/api/execute/{approval["approval_id"]}'
    status, error = api("POST", path, attack)
    assert status == 403 and field in error["detail"]
    assert api("GET", "/api/outbox") == (200, [])
    assert api("GET", f'/api/approvals/{approval["approval_id"]}')[1] == approval
    assert api("POST", path, {"action": proposal.model_dump()})[0] == 200


@pytest.mark.parametrize("body", [
    {"action": None}, {"action": {}},
    {"action": {"tool": "shell"}},
])
def test_invalid_envelope_rejected_without_execution(api, proposal, body):
    _, approval = api("POST", "/api/approvals", proposal.model_dump())
    assert api("POST", f'/api/execute/{approval["approval_id"]}', body)[0] == 422
    assert api("GET", "/api/outbox") == (200, [])


def test_envelope_cannot_supply_server_metadata(api, proposal):
    _, approval = api("POST", "/api/approvals", proposal.model_dump())
    assert api("POST", f'/api/execute/{approval["approval_id"]}', {
        "action": proposal.model_dump(), "status": "APPROVED", "expires_at": "2099-01-01"
    })[0] == 422
    assert api("GET", "/api/outbox") == (200, [])


def test_all_attacks_leave_only_one_legitimate_replay_row(api):
    results = [api("POST", f"/api/attacks/{kind}")[1] for kind in MUTATIONS]
    rows = api("GET", "/api/outbox")[1]
    assert len(rows) == 1
    replay = next(result for result in results if result["attack_type"] == "replay")
    assert rows == replay["outbox_after"]
    assert all(result["status"] == "PASS" for result in results)
    assert rows[0]["recipient"] != "attacker@example.com"


def test_attack_evidence_scoped_to_own_approval(api, proposal):
    _, approval = api("POST", "/api/approvals", proposal.model_dump())
    api("POST", f'/api/execute/{approval["approval_id"]}')
    previous = api("GET", "/api/outbox")[1]
    result = api("POST", "/api/attacks/content_substitution")[1]
    assert result["status"] == "PASS" and result["outbox_after"] == []
    assert api("GET", "/api/outbox")[1] == previous


@pytest.mark.parametrize("attack_type", list(MUTATIONS))
def test_mutations_are_deterministic_and_do_not_edit_original(proposal, attack_type):
    original = proposal.model_dump()
    first = mutate(proposal, attack_type)
    assert first == mutate(proposal, attack_type)
    assert first is not proposal
    assert proposal.model_dump() == original
    if attack_type == "content_substitution":
        assert first.document_name == proposal.document_name
        assert first.task_id == proposal.task_id
        assert first.document_content != proposal.document_content


@pytest.mark.parametrize("fake_status", [200, 403])
def test_classifier_fails_if_execution_writes_despite_reported_status(api, fake_status):
    def compromised_request(method, path, data=None):
        if path.startswith("/api/execute/"):
            status, body = api(method, path)  # Test double strips assertions.
            if fake_status == 403:
                return 403, {"detail": "Execution request does not match approved action: recipient."}
            return status, body
        return api(method, path, data)

    result = run_attack("recipient_substitution", compromised_request)
    assert result.status == "FAIL"
    assert result.side_effect_created and result.unauthorized_effect


@pytest.mark.parametrize("status", [404, 422, 500, 503])
def test_unrelated_http_errors_never_count_as_pass(api, status):
    def broken_request(method, path, data=None):
        if path.startswith("/api/execute/"):
            return status, {"detail": "Unrelated error"}
        return api(method, path, data)

    result = run_attack("recipient_substitution", broken_request)
    assert result.status == "FAIL" and not result.blocked
    assert not result.unauthorized_effect


def test_failed_setup_is_error_not_pass():
    with pytest.raises(AttackRunError, match="setup or evidence"):
        run_attack("replay", lambda *args: (503, {"detail": "Unavailable"}))


def test_unknown_attack_rejected(api):
    assert api("POST", "/api/attacks/not_real")[0] == 422
    assert api("GET", "/api/outbox") == (200, [])
