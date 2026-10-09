"""SQLite storage. Each operation uses its own connection."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "consentcheck.db"


@contextmanager
def connect(db_path=DEFAULT_DB_PATH):
    connection = sqlite3.connect(str(db_path), timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def transaction(db_path=DEFAULT_DB_PATH):
    with connect(db_path) as connection:
        # Serialize verification + side effect, including concurrent executors.
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def init_database(db_path=DEFAULT_DB_PATH):
    with connect(db_path) as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY,
                actor_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                tool TEXT NOT NULL CHECK (tool = 'send_document'),
                recipient TEXT NOT NULL,
                document_name TEXT NOT NULL,
                document_content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN
                    ('PENDING', 'APPROVED', 'EXECUTED', 'REJECTED', 'EXPIRED'))
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                approval_id TEXT NOT NULL UNIQUE REFERENCES approvals(id),
                recipient TEXT NOT NULL,
                document_name TEXT NOT NULL,
                document_content TEXT NOT NULL,
                executed_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status = 'SENT')
            );
        """)


def reset_database(db_path=DEFAULT_DB_PATH):
    with transaction(db_path) as connection:
        connection.execute("DELETE FROM outbox")
        connection.execute("DELETE FROM approvals")
        connection.execute("DELETE FROM sqlite_sequence WHERE name = 'outbox'")


def list_outbox(db_path=DEFAULT_DB_PATH):
    with connect(db_path) as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM outbox ORDER BY id DESC")]
