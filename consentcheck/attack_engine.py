"""Unprivileged simulator: all setup, execution and evidence travel via HTTP.

No database, approval-store or executor imports belong in this module.
The injected request callable also lets tests probe the result classifier.
"""

import json
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from mutations import mutate
from schemas import ActionProposal, AttackResult, AttackType, ExecutionEvidence


class AttackRunError(Exception):
    """A setup/transport/evidence error is not a successful blocked attack."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_api_client(port: int):
    # No external destinations, environment proxies, or redirects.
    opener = build_opener(ProxyHandler({}), NoRedirects())

    def request(method, path, data=None):
        body = None if data is None else json.dumps(data).encode("utf-8")
        req = Request(f"http://127.0.0.1:{port}{path}", data=body, method=method,
                      headers={"Content-Type": "application/json"})
        try:
            try:
                response = opener.open(req, timeout=10)
            except HTTPError as error:
                response = error
            with response:
                return response.status, json.loads(response.read())
        except (URLError, OSError, ValueError):
            raise AttackRunError("Local attack request failed. Check the server and retry.") from None

    return request


def run_attack(attack_type: AttackType, request) -> AttackResult:
    def required(method, path, data=None, expected=200):
        status, result = request(method, path, data)
        if status != expected:
            raise AttackRunError(f"Attack setup or evidence request failed (HTTP {status}).")
        return result

    try:
        proposal = ActionProposal.model_validate(required("POST", "/api/proposal"))
        approved = required("POST", "/api/approvals", proposal.model_dump(), expected=201)
        approval_id = approved["approval_id"]
        # Read the authoritative snapshot back through the public API.
        stored = required("GET", f"/api/approvals/{approval_id}")
        original = ActionProposal.model_validate({k: stored[k] for k in ActionProposal.model_fields})
        if stored["status"] != "APPROVED" or original != proposal:
            raise AttackRunError("Could not verify the legitimate approval for this attack.")

        def evidence():
            rows = required("GET", "/api/outbox")
            return [ExecutionEvidence.model_validate(row) for row in rows
                    if row["approval_id"] == approval_id]

        if evidence():
            raise AttackRunError("A new approval unexpectedly already has execution evidence.")
        setup_executed = attack_type == "replay"
        if setup_executed:
            required("POST", f"/api/execute/{approval_id}", {"action": original.model_dump()})
        before = evidence()
        if setup_executed and (len(before) != 1 or any(
            getattr(before[0], field) != getattr(original, field)
            for field in ("recipient", "document_name", "document_content")
        )):
            raise AttackRunError("Replay setup did not produce exactly one legitimate outbox record.")

        mutated = mutate(original, attack_type)
        http_status, response = request("POST", f"/api/execute/{approval_id}",
                                        {"action": mutated.model_dump()})
        after = evidence()
        detail = response.get("detail", "Execution succeeded.")
        expected_status = 409 if setup_executed else 403
        expected_reason = ("Approval has already been executed." if setup_executed
                           else "Execution request does not match approved action:")
        blocked = http_status == expected_status and expected_reason in str(detail)
        before_ids = {row.id for row in before}
        side_effect_created = any(row.id not in before_ids for row in after)
        # No write is authorized during the attack phase, even a write with the
        # original values. Replay's one legitimate setup row must stay unchanged.
        unauthorized_effect = after != before
        return AttackResult(
            attack_type=attack_type, approval_id=approval_id,
            original_approved_action=original, mutated_action=mutated,
            http_status=http_status, response_detail=str(detail), blocked=blocked,
            side_effect_created=side_effect_created, unauthorized_effect=unauthorized_effect,
            outbox_before=before, outbox_after=after, legitimate_setup_executed=setup_executed,
            status="PASS" if blocked and not unauthorized_effect else "FAIL",
        )
    except (KeyError, TypeError, ValueError):
        raise AttackRunError("Attack setup or evidence returned an invalid response.") from None
