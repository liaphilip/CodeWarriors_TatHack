"""SQLite outbox and additive migration for simulated execution."""
import sqlite3
from pathlib import Path


def initialize_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY,
            document_id TEXT NOT NULL,
            recipient TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(outbox)")}
        additions = {
            "proposal_id": "TEXT",
            "action_type": "TEXT NOT NULL DEFAULT 'send_document'",
            "status": "TEXT NOT NULL DEFAULT 'legacy'",
            "document_title": "TEXT",
            "document_content": "TEXT",
            "cc": "TEXT", "bcc": "TEXT", "subject": "TEXT", "body": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE outbox ADD COLUMN {name} {definition}")
        # NULL proposal IDs preserve old rows without inventing execution records.
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS outbox_proposal_id ON outbox(proposal_id)")
        connection.execute('''CREATE TABLE IF NOT EXISTS secure_actions (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, run_id TEXT NOT NULL,
            scope TEXT NOT NULL, descriptor TEXT NOT NULL, fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('pending','approved','executed','rejected','blocked','failed')),
            decision TEXT, decided_at TEXT, outbox_id INTEGER UNIQUE,
            result TEXT, verification TEXT NOT NULL DEFAULT 'not_checked'
        )''')
        connection.execute('''CREATE TABLE IF NOT EXISTS security_events (
            id INTEGER PRIMARY KEY, action_id TEXT, event TEXT NOT NULL,
            detail TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )''')
        connection.execute('''CREATE TABLE IF NOT EXISTS attack_runs (
            id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
            scenario TEXT NOT NULL CHECK(scenario IN ('recipient-tampering','misleading-approval')),
            created_at TEXT NOT NULL, completed_at TEXT,
            outcome TEXT NOT NULL CHECK(outcome IN ('INCOMPLETE','PREVENTED','FAILED')),
            evidence TEXT
        )''')
        connection.execute('''CREATE TABLE IF NOT EXISTS chat_attack_runs (
            request_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
            scenario TEXT NOT NULL, attack_run_id TEXT UNIQUE, message_id INTEGER UNIQUE
        )''')
        connection.execute('''CREATE TABLE IF NOT EXISTS pending_attack_runs (
            request_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
            proposal_id TEXT NOT NULL UNIQUE, scenario TEXT NOT NULL,
            run_id TEXT NOT NULL UNIQUE, evidence TEXT NOT NULL,
            message_id INTEGER NOT NULL UNIQUE
        )''')


def outbox_count(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
