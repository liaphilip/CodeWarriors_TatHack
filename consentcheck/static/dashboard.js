const select = (id) => document.getElementById(id);
function node(tag, text) { const el = document.createElement(tag); el.textContent = text ?? ''; return el; }
const states = {pending: 'Pending approval', approved: 'Approved · not executed', executed: 'Simulated send completed', rejected: 'Rejected', blocked: 'Blocked', failed: 'Execution failed'};
function table(id, headings, rows) {
  const container = select(id); container.replaceChildren();
  if (!rows.length) { container.append(node('p', 'No records yet.')); return; }
  const wrapper = node('div'); wrapper.className = 'table-scroll';
  const t = node('table'), head = node('thead'), header = node('tr'), body = node('tbody');
  headings.forEach((label) => { const cell = node('th', label); cell.scope = 'col'; header.append(cell); });
  head.append(header);
  rows.forEach((values) => { const row = node('tr'); values.forEach((value) => row.append(node('td', value))); body.append(row); });
  t.append(head, body); wrapper.append(t); container.append(wrapper);
}
function renderDashboard(data) {
  const pending = select('pending'); pending.replaceChildren();
  const actions = data.actions.filter((a) => a.state === 'pending');
  if (!actions.length) pending.append(node('p', 'No pending approvals.'));
  actions.forEach((action) => {
    const card = node('article'); card.className = 'action';
    card.append(node('strong', action.expired ? 'Expired · new proposal required' : 'Awaiting approval'));
    const details = node('dl'), a = action.descriptor;
    Object.entries({Action: action.id, Document: `${a.title || 'Unavailable'} (${a.document_id || 'unknown'})`, Recipient: a.recipient,
      CC: a.cc?.join(', ') || 'None', BCC: a.bcc?.join(', ') || 'None', Subject: a.subject, Body: a.body,
      Expires: action.expires_at, Scope: action.scope}).forEach(([key, value]) => details.append(node('dt', key), node('dd', value)));
    card.append(details); pending.append(card);
  });
  table('verification', ['Action ID', 'Approval / execution state', 'Current fingerprint + document', 'Recorded gate outcome', 'Latest attempt', 'Fingerprint'], [
    ...data.actions.map((a) => [a.id, `${states[a.state]}${a.expired && ['pending','approved'].includes(a.state) ? ' · expired' : ''}`, a.fingerprint_check, a.verification, a.last_attempt, a.fingerprint]),
    ...data.legacy.map((a) => [a.id, `Legacy · ${a.status}`, 'Not available', 'Not verified by Phase 2', 'Not available', '—'])
  ]);
  table('activity', ['Time (UTC)', 'Action ID', 'Event', 'Details'], data.events.map((e) => [e.created_at, e.action_id, e.event, e.detail]));
  table('outbox', ['Time (UTC)', 'Action ID', 'Recipient / CC / BCC', 'Subject', 'Status'], data.outbox.map((o) => [o.created_at,
    o.proposal_id || 'Legacy', `${o.recipient}\nCC: ${o.cc || '[]'}\nBCC: ${o.bcc || '[]'}`, o.subject ?? 'Not recorded (legacy)', o.status]));
}
async function refresh() {
  select('refresh').disabled = true;
  try {
    const response = await fetch('/api/dashboard', {cache: 'no-store'});
    if (!response.ok) throw new Error('Could not load security records. Please retry.');
    renderDashboard(await response.json());
    select('dashboard-status').textContent = `Updated ${new Date().toLocaleTimeString()}. Most recent 200 records per section.`;
    select('dashboard-status').className = '';
  } catch (error) {
    select('dashboard-status').textContent = `${error.message} Displayed records may be stale.`;
    select('dashboard-status').className = 'error';
  } finally { select('refresh').disabled = false; }
}
select('refresh').addEventListener('click', refresh);
window.addEventListener('focus', refresh);
refresh();
