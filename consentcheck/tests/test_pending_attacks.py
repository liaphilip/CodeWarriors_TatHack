import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4
import pytest
from app import attack_demo, security
from app.conversations import Conversations
from test_conversations import chat


def pending(settings):
    return attack_demo.new_action(Conversations(settings.database))


def endpoint(c, p, s='recipient-tampering'):
    return f'/api/conversations/{c}/proposals/{p}/attacks/{s}'


@pytest.mark.parametrize('scenario', attack_demo.SCENARIOS)
def test_live_attack_and_legitimate_control(chat, scenario):
    client, _, settings = chat
    c, p = pending(settings)
    token = uuid4().hex
    with sqlite3.connect(settings.database) as db:
        original = security.load(db, p)
    with patch('app.security.gate', wraps=security.gate) as gate:
        response = client.post(endpoint(c, p, scenario), json={'request_id': token})
    assert response.status_code == 200 and gate.call_count == 1
    data = response.json()
    assert data['messages'][-1]['attack_result'] == data['attack_result']
    assert data['messages'][0]['proposals'][0]['status'] == 'blocked'
    for phrase in ['PREVENTED', 'alice@example.test', 'bob@example.test', 'Actual security gate: rejected',
                   'Execution occurred: No', 'Unauthorized production outbox write: No']:
        assert phrase in data['messages'][-1]['text']
    assert client.get(f'/api/conversations/{c}').json() == {k: v for k, v in data.items() if k != 'attack_result'}
    assert client.post(endpoint(c, p, scenario), json={'request_id': token}).json() == data
    assert client.post(endpoint(c, p, scenario), json={'request_id': uuid4().hex}).status_code == 409
    assert client.post('/api/decisions', json={'conversation_id': c, 'proposal_id': p, 'decision': 'approve'}).status_code == 409
    with sqlite3.connect(settings.database) as db:
        record = security.load(db, p)
        assert record['state'] == 'blocked' and record['decision'] == 'reject'
        assert all(record[k] == original[k] for k in ('descriptor', 'fingerprint'))
        assert db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM pending_attack_runs').fetchone()[0] == 1
        evidence = json.loads(db.execute('SELECT evidence FROM pending_attack_runs').fetchone()[0])
        assert evidence['mismatch_verified'] and evidence['trusted_action_unchanged']
        if scenario == 'misleading-approval':
            assert evidence['reviewed_action']['body'] != evidence['attempted_action']['body']
    c2, p2 = pending(settings)
    assert client.post('/api/decisions', json={'conversation_id': c2, 'proposal_id': p2, 'decision': 'approve'}).json()['sent']
    with sqlite3.connect(settings.database) as db:
        assert db.execute('SELECT recipient FROM outbox').fetchall() == [('alice@example.test',)]


@pytest.mark.parametrize('mutation', ['expired', 'terminal', 'wrong-owner', 'fingerprint', 'ineligible'])
def test_invalid_pending_attack(chat, mutation):
    client, _, settings = chat
    c, p = pending(settings)
    with sqlite3.connect(settings.database) as db:
        if mutation == 'expired':
            db.execute("UPDATE secure_actions SET expires_at='2000-01-01T00:00:00+00:00'")
        elif mutation == 'terminal':
            db.execute("UPDATE secure_actions SET state='rejected'")
        elif mutation == 'fingerprint':
            db.execute('UPDATE secure_actions SET fingerprint=?', ('0' * 64,))
        elif mutation == 'ineligible':
            fields = json.loads(security.load(db, p)['descriptor'])
            fields['recipient'] = 'outside@example.com'
            changed = security.Action.model_validate(fields)
            db.execute('UPDATE secure_actions SET descriptor=?,fingerprint=?', (security.canonical(changed), security.fingerprint(changed)))
            db.execute('UPDATE proposals SET details=?', (json.dumps(fields),))
    if mutation == 'wrong-owner':
        c = Conversations(settings.database).create()
    assert client.post(endpoint(c, p), json={'request_id': uuid4().hex}).status_code in (404, 409)
    with sqlite3.connect(settings.database) as db:
        assert db.execute('SELECT COUNT(*) FROM pending_attack_runs').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 0


def test_validation_and_token_binding(chat):
    client, _, settings = chat
    c, p = pending(settings)
    token = uuid4().hex
    assert client.post(endpoint(c, p, 'unknown'), json={'request_id': token}).status_code == 404
    assert client.post(endpoint('invalid', p), json={'request_id': token}).status_code == 422
    assert client.post(endpoint(c, p), json={'request_id': token, 'action': {}}).status_code == 422
    assert client.post(endpoint(c, p), json={'request_id': token}).status_code == 200
    c2, p2 = pending(settings)
    assert client.post(endpoint(c2, p2), json={'request_id': token}).status_code == 409


def test_duplicate_race(chat):
    _, _, settings = chat
    c, p = pending(settings)
    token = uuid4().hex
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attack_demo.run_for_pending(settings.database, c, p, 'recipient-tampering', token), range(2)))
    assert results[0] == results[1]
    assert len(Conversations(settings.database).get(c)['messages']) == 2


@pytest.mark.parametrize('scenario', attack_demo.SCENARIOS)
def test_approve_attack_race(chat, scenario):
    _, _, settings = chat
    c, p = pending(settings)
    barrier = Barrier(2)
    def run(attack):
        barrier.wait(timeout=5)
        try:
            if attack:
                attack_demo.run_for_pending(settings.database, c, p, scenario, uuid4().hex)
            else:
                Conversations(settings.database).decide(c, p, 'approve')
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, [True, False])) == [False, True]
    with sqlite3.connect(settings.database) as db:
        state = security.load(db, p)['state']
        rows = db.execute('SELECT recipient FROM outbox').fetchall()
        assert (state, rows) in [('executed', [('alice@example.test',)]), ('blocked', [])]


@pytest.mark.parametrize('failure', ['accept', 'error', 'wrong-rejection', 'partial-write'])
def test_honest_regression_evidence(chat, failure):
    client, _, settings = chat
    c, p = pending(settings)
    def broken(db, *_):
        if failure == 'error':
            raise RuntimeError('private detail')
        if failure == 'wrong-rejection':
            raise security.SecurityViolation('Other condition')
        if failure == 'partial-write':
            db.execute("INSERT INTO outbox(document_id,recipient) VALUES ('synthetic','bob@example.test')")
            raise security.ActionMismatch('Mismatch after write')
    with patch('app.security.gate', side_effect=broken):
        response = client.post(endpoint(c, p), json={'request_id': uuid4().hex})
    text = response.json()['messages'][-1]['text']
    assert ('FAILED' if failure in ('accept', 'partial-write') else 'INCOMPLETE') in text
    assert 'PREVENTED' not in text and 'private detail' not in text
    with sqlite3.connect(settings.database) as db:
        assert db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 0
        assert security.load(db, p)['state'] == 'blocked'

@pytest.mark.parametrize('scenario', attack_demo.SCENARIOS)
def test_observed_alice_bug_and_dashboard_refresh(chat, scenario):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from test_conversations import proposal
    client, _, settings = chat
    c, p = proposal(client)  # Existing agent fixture uses exactly 'Alice'.
    card = client.get(f'/api/conversations/{c}').json()['messages'][-1]['proposals'][0]
    assert card['recipient'] == 'Alice' and card['attack_eligible']
    with sqlite3.connect(settings.database) as db:
        before = db.execute('SELECT * FROM outbox').fetchall()
        assert security.load(db, p)['state'] == 'pending'
    # The old footer URL must not silently run an unrelated isolated demo.
    old = client.post(f'/api/conversations/{c}/security/attacks/{scenario}', json={'request_id': uuid4().hex})
    assert old.status_code == 409 and 'specific proposal' in old.text
    response = client.post(endpoint(c, p, scenario), json={'request_id': uuid4().hex})
    assert response.status_code == 200
    data = response.json()
    assert data['attack_result']['gate_rejected'] and data['attack_result']['mismatch_verified']
    assert data['attack_result']['proposal_id'] == p
    assert data['attack_result']['outcome'] == 'PREVENTED'
    assert 'ATTACK BLOCKED — approval rejected by security gate' in data['messages'][-1]['text']
    assert not any('Done!' in m['text'] for m in data['messages'])
    with sqlite3.connect(settings.database) as db:
        assert db.execute('SELECT * FROM outbox').fetchall() == before
        assert security.load(db, p)['state'] == 'blocked'
    for _ in range(2):
        assert client.post('/api/decisions', json={'conversation_id': c, 'proposal_id': p, 'decision': 'approve'}).status_code == 409
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get('/api/security-summary').json()['metrics'] == dict(reviewed=1, executed=0, blocked=1, pending=0)
        assert restarted.get('/api/dashboard').json()['actions'][0]['state'] == 'blocked'
        assert 'ATTACK BLOCKED' in restarted.get(f'/api/conversations/{c}').json()['messages'][-1]['text']
    c2, p2 = proposal(client)
    payload = {'conversation_id': c2, 'proposal_id': p2, 'decision': 'approve'}
    assert client.post('/api/decisions', json=payload).json()['sent']
    assert client.post('/api/decisions', json=payload).status_code == 409
    with sqlite3.connect(settings.database) as db:
        assert db.execute('SELECT proposal_id,recipient FROM outbox').fetchall() == [(p2, 'Alice')]


def test_two_blocks_count_two_not_retries_or_isolated_demos(chat):
    client, _, settings = chat
    for index, scenario in enumerate(attack_demo.SCENARIOS, 1):
        c, p = pending(settings)
        payload = {'request_id': uuid4().hex}
        for _ in range(2):
            assert client.post(endpoint(c, p, scenario), json=payload).status_code == 200
        assert client.get('/api/security-summary').json()['metrics']['blocked'] == index
    assert len(client.get('/api/dashboard').json()['outbox']) == 0


def test_attack_cannot_reclassify_already_executed_action(chat):
    client, _, settings = chat
    c, p = pending(settings)
    client.post('/api/decisions', json={'conversation_id': c, 'proposal_id': p, 'decision': 'approve'})
    assert client.post(endpoint(c, p), json={'request_id': uuid4().hex}).status_code == 409
    assert client.get('/api/security-summary').json()['metrics']['blocked'] == 0
    with sqlite3.connect(settings.database) as db:
        assert security.load(db, p)['state'] == 'executed'
        assert db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM pending_attack_runs').fetchone()[0] == 0
