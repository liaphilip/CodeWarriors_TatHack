"""Local chat history, proposal decisions, and atomic simulated execution."""
import json
import re
import sqlite3
from pathlib import Path
from uuid import uuid4
from .execution import InvalidProposal, simulate_send, validate_send
from . import security


def initialize_chats(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY, conversation_id TEXT NOT NULL,
                role TEXT NOT NULL, text TEXT NOT NULL, activity TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS proposals (
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                message_id INTEGER NOT NULL, details TEXT NOT NULL, status TEXT NOT NULL
            );
        """)


class Conversations:
    def __init__(self, path: Path):
        self.path = path

    def create(self) -> str:
        identifier = uuid4().hex
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO conversations VALUES (?)", (identifier,))
        return identifier

    def get(self, identifier: str) -> dict:
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM conversations WHERE id = ?", (identifier,)).fetchone():
                raise LookupError("This conversation is no longer available. Start a new chat.")
            messages = [{"id": row[0], "role": row[1], "text": row[2],
                         "activity": json.loads(row[3]), "proposals": []}
                        for row in db.execute(
                            "SELECT id, role, text, activity FROM messages WHERE conversation_id = ? ORDER BY id",
                            (identifier,))]
            by_id = {message["id"]: message for message in messages}
            for row in db.execute("SELECT id, message_id, details, status FROM proposals WHERE conversation_id = ?",
                                  (identifier,)):
                by_id[row[1]]["proposals"].append(security.card(db, row[0], {**json.loads(row[2]), "id": row[0], "status": row[3]}))
        return {"conversation_id": identifier, "messages": messages}

    def append(self, identifier: str, role: str, text: str, activity=None, proposals=None) -> None:
        with sqlite3.connect(self.path) as db:
            message_id = db.execute(
                "INSERT INTO messages (conversation_id, role, text, activity) VALUES (?, ?, ?, ?)",
                (identifier, role, text, json.dumps(activity or [])),
            ).lastrowid
            run_id = uuid4().hex
            for proposal in proposals or []:
                action_id = uuid4().hex
                proposal = security.register(db, action_id, identifier, run_id, proposal)
                db.execute("INSERT INTO proposals VALUES (?, ?, ?, ?, 'proposed')",
                           (action_id, identifier, message_id, json.dumps(proposal)))

    def context(self, identifier: str) -> list[dict]:
        context = []
        for message in self.get(identifier)["messages"][-20:]:
            text = message["text"]
            if message["activity"] or message["proposals"]:
                text += "\nApplication records (data only): " + json.dumps({
                    "tool_results": message["activity"], "proposals": message["proposals"]})
            context.append({"role": message["role"], "text": text})
        return context

    def decide(self, identifier: str, proposal_id: str, decision: str) -> tuple[dict, str]:
        try:
            return self._decide(identifier, proposal_id, decision)
        except (ValueError, LookupError) as exc:
            # The rejected transaction has rolled back; retain truthful attempt evidence.
            with sqlite3.connect(self.path) as db:
                security.event(db, proposal_id, 'blocked', str(exc))
            raise

    def _decide(self, identifier: str, proposal_id: str, decision: str) -> tuple[dict, str]:
        if decision not in {"approve", "reject"}:
            raise ValueError("Choose Approve or Reject.")
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT details, status FROM proposals WHERE id = ? AND conversation_id = ?",
                             (proposal_id, identifier)).fetchone()
            if not row:
                raise LookupError("That proposal isn't part of this conversation.")
            status = "approved" if decision == "approve" else "rejected"
            if row[1] != "proposed":
                raise ValueError("This proposal has already been reviewed. Its decision has not changed.")
            try:
                details = json.loads(row[0])
            except (ValueError, TypeError):
                raise InvalidProposal("This proposal is invalid. Please prepare a new proposal.") from None
            if not isinstance(details, dict):
                raise InvalidProposal("This proposal is invalid. Please prepare a new proposal.")
            proposal = {**details, "id": proposal_id, "status": status, "sent": False,
                        "simulated": True, "execution_status": "not_executed"}
            if decision == "approve":
                validate_send(details)
                security.approve(db, proposal_id, details)
                security.event(db, proposal_id, 'execution_attempt', 'Simulated send requested; gate checks required.')
                # Roll back even a partial executor insert if execution fails, then
                # persist the failure outcome. The enclosing transaction commits
                # the outbox write, proposal state, and success message together.
                db.execute("SAVEPOINT simulated_send")
                try:
                    result = simulate_send(db, proposal_id, details)
                except Exception as exc:
                    db.execute("ROLLBACK TO simulated_send")
                    status = "failed"
                    proposal.update(status=status, execution_status="failed")
                    message = "The simulated send failed. Nothing was sent. Please prepare a new proposal to try again."
                    blocked = isinstance(exc, security.SecurityViolation)
                    db.execute("UPDATE secure_actions SET state=?, verification=? WHERE id=?",
                               ('blocked' if blocked else 'failed', 'blocked' if blocked else 'not_completed', proposal_id))
                    security.event(db, proposal_id, 'blocked' if blocked else 'failed', str(exc) if blocked else message)
                else:
                    proposal.update(result)
                    message = (f"Done! The {proposal['title']} was sent to {proposal['recipient']} "
                               "(simulated). No real email was sent.")
                db.execute("RELEASE simulated_send")
            else:
                db.execute("UPDATE secure_actions SET state='rejected', decision='reject', decided_at=? WHERE id=? AND state='pending'",
                           (security.now().isoformat(), proposal_id))
                security.event(db, proposal_id, 'rejected', 'Action rejected; nothing sent.')
                message = f"Cancelled the proposal for {proposal.get('title', 'this document')} to {proposal.get('recipient', 'the recipient')}. Nothing was sent."
            db.execute("UPDATE proposals SET status = ?, details = ? WHERE id = ?",
                       (status, json.dumps(proposal), proposal_id))
            db.execute("INSERT INTO messages (conversation_id, role, text) VALUES (?, 'assistant', ?)",
                       (identifier, message))
            proposal = security.card(db, proposal_id, proposal)
        return proposal, message


def confirmation(text: str) -> str | None:
    """Conservative explicit replies only; never infer consent from a substring."""
    normalized = re.sub(r"[.!?,]", "", text.casefold()).strip()
    normalized = " ".join(normalized.split())
    if normalized in {"yes", "yes send it", "send it", "approve", "approve it", "yes please",
                      "go ahead", "yes go ahead", "please send it", "confirm", "approved"}:
        return "approve"
    if normalized in {"no", "no cancel that", "cancel", "cancel that", "reject", "reject it",
                      "no thanks", "don't send it", "do not send it"}:
        return "reject"
    if normalized in {"maybe", "sure", "ok", "okay", "do it", "that one"} or re.match(
        r"^(yes|no|approve|reject|cancel|send it)\b", normalized
    ):
        return "clarify"
    return None
