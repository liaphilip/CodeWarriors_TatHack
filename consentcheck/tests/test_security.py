import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import security
from app.config import Settings
from app.conversations import Conversations
from app.database import initialize_database
from app.execution import simulate_send
from app.main import create_app
from app.tools import ToolRunner
from test_conversations import chat, proposal


def details(path, action):
    with sqlite3.connect(path) as db:
        return json.loads(db.execute('SELECT details FROM proposals WHERE id=?', (action,)).fetchone()[0])


def approve(client, conversation, action_id, **extra):
    return client.post('/api/decisions', json={'conversation_id': conversation, 'proposal_id': action_id,
                                             'decision': 'approve', **extra})


@pytest.mark.parametrize('change', [
    {'recipient': 'Mallory'}, {'cc': ['Mallory']}, {'bcc': ['Mallory']},
    {'subject': 'Changed'}, {'body': 'Changed'}, {'document_id': 'project-roadmap'},
    {'run_id': '0' * 32}, {'task_id': '0' * 32}, {'scope': 'real_email'},
    {'action_type': 'delete_document'}, {'tool': 'send_email'}, {'schema_version': 2},
    {'content_sha256': '0' * 64},
])
def test_actual_operation_tampering_blocked_at_writer(chat, change):
    client, _, settings = chat
    conversation, action = proposal(client)
    operation = details(settings.database, action)
    with sqlite3.connect(settings.database) as db:
        db.execute('BEGIN IMMEDIATE')
        security.approve(db, action, operation)
        with pytest.raises(security.SecurityViolation):
            simulate_send(db, action, {**operation, **change})
        assert db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 0
        assert security.load(db, action)['state'] == 'approved'


@pytest.mark.parametrize('field', ['cc', 'bcc', 'subject', 'body', 'scope', 'run_id'])
def test_missing_canonical_fields_cannot_silently_default(chat, field):
    client, _, settings = chat
    _, action = proposal(client)
    operation = details(settings.database, action)
    with sqlite3.connect(settings.database) as db:
        db.execute('BEGIN IMMEDIATE')
        security.approve(db, action, operation)
        del operation[field]
        with pytest.raises(security.SecurityViolation):
            simulate_send(db, action, operation)
        assert db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 0


@pytest.mark.parametrize('state', ['pending', 'rejected', 'executed', 'blocked', 'failed', 'expired', 'unknown', 'missing'])
def test_writer_requires_live_unused_approval(chat, state):
    client, _, settings = chat
    _, action = proposal(client)
    operation = details(settings.database, action)
    with sqlite3.connect(settings.database) as db:
        db.execute('BEGIN IMMEDIATE')
        if state == 'expired':
            db.execute("UPDATE secure_actions SET state='approved', expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (action,))
        elif state not in {'unknown', 'missing'}:
            db.execute('UPDATE secure_actions SET state=? WHERE id=?', (state, action))
        with pytest.raises(security.SecurityViolation):
            simulate_send(db, None if state == 'missing' else '0' * 32 if state == 'unknown' else action, operation)
        assert db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 0


def test_document_change_after_approval_is_blocked(chat):
    client, _, settings = chat
    _, action = proposal(client)
    operation = details(settings.database, action)
    with sqlite3.connect(settings.database) as db:
        db.execute('BEGIN IMMEDIATE')
        security.approve(db, action, operation)
        original = security.read_document(operation['document_id'])
        with patch('app.security.read_document', return_value={**original, 'content': 'Substituted content'}):
            with pytest.raises(security.SecurityViolation):
                simulate_send(db, action, operation)
        assert db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 0


def test_browser_cannot_override_action_or_authorization(chat):
    client, _, _ = chat
    conversation, action = proposal(client)
    for extra in [{'recipient': 'Mallory'}, {'fingerprint': 'anything'}, {'approved': True}, {'action': {}}]:
        assert approve(client, conversation, action, **extra).status_code == 422
    assert client.get('/health').json()['outbox_count'] == 0


def test_stored_candidate_change_blocked_and_audited(chat):
    client, _, settings = chat
    conversation, action = proposal(client)
    operation = details(settings.database, action)
    with sqlite3.connect(settings.database) as db:
        db.execute('UPDATE proposals SET details=? WHERE id=?', (json.dumps({**operation, 'recipient': 'Mallory'}), action))
    # Card is derived from the independent trusted record, not the changed proposal.
    card = client.get(f'/api/conversations/{conversation}').json()['messages'][1]['proposals'][0]
    assert card['recipient'] == 'Alice'
    assert approve(client, conversation, action).status_code == 403
    data = client.get('/api/dashboard').json()
    assert data['events'][0]['event'] == 'blocked'
    assert data['outbox'] == []


def test_fingerprint_mismatch_fails_closed(chat):
    client, _, settings = chat
    conversation, action = proposal(client)
    with sqlite3.connect(settings.database) as db:
        db.execute("UPDATE secure_actions SET fingerprint=? WHERE id=?", ('0' * 64, action))
    assert approve(client, conversation, action).status_code == 403
    assert client.get('/api/dashboard').json()['actions'][0]['fingerprint_check'] == 'mismatch'
    assert client.get('/health').json()['outbox_count'] == 0


def test_exact_message_fields_are_executed_and_recorded(chat):
    client, _, settings = chat
    conversation = client.post('/api/conversations').json()['conversation_id']
    Conversations(settings.database).append(conversation, 'assistant', 'Misleading model title', proposals=[{
        'document_id': 'quarterly-report-q3', 'title': 'Fake title', 'recipient': 'Alice',
        'cc': ['Bob'], 'bcc': ['Carol'], 'subject': 'Report review', 'body': 'Hello\nPlease review.',
        'fingerprint': 'model-claim', 'scope': 'model-claim'}])
    card = client.get(f'/api/conversations/{conversation}').json()['messages'][0]['proposals'][0]
    assert card['title'] != 'Fake title'
    assert card['scope'] == 'local_simulated_outbox'
    assert approve(client, conversation, card['id']).status_code == 200
    data = client.get('/api/dashboard').json()
    outbox = data['outbox'][0]
    assert (outbox['subject'], outbox['body']) == ('Report review', 'Hello\nPlease review.')
    assert json.loads(outbox['cc']) == ['Bob'] and json.loads(outbox['bcc']) == ['Carol']
    assert data['actions'][0]['state'] == 'executed'
    assert data['actions'][0]['verification'] == 'permitted'
    assert data['actions'][0]['fingerprint_check'] == 'matched'


def test_concurrent_gate_consumes_approval_once(chat):
    client, _, settings = chat
    _, action = proposal(client)
    operation = details(settings.database, action)
    with sqlite3.connect(settings.database) as db:
        security.approve(db, action, operation)
    ready = Barrier(2)
    def execute():
        ready.wait(timeout=5)
        with sqlite3.connect(settings.database) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                simulate_send(db, action, operation)
                return 'sent'
            except security.SecurityViolation:
                return 'blocked'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: execute(), range(2))) == ['blocked', 'sent']
    assert client.get('/health').json()['outbox_count'] == 1


def test_no_tool_or_alternate_endpoint_executes(chat):
    client, _, _ = chat
    _, action = proposal(client)
    runner = ToolRunner()
    for name in ['send_document', 'send_email', 'simulate_send', 'approve']:
        assert 'error' in runner.execute(name, {'proposal_id': action})
    for route in ['/api/execute', f'/api/execute/{action}', '/api/outbox', '/api/dashboard']:
        assert client.post(route, json={'proposal_id': action}).status_code in {404, 405}
    assert client.get('/health').json()['outbox_count'] == 0


def test_dashboard_empty_pages_and_database_error(tmp_path):
    with TestClient(create_app(Settings(database=tmp_path / 'empty.db'))) as client:
        assert client.get('/api/dashboard').json() == {'actions': [], 'legacy': [], 'events': [], 'outbox': []}
        for page in ['/', '/dashboard']:
            response = client.get(page)
            assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
        for asset in ['dashboard.js', 'dashboard.css']:
            assert client.get(f'/static/{asset}').status_code == 200
        with patch('app.main.dashboard', side_effect=sqlite3.OperationalError('private detail')):
            response = client.get('/api/dashboard')
            assert response.status_code == 503 and 'private detail' not in response.text


@pytest.mark.parametrize('decision', ['approve', 'reject'])
def test_dashboard_and_chat_share_decisions(chat, decision):
    client, _, _ = chat
    conversation, action = proposal(client)
    before = client.get('/api/dashboard').json()
    assert before['actions'][0]['state'] == 'pending'
    assert before['actions'][0]['verification'] == 'not_checked'
    client.post('/api/decisions', json={'conversation_id': conversation, 'proposal_id': action, 'decision': decision})
    data = client.get('/api/dashboard').json()
    assert data['actions'][0]['state'] == ('executed' if decision == 'approve' else 'rejected')
    card = client.get(f'/api/conversations/{conversation}').json()['messages'][1]['proposals'][0]
    assert card['sent'] is (decision == 'approve')
    assert len(data['outbox']) == (1 if decision == 'approve' else 0)


def test_approved_not_executed_is_not_reported_as_sent(chat):
    client, _, settings = chat
    conversation, action = proposal(client)
    with sqlite3.connect(settings.database) as db:
        security.approve(db, action, details(settings.database, action))
    assert client.get('/api/dashboard').json()['actions'][0]['state'] == 'approved'
    card = client.get(f'/api/conversations/{conversation}').json()['messages'][1]['proposals'][0]
    assert card['status'] == 'approved' and card['sent'] is False
    assert card['execution_status'] == 'not_executed'


def test_legacy_data_preserved_but_not_authorized(chat):
    client, _, settings = chat
    conversation, action = proposal(client)
    with sqlite3.connect(settings.database) as db:
        db.execute('DELETE FROM secure_actions WHERE id=?', (action,))  # Simulate a pre-migration proposal.
        db.execute("INSERT INTO outbox(document_id,recipient) VALUES ('old','Legacy')")
        before = db.execute('SELECT * FROM proposals').fetchall()
    initialize_database(settings.database)
    initialize_database(settings.database)
    with sqlite3.connect(settings.database) as db:
        assert db.execute('SELECT * FROM proposals').fetchall() == before
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert approve(client, conversation, action).status_code == 403
    data = client.get('/api/dashboard').json()
    assert data['legacy'][0]['id'] == action and len(data['outbox']) == 1


def test_canonical_serialization_independent_of_dictionary_order(chat):
    client, _, settings = chat
    _, action = proposal(client)
    values = details(settings.database, action)
    fields = {key: values[key] for key in security.Action.model_fields}
    first = security.Action.model_validate(fields)
    second = security.Action.model_validate(dict(reversed(list(fields.items()))))
    assert security.fingerprint(first) == security.fingerprint(second)


def test_expired_action_blocked_in_api_and_visible_on_dashboard(chat):
    client, _, settings = chat
    conversation, action = proposal(client)
    with sqlite3.connect(settings.database) as db:
        db.execute("UPDATE secure_actions SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (action,))
    assert approve(client, conversation, action).status_code == 403
    data = client.get('/api/dashboard').json()
    assert data['actions'][0]['expired'] is True
    assert data['actions'][0]['last_attempt'] == 'blocked'
    assert data['outbox'] == []


def test_failed_execution_has_no_permitted_outcome(chat):
    client, _, settings = chat
    conversation, action = proposal(client)
    def fail_after_write(db, action_id, operation):
        simulate_send(db, action_id, operation)
        raise RuntimeError('private details')
    with patch('app.conversations.simulate_send', side_effect=fail_after_write):
        assert approve(client, conversation, action).status_code == 503
    data = client.get('/api/dashboard').json()
    assert data['actions'][0]['state'] == 'failed'
    assert data['actions'][0]['verification'] == 'not_completed'
    assert data['actions'][0]['outbox_id'] is None
    assert data['outbox'] == []
    assert not any(e['event'] in {'executed', 'execution_verified'} for e in data['events'])
    assert 'private details' not in json.dumps(data)
