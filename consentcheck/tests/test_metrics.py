import sqlite3
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app import security
from test_conversations import chat, proposal
from test_security import approve, details


def counts(client):
    response = client.get('/api/security-summary')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['engine_status'] == 'active'
    return response.json()['metrics']


def test_metrics_track_real_decisions_execution_and_deduplicate_blocks(chat):
    client, _, settings = chat
    assert counts(client) == dict(reviewed=0, executed=0, blocked=0, pending=0)
    conversation, action = proposal(client)
    assert counts(client)['pending'] == 1
    assert approve(client, conversation, action).status_code == 200
    assert counts(client) == dict(reviewed=1, executed=1, blocked=0, pending=0)
    # Repeated blocked requests are one distinct action, not multiple actions.
    assert approve(client, conversation, action).status_code == 409
    assert approve(client, conversation, action).status_code == 409
    assert counts(client) == dict(reviewed=1, executed=1, blocked=1, pending=0)
    other, rejected = proposal(client)
    client.post('/api/decisions', json=dict(conversation_id=other, proposal_id=rejected, decision='reject'))
    assert counts(client) == dict(reviewed=2, executed=1, blocked=1, pending=0)
    with TestClient(create_app(settings)) as restarted:
        assert counts(restarted) == counts(client)


def test_approval_alone_expired_and_legacy_records_do_not_inflate_metrics(chat):
    client, _, settings = chat
    _, approved = proposal(client)
    _, expired = proposal(client)
    _, legacy = proposal(client)
    with sqlite3.connect(settings.database) as db:
        security.approve(db, approved, details(settings.database, approved))
        db.execute("UPDATE secure_actions SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (expired,))
        db.execute('DELETE FROM secure_actions WHERE id=?', (legacy,))
        security.event(db, legacy, 'blocked', 'Legacy attempt')
        security.event(db, 'unknown', 'blocked', 'Unknown action')
    assert counts(client) == dict(reviewed=1, executed=0, blocked=0, pending=0)


def test_metric_counts_are_not_limited_to_dashboard_window(chat):
    client, _, settings = chat
    _, action = proposal(client)
    with sqlite3.connect(settings.database) as db:
        row = db.execute('SELECT * FROM secure_actions WHERE id=?', (action,)).fetchone()
        for index in range(205):
            db.execute('INSERT INTO secure_actions VALUES (' + ','.join('?' for _ in row) + ')', (f'{index:032x}', *row[1:]))
    assert counts(client)['pending'] == 206


def test_status_unavailable_on_database_error(chat):
    client, _, _ = chat
    with patch('app.metrics.sqlite3.connect', side_effect=sqlite3.OperationalError('private detail')):
        response = client.get('/api/security-summary')
    assert response.status_code == 503
    assert 'private detail' not in response.text


def test_main_page_and_dashboard_share_dark_assets(chat):
    client, _, _ = chat
    page = client.get('/').text
    assert page.count('class="metric"') == 4
    assert 'PHANTOM LOOP SECURITY CENTER' in page
    assert '/static/security-center.js?v=' in page
    assert 'grid-template-columns:minmax(0,1fr) minmax(0,1fr)' in client.get('/static/style.css').text
    assert client.get('/dashboard').status_code == 200
