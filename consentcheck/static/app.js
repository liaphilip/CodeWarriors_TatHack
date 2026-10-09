"use strict";

const $ = (id) => document.getElementById(id);
let proposal = null;
let approval = null;
let busy = false;
let locallyExpired = false;
const attackNames = {
  recipient_substitution: "Recipient Substitution",
  content_substitution: "Content Substitution",
  replay: "Replay",
  cross_task_reuse: "Cross-Task Reuse",
};

async function api(path, method = "GET", body) {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  const data = await response.json();
  if (!response.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map((item) => item.msg).join(" ") : data.detail;
    throw new Error(detail || "The request could not be completed.");
  }
  return data;
}

function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
}

function date(value) {
  return new Date(value).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" });
}

function controls() {
  $("approve").disabled = busy || !proposal || !!approval;
  $("reject").disabled = busy || !proposal || (!!approval && approval.status !== "APPROVED") || locallyExpired;
  $("execute").disabled = busy || !approval || approval.status !== "APPROVED" || locallyExpired;
  $("reset").disabled = busy;
  $("refresh").disabled = busy;
  document.querySelectorAll("[data-attack]").forEach((button) => { button.disabled = busy || !proposal; });
}

function renderProposal() {
  for (const [id, key] of Object.entries({ actor: "actor_id", task: "task_id", tool: "tool", recipient: "recipient", document: "document_name", content: "document_content" })) {
    $(id).textContent = proposal[key];
  }
}

function renderApproval() {
  const status = locallyExpired ? "EXPIRED" : (approval?.status || "PROPOSED");
  $("action-status").textContent = status;
  $("action-status").className = "badge " + status.toLowerCase();
  $("approval-empty").hidden = !!approval;
  $("approval-evidence").hidden = !approval;
  $("approval-evidence").replaceChildren();
  $("approval-indicator").textContent = approval ? "Recorded by server" : "Awaiting consent";
  if (approval) {
    for (const [label, value] of [["Approval ID", approval.approval_id], ["Status", status], ["Created", date(approval.created_at)], ["Expires", date(approval.expires_at)], ["Authority", "Server-generated · stored in SQLite"]]) {
      const group = document.createElement("div");
      const term = document.createElement("dt");
      const definition = document.createElement("dd");
      term.textContent = label;
      definition.textContent = value;
      group.append(term, definition);
      $("approval-evidence").append(group);
    }
  }
  $("step-approval").classList.toggle("active", !!approval);
  for (const id of ["step-execution", "step-evidence"]) $(id).classList.toggle("active", status === "EXECUTED");
  const messages = {
    PROPOSED: "Approve the proposed action to unlock execution.",
    APPROVED: "Approved and ready. The server will verify expiry before execution.",
    EXECUTED: "EXECUTION SUCCESSFUL · One simulated SENT record saved to SQLite.",
    REJECTED: "BLOCKED · This action was rejected. Reset the demo to start again.",
    EXPIRED: "BLOCKED · Approval expired. Reset the demo to start again.",
  };
  $("execution-message").textContent = messages[status] || "Awaiting human approval.";
  controls();
}

async function refreshOutbox() {
  const rows = await api("/api/outbox");
  $("outbox-body").replaceChildren();
  $("outbox-empty").hidden = rows.length > 0;
  $("outbox-table").hidden = rows.length === 0;
  $("outbox-count").textContent = `${rows.length} record${rows.length === 1 ? "" : "s"}`;
  for (const row of rows) {
    const tr = document.createElement("tr");
    for (const value of [row.approval_id + ` / #${row.id}`, row.recipient, row.document_name + "\n" + row.document_content, date(row.executed_at), row.status]) {
      const td = document.createElement("td");
      td.textContent = value;
      tr.append(td);
    }
    tr.lastChild.className = "sent";
    $("outbox-body").append(tr);
  }
}

async function action(work) {
  if (busy) return;
  busy = true;
  controls();
  try { await work(); }
  catch (error) {
    notice("BLOCKED · " + error.message, true);
    if (approval) {
      try { approval = await api(`/api/approvals/${approval.approval_id}`); }
      catch { /* Preserve the decision evidence if the server is unreachable. */ }
    }
  } finally {
    busy = false;
    renderApproval();
  }
}

$("approve").addEventListener("click", () => action(async () => {
  approval = await api("/api/approvals", "POST", proposal);
  notice("APPROVAL CREATED · The server recorded your approval and froze the action values.");
}));

$("reject").addEventListener("click", () => action(async () => {
  approval = approval
    ? await api(`/api/approvals/${approval.approval_id}/reject`, "POST")
    : await api("/api/proposal/reject", "POST", proposal);
  notice("REJECTED · The decision was saved. No action was executed.");
}));

$("execute").addEventListener("click", () => action(async () => {
  await api(`/api/execute/${approval.approval_id}`, "POST");
  approval = await api(`/api/approvals/${approval.approval_id}`);
  await refreshOutbox();
  notice("EXECUTION SUCCESSFUL · The approved action is now recorded in the SQLite outbox.");
}));

$("reset").addEventListener("click", () => action(async () => {
  proposal = await api("/api/reset", "POST");
  approval = null;
  locallyExpired = false;
  $("attack-results").replaceChildren();
  renderProposal();
  await refreshOutbox();
  notice("Demo reset. The local approval and outbox records have been cleared.");
}));

$("refresh").addEventListener("click", () => action(async () => {
  if (approval) approval = await api(`/api/approvals/${approval.approval_id}`);
  await refreshOutbox();
  notice("Evidence refreshed from SQLite.");
}));

function renderAttack(kind, result, error) {
  $("attack-empty")?.remove();
  const id = "result-" + kind;
  let card = $(id);
  if (!card) {
    card = document.createElement("article");
    card.id = id;
    $("attack-results").append(card);
  }
  card.className = "attack-result " + (error ? "fail" : result.status.toLowerCase());
  card.replaceChildren();
  const heading = document.createElement("h3");
  heading.textContent = attackNames[kind];
  card.append(heading);
  if (error) {
    const message = document.createElement("p");
    message.textContent = "ERROR · " + error + " No PASS result was recorded.";
    card.append(message);
    return;
  }
  const fields = document.createElement("dl");
  fields.className = "evidence-fields";
  for (const [label, value] of [
    ["Expected", "BLOCK"],
    ["Actual", result.blocked ? "BLOCKED" : "NOT BLOCKED AS EXPECTED"],
    ["Unauthorized side effect", result.unauthorized_effect ? "DETECTED" : "NONE"],
    ["Result", result.status],
    ["HTTP result", `${result.http_status} · ${result.response_detail}`],
    ["Approval ID", result.approval_id],
    ["Outbox rows", `${result.outbox_before.length} before → ${result.outbox_after.length} after (this approval)`],
    ["Attack created a row", result.side_effect_created ? "YES" : "NO"],
  ]) {
    const group = document.createElement("div");
    const term = document.createElement("dt");
    const valueElement = document.createElement("dd");
    term.textContent = label;
    valueElement.textContent = value;
    group.append(term, valueElement);
    fields.append(group);
  }
  card.append(fields);
  if (result.legitimate_setup_executed) {
    const note = document.createElement("p");
    note.className = "description";
    note.textContent = "Replay setup created one legitimate SENT row. The replay attempt must add nothing.";
    card.append(note);
  }
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = "Inspect approved action, mutated request & evidence";
  details.append(summary);
  for (const [label, value] of [
    ["Original approved action", result.original_approved_action],
    ["Mutated execution request", {action: result.mutated_action}],
    ["SQLite outbox before attack", result.outbox_before],
    ["SQLite outbox after attack", result.outbox_after],
  ]) {
    const title = document.createElement("h4");
    title.textContent = label;
    const code = document.createElement("pre");
    code.textContent = JSON.stringify(value, null, 2);
    details.append(title, code);
  }
  card.append(details);
}

document.querySelectorAll("[data-attack]").forEach((button) => {
  button.addEventListener("click", () => action(async () => {
    const kind = button.dataset.attack;
    notice(`Running ${attackNames[kind]} through the local execution API…`);
    try {
      const result = await api(`/api/attacks/${kind}`, "POST");
      renderAttack(kind, result);
      await refreshOutbox();
      notice(`${attackNames[kind]} · ${result.status} · ${result.response_detail}`, result.status !== "PASS");
    } catch (error) {
      renderAttack(kind, null, error.message);
      notice("Attack simulator error · " + error.message, true);
    }
  }));
});

setInterval(() => {
  const expired = approval?.status === "APPROVED" && Date.now() >= Date.parse(approval.expires_at);
  if (expired && !locallyExpired) {
    locallyExpired = true;
    renderApproval();
    notice("BLOCKED · Approval expired. Reset the demo to start again.", true);
  }
}, 1000);

action(async () => {
  proposal = await api("/api/proposal", "POST");
  renderProposal();
  await refreshOutbox();
  notice("Ready for review. All actions stay in this local sandbox.");
});
