(() => {
  const scenarios = ['recipient-tampering', 'misleading-approval'];
  const running = new Set();
  const get = (id) => document.getElementById(id);
  let generation = 0;
  function resultView(scenario, result) {
    const container = get(`result-${scenario}`);
    container.replaceChildren();
    if (!result) return;
    // Green requires the actual returned evidence, not just a 200 or a label.
    const verified = result.outcome === 'PREVENTED' && result.prevented === true &&
      result.unauthorized_execution === false && result.protected?.mismatch_verified === true &&
      result.protected?.rejected === true && result.legitimate_control?.executed === true;
    const outcome = verified ? 'PREVENTED' : result.outcome === 'FAILED' ? 'FAILED' : 'INCOMPLETE';
    container.className = `attack-result ${outcome.toLowerCase()}`;
    const add = (tag, text) => { const el = document.createElement(tag); el.textContent = text; container.append(el); };
    add('strong', `${result.name}: ${outcome}`);
    if (result.approved_action) {
      add('p', `Approved / displayed: ${result.displayed_action.recipient} · ${result.displayed_action.body}`);
      add('p', `Attempted: ${result.attempted_action.recipient} · ${result.attempted_action.body}`);
      add('p', `Vulnerable baseline: ${result.vulnerable.accepted ? 'accepted the mismatch (comparison only; no send)' : 'did not accept'}.`);
      add('p', `PHANTOM LOOP: ${result.protected.reason}`);
    }
    add('p', `Unauthorized simulated execution: ${result.unauthorized_execution === false ? 'No — verified' : result.unauthorized_execution === true ? 'Yes — isolated test database only' : 'Unverified'}. Legitimate control: ${result.legitimate_control?.executed ? 'executed through the gate' : 'not verified'}.`);
    add('p', result.explanation);
    add('small', `Synthetic scenario, not a live Gemini attack · ${result.created_at} · Run ${result.run_id}`);
  }
  function render(data) {
    for (const row of data.scenarios) {
      if (!scenarios.includes(row.scenario)) continue;
      get(`attacks-${row.scenario}`).textContent = row.attacks ? String(row.attacks) : 'Not tested';
      get(`prevented-${row.scenario}`).textContent = row.attacks ? String(row.prevented) : 'Not tested';
      const latest = row.latest;
      const verified = latest?.outcome === 'PREVENTED' && latest.prevented === true && latest.unauthorized_execution === false && latest.protected?.mismatch_verified === true && latest.protected?.rejected === true && latest.legitimate_control?.executed === true;
      const status = get(`status-${row.scenario}`);
      status.textContent = !latest ? 'Planned — not tested yet' : verified ? 'Protected' : latest.outcome === 'FAILED' ? 'Failed' : 'Incomplete';
      status.className = verified ? 'attack-prevented' : latest?.outcome === 'FAILED' ? 'attack-failed' : 'planned';
      resultView(row.scenario, latest);
    }
  }
  async function fetchJson(url, options) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, {...options, signal: controller.signal, cache: 'no-store'});
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Simulation service unavailable.');
      return data;
    } finally { clearTimeout(timeout); }
  }
  async function refresh() {
    const current = ++generation;
    try {
      const data = await fetchJson('/api/security/attacks');
      if (current === generation) { render(data); get('attack-error').textContent = ''; }
    } catch { get('attack-error').textContent = 'Could not refresh simulation results. Displayed records may be stale.'; }
  }
  async function simulate(scenario) {
    if (running.has(scenario)) return;
    running.add(scenario);
    const button = get(`simulate-${scenario}`), label = button.textContent;
    button.disabled = true; button.textContent = 'Running synthetic scenario…';
    get('attack-error').textContent = '';
    const storageKey = `attack-request-${scenario}`;
    try {
      const requestId = sessionStorage.getItem(storageKey) || crypto.randomUUID().replaceAll('-', '');
      sessionStorage.setItem(storageKey, requestId);
      const result = await fetchJson(`/api/security/attacks/${scenario}`, {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({request_id: requestId})});
      ++generation;
      resultView(scenario, result);
      sessionStorage.removeItem(storageKey);
      await refresh();
      window.refreshSecurityCenter?.();
    } catch (error) {
      get('attack-error').textContent = `Simulation response unavailable: ${error.message}. Retry uses the same request ID to avoid duplicates.`;
    } finally { running.delete(scenario); button.disabled = false; button.textContent = label; }
  }
  scenarios.forEach((scenario) => get(`simulate-${scenario}`)?.addEventListener('click', () => simulate(scenario)));
  get('security-refresh').addEventListener('click', refresh);
  window.addEventListener('focus', refresh);
  refresh();
})();
