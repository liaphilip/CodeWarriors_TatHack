const $ = (selector) => document.querySelector(selector);
const messages = $('#messages');
const input = $('#task');
let conversationId = sessionStorage.getItem('assistant-conversation');
let busy = false;
let currentConversation = null;
const revisions = new Map();
function eligiblePending(proposal) {
  return proposal.status === 'proposed' && proposal.attack_eligible === true
    && Date.parse(proposal.expires_at) > Date.now();
}
function pendingAttacks() {
  return (currentConversation?.messages || []).flatMap((message) => message.proposals || []).filter(eligiblePending);
}
const activityLabels = {
  search_documents: 'Searched documents', read_document: 'Read document',
  propose_send_document: 'Prepared action',
};
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}
function scrollToLatest() {
  requestAnimationFrame(() => $('#main').scrollTo({top: $('#main').scrollHeight, behavior: 'smooth'}));
}
function setBusy(value, label = 'Thinking through your request…') {
  busy = value;
  $('#send').disabled = value;
  $('#new-chat').disabled = value;
  $('#working').hidden = !value;
  $('#working-label').textContent = label;
  messages.setAttribute('aria-busy', String(value));
  document.querySelectorAll('.proposal-actions button, .examples button, [data-chat-demo]').forEach((node) => { node.disabled = value; });
  document.querySelectorAll('[data-chat-demo]').forEach((node) => { node.disabled = value || pendingAttacks().length !== 1; });
}
// Render basic emphasis safely using text nodes, never HTML from model output.
function richText(container, text) {
  for (const paragraph of text.split(/\n\s*\n/)) {
    const lines = paragraph.split('\n');
    const isList = lines.every((line) => /^\s*[-*]\s+/.test(line));
    const group = element(isList ? 'ul' : 'p');
    for (const line of isList ? lines : [paragraph]) {
      const p = isList ? element('li') : group;
      for (const piece of (isList ? line.replace(/^\s*[-*]\s+/, '') : line).split(/(\*\*[^*]+\*\*)/g)) {
        p.append(piece.startsWith('**') && piece.endsWith('**')
          ? element('strong', '', piece.slice(2, -2)) : document.createTextNode(piece));
      }
      if (isList) group.append(p);
    }
    container.append(group);
  }
}
function proposalCard(proposal) {
  const card = element('article', 'proposal');
  const content = element('div', 'proposal-content');
  const heading = element('div', 'proposal-heading');
  heading.append(element('h2', '', 'Ready to send this document?'));
  const statuses = {proposed: 'Awaiting your review',
    approved: proposal.execution_status === 'completed' ? 'Approved · simulated send completed' : 'Approved · not sent',
    rejected: 'Rejected', failed: 'Simulated send failed', blocked: 'Execution blocked'};
  const expired = proposal.status === 'proposed' && proposal.expires_at && Date.parse(proposal.expires_at) <= Date.now();
  const visualState = expired ? 'expired' : proposal.execution_status === 'completed' ? 'executed' : proposal.status;
  heading.append(element('span', `proposal-status ${visualState}`, expired ? 'Expired · new proposal required' : statuses[proposal.status] || 'Not sent'));
  content.append(heading);
  const details = element('dl');
  const fields = [['Document', proposal.title], ['Recipient', proposal.recipient], ['Action', 'Send document']];
  if (proposal.security === 'secured') fields.push(['CC', proposal.cc.join(', ') || 'None'],
    ['BCC', proposal.bcc.join(', ') || 'None'], ['Subject', proposal.subject || '(empty)'],
    ['Body', proposal.body || '(empty)'], ['Expires', new Date(proposal.expires_at).toLocaleString()]);
  for (const [label, value] of fields) {
    details.append(element('dt', '', label), element('dd', '', value));
  }
  const notice = ['failed', 'blocked'].includes(proposal.status) ? 'Nothing was sent. Please prepare a new proposal to try again.'
    : proposal.status === 'approved' && proposal.execution_status !== 'completed'
      ? 'Approval was recorded without completed execution. Nothing was sent.'
      : 'Approval records a simulated send in the local outbox. No real email is sent.';
  content.append(details, element('p', 'proposal-notice', notice));
  card.append(content);
  if (proposal.security === 'legacy') content.append(element('p', 'proposal-notice', 'Legacy record: not secured by Phase 2. Prepare a new proposal to send.'));
  if (proposal.status === 'proposed' && proposal.security !== 'legacy' && !expired) {
    const actions = element('div', 'proposal-actions');
    for (const [decision, label] of [['approve', 'Approve'], ['reject', 'Reject']]) {
      const button = element('button', decision, label);
      button.type = 'button';
      button.setAttribute('aria-label', `${label} ${proposal.title} to ${proposal.recipient}`);
      button.addEventListener('click', () => review(proposal.id, decision));
      actions.append(button);
    }
    card.append(actions);
    if (eligiblePending(proposal)) {
      content.append(element('p', 'proposal-notice', 'Controlled synthetic attack demonstrations against this pending approval. Not generated by Gemini. Running one permanently blocks this proposal.'));
      for (const [scenario, label] of [['recipient-tampering', 'Simulate Recipient Tampering'],
        ['misleading-approval', 'Simulate Misleading Approval Display']]) {
        const button = element('button', 'reject', label);
        button.type = 'button';
        button.addEventListener('click', () => simulatePendingAttack(proposal.id, scenario));
        actions.append(button);
      }
    }
  }
  return card;
}
function addMessage(message, isError = false) {
  $('#welcome').hidden = true;
  const row = element('article', `message ${message.role}${isError ? ' error' : ''}`);
  row.setAttribute('aria-label', message.role === 'user' ? 'You' : 'AI Assistant');
  if (message.role !== 'user') {
    const avatar = element('span', 'avatar', '✳');
    avatar.setAttribute('aria-hidden', 'true');
    row.append(avatar);
  }
  const body = element('div', 'message-body');
  if (message.role !== 'user') body.append(element('p', 'message-label', 'AI Assistant'));
  if (message.activity?.length) {
    const activity = element('div', 'activity');
    for (const name of [...new Set(message.activity.map((item) => item.tool))]) {
      if (activityLabels[name]) {
        const failed = message.activity.some((item) => item.tool === name && item.result?.error);
        activity.append(element('span', '', failed ? 'A step needs attention' : activityLabels[name]));
      }
    }
    body.append(activity);
  }
  const text = element('div', 'message-text');
  const evidence = message.attack_result;
  const verifiedBlock = evidence?.outcome === 'PREVENTED' && evidence.gate_rejected === true
    && evidence.mismatch_verified === true && evidence.trusted_action_unchanged === true
    && evidence.approval_status === 'blocked' && evidence.execution_occurred === false
    && evidence.unauthorized_production_write === false
    && ['recipient-tampering', 'misleading-approval'].includes(evidence.scenario);
  if (verifiedBlock) {
    text.append(element('h3', 'blocked-heading', 'ATTACK BLOCKED'));
    text.append(element('p', '', evidence.scenario === 'recipient-tampering'
      ? 'The attempted recipient differed from the reviewed recipient, so PHANTOM LOOP rejected the action.'
      : 'The attempted recipient and message differed from the reviewed details, so PHANTOM LOOP rejected the action.'));
    text.append(element('p', '', 'No message was sent. This approval is now blocked.'));
    const disclosure = element('details', 'security-evidence');
    disclosure.append(element('summary', '', 'View security evidence'));
    const report = element('div', 'security-evidence-report');
    richText(report, message.text);
    disclosure.append(report);
    text.append(disclosure);
  } else {
    richText(text, message.text);
  }
  body.append(text);
  for (const proposal of message.proposals || []) body.append(proposalCard(proposal));
  row.append(body);
  messages.append(row);
}
function render(data) {
  // A delayed snapshot must never replace newer blocking evidence with an old
  // pending card or a previous successful-send message.
  if (conversationId && conversationId !== data.conversation_id) return false;
  const revision = Math.max(0, ...data.messages.map((message) => message.id || 0));
  if (revision < (revisions.get(data.conversation_id) || 0)) return false;
  revisions.set(data.conversation_id, revision);
  currentConversation = data;
  conversationId = data.conversation_id;
  sessionStorage.setItem('assistant-conversation', conversationId);
  messages.replaceChildren();
  $('#welcome').hidden = Boolean(data.messages.length);
  for (const message of data.messages) addMessage(message);
  return true;
}
async function api(path, payload) {
  let response;
  try {
    response = await fetch(path, payload ? {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
    } : {});
  } catch { throw new Error("I couldn't connect. Check that the app is running, then try again."); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : "I couldn't complete that request. Please try again.");
  return data;
}
async function submit() {
  const task = input.value.trim();
  if (!task || busy) return;
  addMessage({role: 'user', text: task});
  input.value = ''; input.style.height = 'auto';
  setBusy(true); scrollToLatest();
  try {
    if (!conversationId) {
      const created = await api('/api/conversations', {});
      conversationId = created.conversation_id;
      sessionStorage.setItem('assistant-conversation', conversationId);
    }
    render(await api('/api/run', {task, conversation_id: conversationId}));
  } catch (error) {
    if (conversationId) {
      try {
        const saved = await api(`/api/conversations/${conversationId}`);
        if (saved.messages.length) render(saved);
      } catch {}
    }
    addMessage({role: 'assistant', text: error.message}, true);
  }
  finally { setBusy(false); input.focus(); scrollToLatest(); window.refreshSecurityCenter?.(); }
}
async function review(proposalId, decision) {
  if (busy) return;
  if (!(currentConversation?.messages || []).some((message) => (message.proposals || [])
      .some((proposal) => proposal.id === proposalId && proposal.status === 'proposed'))) return;
  setBusy(true, 'Saving your choice…'); scrollToLatest();
  try {
    render(await api('/api/decisions', {conversation_id: conversationId, proposal_id: proposalId, decision}));
  } catch (error) {
    // A request may have succeeded before the connection failed. Reload server
    // state before enabling controls again, avoiding a stale actionable card.
    try { render(await api(`/api/conversations/${conversationId}`)); } catch {}
    addMessage({role: 'assistant', text: error.message}, true);
  } finally { setBusy(false); input.focus(); scrollToLatest(); window.refreshSecurityCenter?.(); }
}
async function simulatePendingAttack(proposalId, scenario) {
  if (busy) return;
  if (!['recipient-tampering', 'misleading-approval'].includes(scenario)
      || !pendingAttacks().some((proposal) => proposal.id === proposalId)) return;
  const identifier = conversationId;
  const key = `pending-demo-${identifier}-${proposalId}-${scenario}`;
  const token = sessionStorage.getItem(key) || crypto.randomUUID().replaceAll('-', '');
  sessionStorage.setItem(key, token);
  setBusy(true, 'Checking the attempted alteration against the security gate…');
  try {
    const data = await api(`/api/conversations/${identifier}/proposals/${proposalId}/attacks/${scenario}`, {request_id: token});
    if (data.attack_result?.proposal_id !== proposalId || data.attack_result?.scenario !== scenario
        || data.attack_result?.approval_status !== 'blocked') {
      throw new Error('Attack result was not verified. Reload the approval before taking another action.');
    }
    render(data);
    sessionStorage.removeItem(key);
  } catch (error) {
    try { render(await api(`/api/conversations/${identifier}`)); } catch {}
    addMessage({role: 'assistant', text: error.message}, true);
  } finally { setBusy(false); scrollToLatest(); window.refreshSecurityCenter?.(); }
}
$('#task-form').addEventListener('submit', (event) => { event.preventDefault(); submit(); });
input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); submit(); }
});
input.addEventListener('input', () => {
  input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight, 150)}px`;
});
document.querySelectorAll('[data-prompt]').forEach((button) => button.addEventListener('click', () => {
  input.value = button.dataset.prompt; submit();
}));
$('#new-chat').addEventListener('click', () => {
  if (busy) return;
  conversationId = null; sessionStorage.removeItem('assistant-conversation');
  currentConversation = null;
  messages.replaceChildren(); $('#welcome').hidden = false; input.value = ''; input.style.height = 'auto'; input.focus();
  $('#main').scrollTo({top: 0, behavior: 'smooth'});
});
async function initialize() {
  if (conversationId) {
    setBusy(true, 'Restoring your conversation…');
    try { render(await api(`/api/conversations/${conversationId}`)); scrollToLatest(); }
    catch { conversationId = null; sessionStorage.removeItem('assistant-conversation'); }
    finally { setBusy(false); }
  }
  try {
    const health = await api('/health');
    if (!health.configured) $('#connection').textContent = 'The assistant needs a local API key and model before we can chat.';
  } catch { $('#connection').textContent = "Can't connect to the app. Please check the server and refresh."; }
}
initialize();


async function simulateChatAttack(scenario) {
  if (busy || !['recipient-tampering', 'misleading-approval'].includes(scenario)) return;
  $('#chat-demo-error').textContent = '';
  const eligible = pendingAttacks();
  if (eligible.length !== 1) {
    $('#chat-demo-error').textContent = 'Choose a specific eligible pending approval card. Recorded evidence is available in the Security Center.';
    return;
  }
  await simulatePendingAttack(eligible[0].id, scenario);
}
document.querySelectorAll('[data-chat-demo]').forEach((button) => button.addEventListener('click', () => simulateChatAttack(button.dataset.chatDemo)));
setBusy(busy);
