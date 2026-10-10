// Dependency-free renderer checks: node --test tests/frontend.test.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
function node(tag = '') {
  return {tag, children: [], textContent: '', style: {}, focus() {}, scrollTo() {},
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; }, setAttribute() {}, addEventListener() {}};
}
function text(n) { return [n.textContent, ...n.children.map(text)].join(' '); }
function load(file, response) {
  const nodes = new Map();
  const get = (id) => { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); };
  const context = {AbortController, setTimeout, clearTimeout, setInterval: () => 0,
    document: {getElementById: get, querySelector: get, querySelectorAll: () => [], createElement: node,
      createTextNode: (value) => ({...node(), textContent: value})},
    requestAnimationFrame: (fn) => fn(), crypto: {randomUUID: () => 'a'.repeat(32)},
    window: {addEventListener() {}}, sessionStorage: {getItem: () => null, setItem() {}, removeItem() {}},
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

test('eligible pending cards expose exactly two attacks and terminal cards expose none', () => {
  const {context} = load('app.js', {configured: true});
  const proposal = {id: 'one', title: 'Q3', recipient: 'alice@example.test', cc: [], bcc: [],
    subject: 'Review', body: 'Synthetic', expires_at: '2099-01-01T00:00:00Z', security: 'secured', status: 'proposed', attack_eligible: true};
  const labels = (n) => [...(n.tag === 'button' ? [n.textContent] : []), ...n.children.flatMap(labels)];
  assert.deepEqual(labels(context.proposalCard(proposal)), ['Approve', 'Reject', 'Simulate Recipient Tampering', 'Simulate Misleading Approval Display']);
  for (const status of ['blocked', 'rejected', 'approved', 'failed']) assert.equal(labels(context.proposalCard({...proposal, status})).length, 0);
  assert.equal(labels(context.proposalCard({...proposal, expires_at: '2000-01-01T00:00:00Z'})).length, 0);
  assert.equal(labels(context.proposalCard({...proposal, recipient: 'someone@example.com', attack_eligible: false})).length, 2);
});

for (const scenario of ['recipient-tampering', 'misleading-approval']) {
  test(`${scenario}: footer targets the Alice pending card, ignores duplicates and stale success`, async () => {
    const {context, get} = load('app.js', {configured: true});
    const proposal = {id: 'p1', title: 'Q3', recipient: 'Alice', cc: [], bcc: [],
      body: 'Synthetic', expires_at: '2099-01-01T00:00:00Z', status: 'proposed', security: 'secured', attack_eligible: true};
    const pending = {conversation_id: 'c1', messages: [{id: 1, role: 'assistant', text: 'Review', proposals: [proposal]}]};
    context.render(pending);
    let release;
    const calls = [];
    context.fetch = async (url, options) => {
      calls.push({url, body: JSON.parse(options.body)});
      return await new Promise((resolve) => { release = resolve; });
    };
    const operation = context.simulateChatAttack(scenario);
    await context.simulateChatAttack(scenario);
    await context.review('p1', 'approve');
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, `/api/conversations/c1/proposals/p1/attacks/${scenario}`);
    assert.deepEqual(Object.keys(calls[0].body), ['request_id']);
    const blocked = {conversation_id: 'c1', attack_result: {proposal_id: 'p1', scenario, approval_status: 'blocked'},
      messages: [{...pending.messages[0], proposals: [{...proposal, status: 'blocked', attack_eligible: false}]},
        {id: 3, role: 'assistant', text: 'ATTACK BLOCKED — approval rejected by security gate'}]};
    release({ok: true, json: async () => blocked});
    await operation;
    assert(text(get('#messages')).includes('ATTACK BLOCKED'));
    // Old response from before the blocking result must not resurrect a success or pending card.
    assert.equal(context.render({conversation_id: 'c1', messages: [{id: 2, role: 'assistant',
      text: 'Done! The Quarterly Report — Q3 2026 was sent to Alice (simulated).', proposals: [proposal]}]}), false);
    assert(!text(get('#messages')).includes('Done!'));
    await context.review('p1', 'approve');
    await context.simulatePendingAttack('p1', scenario);
    await context.simulateChatAttack(scenario);
    assert.equal(calls.length, 1);
  });
}

test('footer refuses ambiguous targets rather than attacking the wrong approval', async () => {
  const {context} = load('app.js', {configured: true});
  const p = {title:'Q3', recipient:'Alice', status:'proposed', attack_eligible:true, expires_at:'2099-01-01T00:00:00Z'};
  context.render({conversation_id:'c1', messages:[{id:1, role:'assistant', text:'Review', proposals:[{...p,id:'one'}, {...p,id:'two'}]}]});
  context.fetch = async () => { throw new Error('No request allowed'); };
  await context.simulateChatAttack('recipient-tampering');
});

for (const scenario of ['recipient-tampering', 'misleading-approval']) {
  test(`${scenario}: verified block has concise default and full collapsed evidence`, () => {
    const {context, get} = load('app.js', {configured:true});
    const report = 'Run ID: full-run\n\nProposal ID: full-proposal\n\nOriginal body: <script>literal</script>\n\nOutbox evidence: before=0, after attempt=0';
    const evidence = {scenario, outcome:'PREVENTED',gate_rejected:true,mismatch_verified:true,
      trusted_action_unchanged:true,approval_status:'blocked',execution_occurred:false,unauthorized_production_write:false};
    context.addMessage({role:'assistant',text:report,attack_result:evidence});
    const find = (n,tag) => n.tag === tag ? n : n.children.map(c=>find(c,tag)).find(Boolean);
    const row = get('#messages').children[0];
    const details = find(row,'details');
    assert(details);
    assert(!details.open); // Native details is collapsed unless explicitly opened.
    assert.equal(details.children[0].tag,'summary');
    assert.equal(details.children[0].textContent,'View security evidence');
    assert(text(details).includes('full-run') && text(details).includes('full-proposal'));
    assert(text(details).includes('<script>literal</script>'));
    const visible = n => n.tag === 'details' ? text(n.children[0]) : [n.textContent,...n.children.map(visible)].join(' ');
    assert(visible(row).includes('ATTACK BLOCKED'));
    assert(visible(row).includes('No message was sent. This approval is now blocked.'));
    assert(!visible(row).includes('full-run'));
    details.open = true;
    assert(text(details).includes('Outbox evidence: before=0, after attempt=0'));
  });
}

test('unverified evidence never receives a fabricated no-send confirmation', () => {
  for (const evidence of [undefined, {outcome:'FAILED'}, {outcome:'INCOMPLETE'}, {outcome:'PREVENTED'}]) {
    const {context,get} = load('app.js',{configured:true});
    context.addMessage({role:'assistant',text:'Full unverified report',attack_result:evidence});
    const content = text(get('#messages'));
    assert(content.includes('Full unverified report'));
    assert(!content.includes('No message was sent. This approval is now blocked.'));
    assert(!content.includes('ATTACK BLOCKED'));
  }
});
