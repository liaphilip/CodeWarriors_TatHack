"""Two fixed synthetic scenarios. Production receives evidence only, never demo sends."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from . import security
from .conversations import Conversations, initialize_chats
from .database import initialize_database
from .demo_baseline import evaluate as baseline
from .execution import simulate_send

SCENARIOS = {'recipient-tampering': 'Recipient Tampering',
             'misleading-approval': 'Misleading Approval Display'}


def new_action(chats):
    conversation = chats.create()
    chats.append(conversation, 'assistant', 'Synthetic security demonstration, not a Gemini conversation.', proposals=[{
        'document_id': 'quarterly-report-q3', 'recipient': 'alice@example.test',
        'subject': 'Synthetic report review', 'body': 'Please review this synthetic report.'}])
    card = chats.get(conversation)['messages'][0]['proposals'][0]
    return conversation, card['id']


def scenario_evidence(scenario):
    # A separate database enforces isolation even if the real gate regresses.
    with TemporaryDirectory(prefix='phantom-demo-', ignore_cleanup_errors=True) as directory:
        path = Path(directory) / 'synthetic.db'
        initialize_database(path)
        initialize_chats(path)
        chats = Conversations(path)
        _, action_id = new_action(chats)
        with closing(sqlite3.connect(path)) as db:
            approved = json.loads(db.execute('SELECT details FROM proposals WHERE id=?', (action_id,)).fetchone()[0])
            with db:
                db.execute('BEGIN IMMEDIATE')
                security.approve(db, action_id, approved)
            original_record = security.load(db, action_id)
            attempted = {**approved, 'recipient': 'bob@example.test'}
            if scenario == 'misleading-approval':
                attempted['body'] = 'Send the full synthetic report to Bob instead of the reviewed message.'
            displayed = {**approved}
            vulnerable = baseline(displayed, attempted, True)
            before = db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]
            rejected, mismatch, accepted = False, False, False
            reason = 'Execution evidence unavailable.'
            try:
                db.execute('BEGIN IMMEDIATE')
                simulate_send(db, action_id, attempted)
                accepted, reason = True, 'The gate accepted the mismatched action.'
            except security.ActionMismatch as exc:
                rejected, mismatch, reason = True, True, str(exc)
            except security.SecurityViolation:
                rejected, reason = True, 'Rejected for another security condition; mismatch prevention is unverified.'
            except Exception:
                reason = 'Unexpected execution error; prevention is unverified.'
            # Inspect writes before rollback: rollback alone must never count as prevention.
            after = db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]
            unchanged = security.load(db, action_id)['descriptor'] == original_record['descriptor']
            db.rollback()
            unauthorized = after > before

        control_conversation, control_action = new_action(chats)
        control_ok = False
        try:
            result, _ = chats.decide(control_conversation, control_action, 'approve')
            with closing(sqlite3.connect(path)) as db:
                row = db.execute('SELECT recipient,body,status FROM outbox WHERE proposal_id=?', (control_action,)).fetchone()
                control_ok = (result['execution_status'] == 'completed' and
                              row == ('alice@example.test', approved['body'], 'simulated_sent'))
        except Exception:
            pass
        prevented = rejected and mismatch and not unauthorized and unchanged and control_ok and vulnerable['accepted']
        outcome = 'FAILED' if accepted or unauthorized else 'PREVENTED' if prevented else 'INCOMPLETE'
        return {'synthetic': True, 'isolation': 'Temporary SQLite database; no production outbox access; no Gemini calls.',
                'approved_action': approved, 'displayed_action': displayed, 'attempted_action': attempted,
                'vulnerable': vulnerable,
                'protected': {'rejected': rejected, 'mismatch_verified': mismatch, 'reason': reason,
                              'outbox_before': before, 'outbox_after_attempt': after,
                              'approved_action_unchanged': unchanged},
                'legitimate_control': {'executed': control_ok, 'recipient': 'alice@example.test'},
                'unauthorized_execution': unauthorized, 'prevented': prevented, 'outcome': outcome,
                'explanation': 'Mismatch blocked, no unauthorized write, and legitimate control executed.' if prevented
                               else 'Protection test failed.' if outcome == 'FAILED' else 'Evidence incomplete; prevention not claimed.'}


def unpack(row):
    identifier, scenario, created, completed, outcome, evidence = row
    return {**(json.loads(evidence) if evidence else {'prevented': False, 'unauthorized_execution': None,
             'explanation': 'Run is in progress or was interrupted; no prevention result recorded.'}),
            'run_id': identifier, 'scenario': scenario, 'name': SCENARIOS[scenario],
            'created_at': created, 'completed_at': completed, 'outcome': outcome}


def run(path, scenario, request_id):
    if scenario not in SCENARIOS:
        raise LookupError('Unknown scenario')
    with closing(sqlite3.connect(path)) as db:
        with db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT id,scenario,created_at,completed_at,outcome,evidence FROM attack_runs WHERE request_id=?', (request_id,)).fetchone()
            if existing:
                if existing[1] != scenario:
                    raise ValueError('Request ID already belongs to another scenario.')
                return unpack(existing)
            identifier = uuid4().hex
            db.execute("INSERT INTO attack_runs(id,request_id,scenario,created_at,outcome) VALUES (?,?,?,?,'INCOMPLETE')",
                       (identifier, request_id, scenario, security.now().isoformat()))
        try:
            evidence = scenario_evidence(scenario)
        except Exception:
            evidence = {'outcome': 'INCOMPLETE', 'prevented': False, 'unauthorized_execution': None,
                        'explanation': 'The synthetic scenario could not complete. No prevention claim is available.'}
        with db:
            db.execute('UPDATE attack_runs SET completed_at=?,outcome=?,evidence=? WHERE id=?',
                       (security.now().isoformat(), evidence['outcome'], json.dumps(evidence), identifier))
        return unpack(db.execute('SELECT id,scenario,created_at,completed_at,outcome,evidence FROM attack_runs WHERE id=?', (identifier,)).fetchone())


def results(path):
    with closing(sqlite3.connect(path)) as db:
        db.execute('BEGIN')
        scenarios = []
        for scenario, name in SCENARIOS.items():
            counts = db.execute("SELECT COUNT(*),COUNT(CASE WHEN outcome='PREVENTED' THEN 1 END) FROM attack_runs WHERE scenario=?", (scenario,)).fetchone()
            latest = db.execute('SELECT id,scenario,created_at,completed_at,outcome,evidence FROM attack_runs WHERE scenario=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (scenario,)).fetchone()
            scenarios.append({'scenario': scenario, 'name': name, 'attacks': counts[0], 'prevented': counts[1],
                              'latest': unpack(latest) if latest else None})
        return {'scenarios': scenarios}


def chat_result_text(result):
    """Backend evidence only; model prose never determines a security outcome."""
    protected = result.get('protected', {})
    verified = (result.get('outcome') == 'PREVENTED' and result.get('prevented') is True
                and protected.get('rejected') is True and protected.get('mismatch_verified') is True
                and protected.get('approved_action_unchanged') is True
                and result.get('unauthorized_execution') is False
                and result.get('legitimate_control', {}).get('executed') is True
                and result.get('vulnerable', {}).get('accepted') is True)
    status = 'PREVENTED' if verified else 'FAILED' if result['outcome'] == 'FAILED' else 'INCOMPLETE'
    lines = [f"Controlled synthetic demonstration — {result['name']}: {status}",
             'Not generated by Gemini. No live proposal was changed.', f"Run ID: {result['run_id']}"]
    for label, key in [('Reviewed / approved', 'displayed_action'), ('Attempted', 'attempted_action')]:
        action = result.get(key)
        lines.append(f"{label}: Recipient: {action['recipient']} | Subject: {action['subject']} | Body: {action['body']}"
                     if action else f'{label}: evidence unavailable')
    baseline = result.get('vulnerable', {}).get('accepted')
    lines.append('Vulnerable baseline: ' + ('accepted mismatch (comparison only; no send)' if baseline is True
                 else 'did not accept' if baseline is False else 'unverified'))
    lines.append('Real security gate: ' + ('rejected — ' if protected.get('rejected') else '')
                 + protected.get('reason', 'outcome unverified'))
    unauthorized = result.get('unauthorized_execution')
    lines.append('Unauthorized isolated outbox write: ' + ('No — verified' if unauthorized is False
                 else 'Yes' if unauthorized is True else 'unverified'))
    lines.append('Unauthorized production write: No — this runner uses a separate temporary database; only demo evidence is persisted here.')
    lines.append('Legitimate isolated control: ' + ('executed through the gate' if result.get('legitimate_control', {}).get('executed') else 'unverified'))
    return '\n\n'.join(lines)


def run_for_conversation(path, identifier, scenario, request_id):
    # Reserve ownership before executing. A retry cannot attach a run to another chat.
    from hashlib import sha256
    if scenario not in SCENARIOS:
        raise LookupError('Unknown scenario')
    with closing(sqlite3.connect(path)) as db:
        with db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM conversations WHERE id=?', (identifier,)).fetchone():
                raise LookupError('This conversation is no longer available. Start a new chat.')
            if db.execute("SELECT 1 FROM proposals WHERE conversation_id=? AND status='proposed'", (identifier,)).fetchone():
                raise ValueError('A pending approval exists. Use its attack controls to target that specific proposal; an isolated demo cannot block it.')
            existing = db.execute('SELECT conversation_id,scenario,message_id FROM chat_attack_runs WHERE request_id=?', (request_id,)).fetchone()
            if existing:
                if existing[:2] != (identifier, scenario):
                    raise ValueError('Request ID already belongs to another conversation or scenario.')
                if existing[2] is not None:
                    return
            else:
                db.execute('INSERT INTO chat_attack_runs(request_id,conversation_id,scenario) VALUES (?,?,?)',
                           (request_id, identifier, scenario))
        # Namespace tokens so dashboard retries cannot collide with chat requests.
        result = run(path, scenario, sha256(('chat:' + request_id).encode()).hexdigest())
        with db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT message_id FROM chat_attack_runs WHERE request_id=?', (request_id,)).fetchone()[0] is not None:
                return
            message_id = db.execute("INSERT INTO messages(conversation_id,role,text) VALUES (?, 'assistant', ?)",
                                    (identifier, chat_result_text(result))).lastrowid
            db.execute('UPDATE chat_attack_runs SET attack_run_id=?,message_id=? WHERE request_id=?',
                       (result['run_id'], message_id, request_id))


def run_for_pending(path, identifier, proposal_id, scenario, request_id):
    """Exercise the live gate without calling an executor; consume the pending card atomically."""
    if scenario not in SCENARIOS:
        raise LookupError('Unknown scenario')
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT conversation_id,proposal_id,scenario,evidence FROM pending_attack_runs WHERE request_id=?',
                              (request_id,)).fetchone()
        if existing:
            if existing[:3] != (identifier, proposal_id, scenario):
                raise ValueError('Request ID already belongs to another proposal or scenario.')
            return json.loads(existing[3])
        row = db.execute('SELECT details,status FROM proposals WHERE id=? AND conversation_id=?',
                         (proposal_id, identifier)).fetchone()
        if not row or not db.execute('SELECT 1 FROM conversations WHERE id=?', (identifier,)).fetchone():
            raise LookupError('That proposal is not part of this conversation.')
        record = security.load(db, proposal_id)
        security.require_live(record, 'pending')
        if row[1] != 'proposed' or record['conversation_id'] != identifier:
            raise ValueError('Proposal is not pending in this conversation.')
        action, _ = security.verify(record, json.loads(row[0]))
        if not security.demo_eligible(action):
            raise ValueError('This demonstration requires a synthetic Alice proposal without CC or BCC.')
        reviewed = action.model_dump()
        attempted = {**reviewed, 'recipient': 'bob@example.test'}
        if scenario == 'misleading-approval':
            attempted['body'] = 'Send the full synthetic report to Bob instead of the reviewed message.'
        vulnerable = baseline(reviewed, attempted, True)
        before = db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]
        rejected = mismatch = accepted = False
        reason = 'Execution evidence unavailable.'
        # The explicit demo click authorizes testing the reviewed action, never a send.
        # Roll back the temporary approval even if a regressed gate accepts the candidate.
        db.execute('SAVEPOINT pending_demo')
        try:
            security.approve(db, proposal_id, reviewed)
            security.gate(db, proposal_id, attempted)
            accepted, reason = True, 'The gate accepted the mismatch; no executor was called.'
        except security.ActionMismatch as exc:
            rejected, mismatch, reason = True, True, str(exc)
        except security.SecurityViolation as exc:
            rejected, reason = True, str(exc)
        except Exception:
            reason = 'Unexpected gate error; prevention is unverified.'
        after = db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]
        current = security.load(db, proposal_id)
        unchanged = all(current[key] == record[key] for key in ('descriptor', 'fingerprint'))
        execution_observed = current['outbox_id'] is not None or after != before
        db.execute('ROLLBACK TO pending_demo')
        db.execute('RELEASE pending_demo')
        outcome = 'FAILED' if accepted or execution_observed else 'PREVENTED' if mismatch and unchanged else 'INCOMPLETE'
        db.execute("UPDATE secure_actions SET state='blocked',decision='reject',decided_at=?,verification=? WHERE id=?",
                   (security.now().isoformat(), 'blocked' if rejected else 'not_completed', proposal_id))
        db.execute("UPDATE proposals SET status='blocked' WHERE id=?", (proposal_id,))
        security.event(db, proposal_id, 'blocked', f'Controlled {SCENARIOS[scenario]}: {reason}')
        run_id = uuid4().hex
        result = {'run_id': run_id, 'scenario': scenario, 'name': SCENARIOS[scenario],
                  'proposal_id': proposal_id, 'reviewed_action': reviewed, 'attempted_action': attempted,
                  'vulnerable': vulnerable, 'outcome': outcome, 'approval_status': 'blocked',
                  'gate_rejected': rejected, 'mismatch_verified': mismatch, 'reason': reason,
                  'trusted_action_unchanged': unchanged, 'execution_occurred': execution_observed,
                  'outbox_before': before, 'outbox_after_attempt': after,
                  'unauthorized_production_write': after != before,
                  'outbox_after_rollback': db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]}
        lines = [('ATTACK BLOCKED — approval rejected by security gate' if outcome == 'PREVENTED'
                  else f'ATTACK {outcome} — approval disabled; prevention not verified'),
                 f'Controlled synthetic demonstration — {SCENARIOS[scenario]}: {outcome}',
                 'User-triggered demonstration; not generated by Gemini.', f'Run ID: {run_id}',
                 f'Proposal ID: {proposal_id}']
        for label, details in [('Original / reviewed', reviewed), ('Attempted altered action', attempted)]:
            lines.append(f"{label}: Recipient: {details['recipient']} | Subject: {details['subject']} | Body: {details['body']}")
        lines.extend(['Vulnerable baseline: accepted mismatch (pure comparison only; no send).',
                      f"Actual security gate: {'rejected' if rejected else 'unverified / accepted'} — {reason}",
                      'Approval status: blocked (terminal).',
                      f"Execution occurred: {'Yes' if execution_observed else 'No — verified; executor never called'}.",
                      f"Unauthorized production outbox write: {'Yes — rolled back' if after != before else 'No — verified'}.",
                      f'Outbox evidence: before={before}, after attempt={after}, persisted={result["outbox_after_rollback"]}.',
                      f'Final result: {outcome}'])
        message_id = db.execute("INSERT INTO messages(conversation_id,role,text) VALUES (?, 'assistant', ?)",
                                (identifier, '\n\n'.join(lines))).lastrowid
        db.execute('INSERT INTO pending_attack_runs VALUES (?,?,?,?,?,?,?)',
                   (request_id, identifier, proposal_id, scenario, run_id, json.dumps(result), message_id))
        return result
