"""Trusted local action records. Hashes detect consistency, not database compromise."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .documents import read_document
from .schemas import ProposeInput


class SecurityViolation(ValueError):
    pass


class ActionMismatch(SecurityViolation):
    """The actual operation does not match the trusted, versioned action."""


class Action(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    schema_version: Literal[1]
    tool: Literal['send_document']
    action_type: Literal['send_document']
    scope: Literal['local_simulated_outbox']
    task_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    run_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    recipient: str
    cc: list[str]
    bcc: list[str]
    subject: str
    body: str
    document_id: str
    title: str
    content_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


def now():
    return datetime.now(timezone.utc)


def canonical(action):
    return json.dumps(action.model_dump(), sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def fingerprint(action):
    return sha256(canonical(action).encode('utf-8')).hexdigest()


def event(db, identifier, kind, detail):
    db.execute('INSERT INTO security_events(action_id,event,detail) VALUES (?,?,?)', (identifier, kind, detail))


def register(db, identifier, conversation_id, run_id, proposal):
    data = ProposeInput.model_validate({key: proposal[key] for key in ProposeInput.model_fields if key in proposal})
    document = read_document(data.document_id)
    action = Action(schema_version=1, tool='send_document', action_type='send_document',
                    scope='local_simulated_outbox', task_id=conversation_id, run_id=run_id,
                    recipient=data.recipient, cc=data.cc, bcc=data.bcc,
                    subject=data.subject if data.subject is not None else document['title'],
                    body=data.body if data.body is not None else f"Please find attached: {document['title']}.",
                    document_id=data.document_id, title=document['title'],
                    content_sha256=sha256(document['content'].encode('utf-8')).hexdigest())
    created = now()
    db.execute('''INSERT INTO secure_actions
        (id,conversation_id,run_id,scope,descriptor,fingerprint,created_at,expires_at,state)
        VALUES (?,?,?,?,?,?,?,?,'pending')''',
        (identifier, conversation_id, run_id, action.scope, canonical(action), fingerprint(action),
         created.isoformat(), (created + timedelta(minutes=10)).isoformat()))
    event(db, identifier, 'proposed', 'Trusted action created; awaiting approval.')
    return {**action.model_dump(), 'status': 'proposed', 'sent': False}


def load(db, identifier):
    cursor = db.execute('SELECT * FROM secure_actions WHERE id = ?', (identifier,))
    row = cursor.fetchone()
    if row is None:
        raise SecurityViolation('No secured approval exists. Prepare a new proposal.')
    return dict(zip([column[0] for column in cursor.description], row))


def verify(record, details):
    try:
        action = Action.model_validate(json.loads(record['descriptor']))
        # Execution candidates must supply every versioned field, even empty CC/BCC.
        candidate = Action.model_validate({key: details[key] for key in Action.model_fields})
        validated = ProposeInput.model_validate({key: getattr(action, key) for key in ProposeInput.model_fields})
        if any(getattr(validated, key) != getattr(action, key) for key in ProposeInput.model_fields):
            raise ValueError()
        if (fingerprint(action) != record['fingerprint'] or canonical(candidate) != canonical(action)
                or action.task_id != record['conversation_id'] or action.run_id != record['run_id']
                or action.scope != record['scope']):
            raise ValueError()
        document = read_document(action.document_id)
        if (sha256(document['content'].encode('utf-8')).hexdigest() != action.content_sha256
                or document['title'] != action.title):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ActionMismatch('Action or document differs from the trusted proposal. Prepare a new proposal.') from None
    return action, document


def require_live(record, state):
    if record['state'] != state or record['outbox_id'] is not None:
        raise SecurityViolation('Approval is not available for this operation or was already used.')
    try:
        expired = datetime.fromisoformat(record['expires_at']) <= now()
    except (ValueError, TypeError):
        expired = True
    if expired:
        raise SecurityViolation('Approval expired. Prepare a new proposal.')


def approve(db, identifier, details):
    record = load(db, identifier)
    require_live(record, 'pending')
    verify(record, details)
    db.execute("UPDATE secure_actions SET state='approved', decision='approve', decided_at=? WHERE id=?",
               (now().isoformat(), identifier))
    event(db, identifier, 'approved', 'Specific action approved; execution not yet completed.')


def gate(db, identifier, details):
    if not db.in_transaction:
        raise SecurityViolation('Execution requires a transaction.')
    record = load(db, identifier)
    require_live(record, 'approved')
    if record['decision'] != 'approve' or not record['decided_at']:
        raise SecurityViolation('An explicit approval decision is required.')
    row = db.execute('SELECT conversation_id,details,status FROM proposals WHERE id=?', (identifier,)).fetchone()
    if not row or row[0] != record['conversation_id'] or row[2] != 'proposed':
        raise SecurityViolation('Proposal scope or state is invalid.')
    verify(record, json.loads(row[1]))
    action, document = verify(record, details)
    event(db, identifier, 'execution_verified', 'Approved action and current document verified.')
    return action, document


def card(db, identifier, fallback):
    try:
        record = load(db, identifier)
    except SecurityViolation:
        return {**fallback, 'security': 'legacy', 'expires_at': None}
    try:
        action = Action.model_validate(json.loads(record['descriptor']))
    except ValueError:
        return {**fallback, 'status': 'blocked', 'security': 'invalid'}
    states = {'pending': 'proposed', 'approved': 'approved', 'executed': 'approved',
              'rejected': 'rejected', 'failed': 'failed', 'blocked': 'blocked'}
    completed = record['state'] == 'executed'
    return {**fallback, **action.model_dump(), 'status': states[record['state']],
            'security': 'secured', 'expires_at': record['expires_at'], 'sent': completed,
            'simulated': True, 'execution_status': 'completed' if completed else
            'failed' if record['state'] == 'failed' else 'not_executed',
            'outbox_id': record['outbox_id'], **(json.loads(record['result']) if record['result'] else {})}


def dashboard(path):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        actions = []
        for row in db.execute('SELECT * FROM secure_actions ORDER BY created_at DESC LIMIT 200'):
            record = dict(row)
            try:
                action = json.loads(record['descriptor'])
                verify(record, action)
                check = 'matched'
            except (ValueError, TypeError):
                action, check = {}, 'mismatch'
            attempt = db.execute("SELECT event FROM security_events WHERE action_id=? AND event IN ('blocked','failed','executed') ORDER BY id DESC LIMIT 1", (record['id'],)).fetchone()
            actions.append({**record, 'descriptor': action, 'fingerprint_check': check,
                            'last_attempt': attempt[0] if attempt else 'No execution outcome recorded',
                            'expired': datetime.fromisoformat(record['expires_at']) <= now()})
        legacy = [dict(row) for row in db.execute('''SELECT p.id,p.status,p.conversation_id FROM proposals p
                     LEFT JOIN secure_actions s ON s.id=p.id WHERE s.id IS NULL ORDER BY p.message_id DESC LIMIT 200''')]
        return {'actions': actions, 'legacy': legacy,
                'events': [dict(row) for row in db.execute('SELECT * FROM security_events ORDER BY id DESC LIMIT 200')],
                'outbox': [dict(row) for row in db.execute('SELECT * FROM outbox ORDER BY id DESC LIMIT 200')]}
