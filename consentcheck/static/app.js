const $ = (selector) => document.querySelector(selector);
const messages = $('#messages');
const input = $('#task');
let conversationId = sessionStorage.getItem('assistant-conversation');
let busy = false;
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
  if (proposal.status === 'proposed' && proposal.security !== 'legacy') {
    const actions = element('div', 'proposal-actions');
    for (const [decision, label] of [['approve', 'Approve'], ['reject', 'Reject']]) {
      const button = element('button', decision, label);
      button.type = 'button';
      button.setAttribute('aria-label', `${label} ${proposal.title} to ${proposal.recipient}`);
      button.addEventListener('click', () => review(proposal.id, decision));
      actions.append(button);
    }
    card.append(actions);
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
  richText(text, message.text);
  body.append(text);
  for (const proposal of message.proposals || []) body.append(proposalCard(proposal));
  row.append(body);
  messages.append(row);
}
function render(data) {
  conversationId = data.conversation_id;
  sessionStorage.setItem('assistant-conversation', conversationId);
  messages.replaceChildren();
  $('#welcome').hidden = Boolean(data.messages.length);
  for (const message of data.messages) addMessage(message);
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
  setBusy(true, 'Running controlled synthetic demonstration…');
  $('#chat-demo-error').textContent = '';
  try {
    if (!conversationId) {
      const created = await api('/api/conversations', {});
      conversationId = created.conversation_id;
      sessionStorage.setItem('assistant-conversation', conversationId);
    }
    const key = `chat-demo-${conversationId}-${scenario}`;
    const token = sessionStorage.getItem(key) || crypto.randomUUID().replaceAll('-', '');
    sessionStorage.setItem(key, token);
    render(await api(`/api/conversations/${conversationId}/security/attacks/${scenario}`, {request_id: token}));
    sessionStorage.removeItem(key);
  } catch (error) {
    $('#chat-demo-error').textContent = `${error.message} Retry retains the same demo request ID.`;
  } finally { setBusy(false); scrollToLatest(); }
}
document.querySelectorAll('[data-chat-demo]').forEach((button) => button.addEventListener('click', () => simulateChatAttack(button.dataset.chatDemo)));
