"""Simulate send_document using only values from a stored approval."""

from approval_store import ApprovalError, expire_if_due, require_approval, utc_now
from database import DEFAULT_DB_PATH, transaction


def execute_approval(approval_id: str, db_path=DEFAULT_DB_PATH, *, claimed_action=None):
    expired = False
    with transaction(db_path) as connection:
        approval = require_approval(connection, approval_id)
        if approval["status"] == "EXECUTED":
            raise ApprovalError(409, "Approval has already been executed.")
        expired = expire_if_due(connection, approval)
        if not expired:
            if approval["status"] != "APPROVED":
                raise ApprovalError(403, "Approval is not executable; human approval is required.")
            if approval["tool"] != "send_document":
                raise ApprovalError(400, "Unsupported tool.")
            if claimed_action is not None:
                mismatches = [field for field in (
                    "actor_id", "task_id", "tool", "recipient", "document_name", "document_content"
                ) if claimed_action.get(field) != approval[field]]
                if mismatches:
                    raise ApprovalError(403, "Execution request does not match approved action: "
                                        + ", ".join(mismatches) + ".")
            cursor = connection.execute("""
                INSERT INTO outbox (approval_id, recipient, document_name,
                    document_content, executed_at, status)
                VALUES (?, ?, ?, ?, ?, 'SENT')
            """, (approval_id, approval["recipient"], approval["document_name"],
                  approval["document_content"], utc_now().isoformat()))
            connection.execute("UPDATE approvals SET status = 'EXECUTED' WHERE id = ?", (approval_id,))
            evidence = dict(connection.execute("SELECT * FROM outbox WHERE id = ?", (cursor.lastrowid,)).fetchone())
    if expired:
        raise ApprovalError(403, "Approval expired.")
    return evidence
