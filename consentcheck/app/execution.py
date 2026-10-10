"""Simulated sends only: a local database insert, with no email/network client."""
import sqlite3
import json

from pydantic import ValidationError

from .documents import read_document
from .schemas import ProposeInput
from .security import gate, event


class InvalidProposal(ValueError):
    """The stored proposal cannot be executed."""


def validate_send(details: dict) -> dict:
    try:
        data = ProposeInput.model_validate({
            "document_id": details.get("document_id"), "recipient": details.get("recipient"),
        })
        document = read_document(data.document_id)
        if (details.get("action_type", "send_document") != "send_document"
                or details.get("title") != document["title"]
                or any(ord(char) < 32 or ord(char) == 127 for char in data.recipient)):
            raise ValueError()
    except (ValidationError, ValueError, AttributeError):
        raise InvalidProposal("This proposal has invalid document or recipient details. Please prepare a new proposal.") from None
    return {**document, "recipient": data.recipient, "action_type": "send_document"}


def simulate_send(db: sqlite3.Connection, proposal_id: str, details: dict) -> dict:
    """Insert using the caller's transaction; do not commit or call external services."""
    action, document = gate(db, proposal_id, details)
    cursor = db.execute("""
        INSERT INTO outbox (proposal_id, document_id, recipient, action_type, status,
                            document_title, document_content, cc, bcc, subject, body)
        VALUES (?, ?, ?, 'send_document', 'simulated_sent', ?, ?, ?, ?, ?, ?)
    """, (proposal_id, action.document_id, action.recipient,
          action.title, document["content"], json.dumps(action.cc), json.dumps(action.bcc), action.subject, action.body))
    timestamp = db.execute("SELECT created_at FROM outbox WHERE id = ?", (cursor.lastrowid,)).fetchone()[0]
    result = {"sent": True, "simulated": True, "execution_status": "completed",
            "outbox_id": cursor.lastrowid, "executed_at": timestamp}
    db.execute("UPDATE secure_actions SET state='executed', outbox_id=?, result=?, verification='permitted' WHERE id=?",
               (cursor.lastrowid, json.dumps(result), proposal_id))
    event(db, proposal_id, 'executed', 'Simulated send committed with one-time approval consumption.')
    return result
