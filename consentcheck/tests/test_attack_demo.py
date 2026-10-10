import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

import pytest

from app import attack_demo, security
from app.demo_baseline import evaluate
from app.execution import simulate_send
from test_conversations import chat, proposal


def submit(client, scenario, token=None):
    return client.post(f'/api/security/attacks/{scenario}', json={'request_id': token or uuid4().hex})


@pytest.mark.parametrize('scenario', attack_demo.SCENARIOS)
def test_real_gate_prevents_attack_and_control_succeeds_without_production_writes(chat, scenario):
    client, _, settings = chat
    metrics = client.get('/api/security-summary').json()
    with patch('app.execution.gate', wraps=security.gate) as gate:
        response = submit(client, scenario)
    assert response.status_code == 200
    result = response.json()
    assert gate.call_count == 2  # Malicious candidate and legitimate control, both actual gate.
    assert result['outcome'] == 'PREVENTED' and result['prevented'] is True
    assert result['vulnerable']['accepted'] is True
    assert result['vulnerable']['simulated_only'] is True
    assert result['approved_action']['recipient'] == 'alice@example.test'
    assert result['attempted_action']['recipient'] == 'bob@example.test'
    assert result['protected']['approved_action_unchanged'] is True
    assert result['protected']['outbox_before'] == result['protected']['outbox_after_attempt'] == 0
    assert result['protected']['mismatch_verified'] is True
    assert result['unauthorized_execution'] is False
    assert result['legitimate_control']['executed'] is True
    if scenario == 'misleading-approval':
        assert result['displayed_action']['body'] != result['attempted_action']['body']
    assert client.get('/api/security-summary').json() == metrics
    with sqlite3.connect(settings.database) as db:
        assert db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM secure_actions').fetchone()[0] == 0
    # Normal app conversation works after a demo as well.
    conversation, action = proposal(client)
    assert client.post('/api/decisions', json={'conversation_id': conversation, 'proposal_id': action, 'decision': 'approve'}).json()['sent'] is True


def test_baseline_is_pure_and_never_calls_production_executor():
    with patch('app.execution.simulate_send') as executor:
        result = evaluate({'recipient': 'alice@example.test'}, {'recipient': 'bob@example.test', 'body': 'Synthetic'}, True)
    executor.assert_not_called()
    assert result['accepted'] is True
    with pytest.raises(ValueError):
        evaluate({'recipient': 'real@example.com'}, {'recipient': 'bob@example.test'}, True)


def test_results_persist_refresh_never_runs_scenarios_and_retries_are_idempotent(chat):
    client, _, settings = chat
    assert all(row['attacks'] == 0 and row['latest'] is None for row in client.get('/api/security/attacks').json()['scenarios'])
    token = uuid4().hex
    first = submit(client, 'recipient-tampering', token).json()
    assert submit(client, 'recipient-tampering', token).json() == first
    with patch('app.attack_demo.scenario_evidence', side_effect=AssertionError('Must not run on refresh')):
        one = client.get('/api/security/attacks').json()
        assert client.get('/api/security/attacks').json() == one
        assert attack_demo.results(settings.database) == one
    assert one['scenarios'][0]['attacks'] == one['scenarios'][0]['prevented'] == 1
    assert one['scenarios'][0]['latest'] == first
    assert submit(client, 'misleading-approval', token).status_code == 409


def test_concurrent_duplicate_request_runs_once(chat):
    _, _, settings = chat
    token = uuid4().hex
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda _: attack_demo.run(settings.database, 'recipient-tampering', token), range(2)))
    assert rows[0]['run_id'] == rows[1]['run_id']
    assert attack_demo.results(settings.database)['scenarios'][0]['attacks'] == 1


@pytest.mark.parametrize('error', [RuntimeError('private detail'), security.SecurityViolation('wrong state')])
def test_unrelated_errors_never_claim_prevention(chat, error):
    client, _, _ = chat
    with patch('app.attack_demo.simulate_send', side_effect=error):
        result = submit(client, 'recipient-tampering').json()
    assert result['outcome'] == 'INCOMPLETE' and result['prevented'] is False
    assert 'private detail' not in str(result)
    assert client.get('/api/security/attacks').json()['scenarios'][0]['prevented'] == 0


def test_gate_acceptance_is_failed_even_if_no_write(chat):
    client, _, _ = chat
    with patch('app.attack_demo.simulate_send', return_value={'sent': True}):
        result = submit(client, 'recipient-tampering').json()
    assert result['outcome'] == 'FAILED' and result['prevented'] is False


def test_partial_unauthorized_write_cannot_be_hidden_by_exception_or_rollback(chat):
    client, _, settings = chat
    def broken(db, action, details):
        db.execute("INSERT INTO outbox(document_id,recipient) VALUES ('synthetic','bob@example.test')")
        raise security.ActionMismatch('Mismatch after write')
    with patch('app.attack_demo.simulate_send', side_effect=broken):
        result = submit(client, 'misleading-approval').json()
    assert result['outcome'] == 'FAILED' and result['unauthorized_execution'] is True
    assert result['prevented'] is False
    with sqlite3.connect(settings.database) as db:
        assert db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 0


def test_failed_legitimate_control_makes_prevention_incomplete(chat):
    client, _, _ = chat
    with patch('app.attack_demo.Conversations.decide', side_effect=RuntimeError()):
        result = submit(client, 'recipient-tampering').json()
    assert result['outcome'] == 'INCOMPLETE' and result['prevented'] is False


def test_interrupted_run_is_explicit_and_custom_targets_rejected(chat):
    client, _, settings = chat
    token = uuid4().hex
    with sqlite3.connect(settings.database) as db:
        db.execute("INSERT INTO attack_runs(id,request_id,scenario,created_at,outcome) VALUES (?,?,? ,?,'INCOMPLETE')",
                   (uuid4().hex, token, 'recipient-tampering', security.now().isoformat()))
    result = submit(client, 'recipient-tampering', token).json()
    assert result['outcome'] == 'INCOMPLETE' and result['unauthorized_execution'] is None
    assert client.post('/api/security/attacks/recipient-tampering', json={'request_id': uuid4().hex, 'recipient': 'outside@example.com'}).status_code == 422
    assert submit(client, 'third-scenario').status_code == 404
    with patch('app.main.attack_demo.results', side_effect=sqlite3.OperationalError('secret')):
        response = client.get('/api/security/attacks')
    assert response.status_code == 503 and 'secret' not in response.text


@pytest.mark.parametrize('scenario', attack_demo.SCENARIOS)
def test_chat_demo_persistence_isolation_and_retry(chat, scenario):
    client, providers, settings = chat
    identifier = client.post('/api/conversations').json()['conversation_id']
    endpoint = f'/api/conversations/{identifier}/security/attacks/{scenario}'
    token = uuid4().hex
    with patch('app.execution.gate', wraps=security.gate) as gate:
        first = client.post(endpoint, json={'request_id': token})
    assert first.status_code == 200 and gate.call_count == 2
    messages = first.json()['messages']
    assert len(messages) == 1
    for expected in ['PREVENTED', 'alice@example.test', 'bob@example.test', 'Real security gate: rejected', 'Unauthorized production write: No']:
        assert expected in messages[0]['text']
    assert client.post(endpoint, json={'request_id': token}).json() == first.json()
    assert client.get(f'/api/conversations/{identifier}').json() == first.json()
    assert providers == []
    with sqlite3.connect(settings.database) as db:
        assert db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM chat_attack_runs WHERE message_id IS NOT NULL').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM attack_runs').fetchone()[0] == 1
    other = client.post('/api/conversations').json()['conversation_id']
    assert client.get(f'/api/conversations/{other}').json()['messages'] == []
    assert client.post(endpoint.replace(identifier, other), json={'request_id': token}).status_code == 409
    assert client.post(endpoint.replace(identifier, uuid4().hex), json={'request_id': uuid4().hex}).status_code == 404
    assert client.post(endpoint.replace(scenario, 'unknown'), json={'request_id': uuid4().hex}).status_code == 404
    assert client.post(endpoint, json={'request_id': uuid4().hex, 'recipient': 'bob@example.test'}).status_code == 422
    assert sum(row['attacks'] for row in client.get('/api/security/attacks').json()['scenarios']) == 1
    conversation, action = proposal(client)
    assert client.post('/api/decisions', json={'conversation_id': conversation, 'proposal_id': action, 'decision': 'approve'}).json()['sent'] is True


@pytest.mark.parametrize('outcome', ['FAILED', 'INCOMPLETE'])
def test_chat_demo_honest_failed_evidence(chat, outcome):
    client, _, _ = chat
    identifier = client.post('/api/conversations').json()['conversation_id']
    with patch('app.attack_demo.scenario_evidence', return_value={'outcome': outcome, 'prevented': False}):
        response = client.post(f'/api/conversations/{identifier}/security/attacks/recipient-tampering', json={'request_id': uuid4().hex})
    text = response.json()['messages'][0]['text']
    assert outcome in text and 'PREVENTED' not in text
    assert 'outcome unverified' in text
