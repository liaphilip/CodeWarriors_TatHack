"""Read-only, full-history aggregates; legacy records are never counted as secured."""
import sqlite3
from .security import now


def security_summary(path):
    with sqlite3.connect(path) as db:
        db.execute('BEGIN')
        reviewed, executed, blocked, pending = db.execute('''
            SELECT
              COUNT(CASE WHEN a.decision IS NOT NULL OR EXISTS (
                SELECT 1 FROM security_events e WHERE e.action_id=a.id AND e.event='blocked'
              ) THEN 1 END),
              COUNT(CASE WHEN a.state='executed' AND a.decision='approve' AND EXISTS (
                SELECT 1 FROM outbox o WHERE o.id=a.outbox_id AND o.proposal_id=a.id
                  AND o.status='simulated_sent'
              ) THEN 1 END),
              COUNT(CASE WHEN EXISTS (
                SELECT 1 FROM security_events e WHERE e.action_id=a.id AND e.event='blocked'
              ) THEN 1 END),
              COUNT(CASE WHEN a.state='pending' AND a.decision IS NULL
                AND julianday(a.expires_at)>julianday(?) THEN 1 END)
            FROM secure_actions a
        ''', (now().isoformat(),)).fetchone()
        return {'engine_status': 'active', 'metrics': {
            'reviewed': reviewed, 'executed': executed, 'blocked': blocked, 'pending': pending}}
