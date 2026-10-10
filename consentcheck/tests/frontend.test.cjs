// Dependency-free renderer checks: node --test tests/frontend.test.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
function node(tag = '') {
  return {tag, children: [], textContent: '', append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; }, setAttribute() {}, addEventListener() {}};
}
function text(n) { return [n.textContent, ...n.children.map(text)].join(' '); }
function load(file, response) {
  const nodes = new Map();
  const get = (id) => { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); };
  const context = {AbortController, setTimeout, clearTimeout, setInterval: () => 0,
    document: {getElementById: get, querySelector: get, querySelectorAll: () => [], createElement: node},
    window: {addEventListener() {}}, sessionStorage: {getItem: () => null},
    fetch: async () => ({ok: true, json: async () => response})};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../static', file), 'utf8'), context);
  return {context, get};
}
test('completed secured approval card displays exact fields without stale send text or buttons', () => {
  const {context} = load('app.js', {configured: true});
  const card = context.proposalCard({id: 'one', title: 'Q3', recipient: 'Alice', cc: ['Bob'], bcc: ['Carol'],
    subject: 'Exact subject', body: '<script>literal message</script>', expires_at: '2026-10-10T00:00:00Z',
    security: 'secured', status: 'approved', execution_status: 'completed'});
  const content = text(card);
  for (const expected of ['Approved · simulated send completed', 'Alice', 'Bob', 'Carol', 'Exact subject', '<script>literal message</script>']) assert(content.includes(expected));
  for (const stale of ['Approved · not sent', "Sending isn't available yet"]) assert(!content.includes(stale));
  const buttons = (n) => (n.tag === 'button' ? 1 : 0) + n.children.reduce((s, c) => s + buttons(c), 0);
  assert.equal(buttons(card), 0);
});
test('approval without execution remains visibly unsent', () => {
  const {context} = load('app.js', {configured: true});
  assert(text(context.proposalCard({status: 'approved', title: 'Q3', recipient: 'Alice'})).includes('Approved · not sent'));
});
test('dashboard renders backend states, trusted message, outcomes and actual outbox', () => {
  const empty = {actions: [], legacy: [], events: [], outbox: []};
  const {context, get} = load('dashboard.js', empty);
  const base = {id: 'action', state: 'pending', descriptor: {title: 'Q3', recipient: 'Alice', subject: 'Review', body: 'Message'},
    fingerprint_check: 'matched', verification: 'not_checked', last_attempt: 'No execution outcome recorded', fingerprint: 'hash'};
  context.renderDashboard({...empty, actions: [base]});
  assert(text(get('pending')).includes('Message'));
  assert(text(get('verification')).includes('not_checked'));
  context.renderDashboard({...empty, actions: [{...base, state: 'approved'}]});
  assert(text(get('verification')).includes('Approved · not executed'));
  context.renderDashboard({...empty, actions: [{...base, state: 'executed', verification: 'permitted'}],
    outbox: [{proposal_id: 'action', recipient: 'Alice', subject: 'Review', status: 'simulated_sent'}]});
  assert(text(get('pending')).includes('No pending approvals'));
  assert(text(get('verification')).includes('Simulated send completed'));
  assert(text(get('outbox')).includes('simulated_sent'));
});
test('dashboard handles empty backend data', () => {
  const empty = {actions: [], legacy: [], events: [], outbox: []};
  const {context, get} = load('dashboard.js', empty);
  context.renderDashboard(empty);
  assert(text(get('pending')).includes('No pending approvals'));
  assert(text(get('activity')).includes('No records yet'));
});

test('security metrics use server values, preserve zeros, and clear on failure', async () => {
  const {context, get} = load('security-center.js', {engine_status: 'active', metrics: {reviewed: 4, executed: 0, blocked: 1, pending: 2}});
  await context.window.refreshSecurityCenter();
  assert.equal(get('metric-reviewed').textContent, '4');
  assert.equal(get('metric-executed').textContent, '0');
  assert(get('protection-status').textContent.includes('engine active'));
  context.fetch = async () => ({ok: false});
  await context.window.refreshSecurityCenter();
  assert.equal(get('metric-reviewed').textContent, '—');
  assert(get('protection-status').textContent.includes('Unavailable'));
});

test('invalid metric responses cannot imply active protection or fabricated zeros', async () => {
  const {context, get} = load('security-center.js', {engine_status: 'active', metrics: {}});
  await context.window.refreshSecurityCenter();
  assert.equal(get('metric-pending').textContent, '—');
  assert(get('protection-status').textContent.includes('Unavailable'));
});
