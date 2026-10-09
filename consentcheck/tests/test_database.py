import sqlite3

import pytest

from approval_store import record_decision
from database import connect, init_database, list_outbox, reset_database, transaction
from executor import execute_approval


def test_database_created_and_initialization_is_idempotent(db_path):
    init_database(db_path)
    with connect(db_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"approvals", "outbox"} <= tables


def test_database_unique_constraint_prevents_duplicate_outbox(db_path, proposal):
    approval = record_decision(proposal, db_path)
    execute_approval(approval["approval_id"], db_path)
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(db_path) as connection:
            connection.execute("""INSERT INTO outbox
                (approval_id, recipient, document_name, document_content, executed_at, status)
                SELECT approval_id, recipient, document_name, document_content, executed_at, status FROM outbox""")
    assert len(list_outbox(db_path)) == 1


def test_reset_clears_both_tables(db_path, proposal):
    approval = record_decision(proposal, db_path)
    execute_approval(approval["approval_id"], db_path)
    reset_database(db_path)
    assert list_outbox(db_path) == []
    with connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
