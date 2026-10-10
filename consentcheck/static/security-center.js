(() => {
  const keys = ['reviewed', 'executed', 'blocked', 'pending'];
  const get = (id) => document.getElementById(id);
  let generation = 0;
  async function refreshSecurityCenter() {
    const request = ++generation;
    const grid = document.querySelector('.metrics');
    grid.setAttribute('aria-busy', 'true');
    get('security-refresh').disabled = true;
    get('metrics-status').textContent = 'Refreshing security records…';
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch('/api/security-summary', {cache: 'no-store', signal: controller.signal});
      if (!response.ok) throw new Error('Unavailable');
      const data = await response.json();
      if (data.engine_status !== 'active' || keys.some((key) => !Number.isInteger(data.metrics?.[key]) || data.metrics[key] < 0)) throw new Error('Invalid status');
      if (request !== generation) return;
      keys.forEach((key) => { get(`metric-${key}`).textContent = data.metrics[key].toLocaleString(); });
      get('protection-status').textContent = 'Protection status: Approval security engine active';
      get('protection-status').className = 'protection-status active';
      get('metrics-status').textContent = `Updated ${new Date().toLocaleTimeString()} · Legacy records excluded. Counts may overlap.`;
    } catch {
      if (request !== generation) return;
      keys.forEach((key) => { get(`metric-${key}`).textContent = '—'; });
      get('protection-status').textContent = 'Protection status: Unavailable — could not verify engine status';
      get('protection-status').className = 'protection-status unavailable';
      get('metrics-status').textContent = 'Security metrics unavailable. Refresh to retry.';
    } finally {
      clearTimeout(timeout);
      if (request === generation) { grid.setAttribute('aria-busy', 'false'); get('security-refresh').disabled = false; }
    }
  }
  window.refreshSecurityCenter = refreshSecurityCenter;
  get('security-refresh').addEventListener('click', refreshSecurityCenter);
  window.addEventListener('focus', refreshSecurityCenter);
  // Expiry and decisions in other tabs must also be reflected without a reload.
  setInterval(() => { if (!document.hidden) refreshSecurityCenter(); }, 30000);
  refreshSecurityCenter();
})();
