"""Server-owned approval records; no interface edits approved action values."""

import secrets
from datetime import datetime, timedelta, timezone

from database import DEFAULT_DB_PATH, transaction
from schemas import ActionProposal


class ApprovalError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def utc_now():
    return datetime.now(timezone.utc)


def as_approval(row):
    result = dict(row)
    result["approval_id"] = result.pop("id")
    return result


def require_approval(connection, approval_id):
    row = connection.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    if row is None:
        raise ApprovalError(404, "Approval not found.")
    return row


def expire_if_due(connection, row):
    if row["status"] in {"PENDING", "APPROVED"} and datetime.fromisoformat(row["expires_at"]) <= utc_now():
        connection.execute("UPDATE approvals SET status = 'EXPIRED' WHERE id = ?", (row["id"],))
        return True
    return row["status"] == "EXPIRED"


def record_decision(proposal: ActionProposal, db_path=DEFAULT_DB_PATH, *, approved=True):
    # Validate here as well, so future callers use the same boundary as the API.
    proposal = ActionProposal.model_validate(proposal)
    now = utc_now()
    record = {
        "id": secrets.token_urlsafe(32),
        **proposal.model_dump(),
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=10)).isoformat(),
        "status": "APPROVED" if approved else "REJECTED",
    }
    with transaction(db_path) as connection:
        connection.execute("""
            INSERT INTO approvals (id, actor_id, task_id, tool, recipient,
                document_name, document_content, created_at, expires_at, status)
            VALUES (:id, :actor_id, :task_id, :tool, :recipient,
                :document_name, :document_content, :created_at, :expires_at, :status)
        """, record)
    return as_approval(record)


def get_approval(approval_id, db_path=DEFAULT_DB_PATH):
    with transaction(db_path) as connection:
        row = require_approval(connection, approval_id)
        expire_if_due(connection, row)
        return as_approval(require_approval(connection, approval_id))


def reject_approval(approval_id, db_path=DEFAULT_DB_PATH):
    expired = False
    with transaction(db_path) as connection:
        row = require_approval(connection, approval_id)
        expired = expire_if_due(connection, row)
        if not expired:
            if row["status"] not in {"PENDING", "APPROVED"}:
                raise ApprovalError(409, "Only a pending or approved action can be rejected.")
            connection.execute("UPDATE approvals SET status = 'REJECTED' WHERE id = ?", (approval_id,))
            result = as_approval(require_approval(connection, approval_id))
    # Raise after commit so the expiry status remains recorded.
    if expired:
        raise ApprovalError(403, "Approval expired.")
    return result
