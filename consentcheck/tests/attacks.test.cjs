const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const scenarios = ['recipient-tampering', 'misleading-approval'];
function harness(outcome = 'PREVENTED', failPost = false, readOnly = false) {
  const nodes = new Map(), storage = new Map(), calls = [];
  const make = () => ({textContent: '', children: [], handlers: {}, append(n) {this.children.push(n);},
    replaceChildren() {this.children = [];}, addEventListener(e, fn) {this.handlers[e] = fn;}});
  const get = id => {if (readOnly && id.startsWith('simulate-')) return null; if (!nodes.has(id)) nodes.set(id, make()); return nodes.get(id);};
  let latest = null, count = 0, summaryRefreshes = 0;
  const context = {AbortController, setTimeout, clearTimeout, crypto,
    document: {getElementById:get, createElement:make},
    sessionStorage: {getItem:k=>storage.get(k), setItem:(k,v)=>storage.set(k,v), removeItem:k=>storage.delete(k)},
    window:{addEventListener(){}, refreshSecurityCenter(){summaryRefreshes++;}},
    fetch: async (url, options={}) => {
      calls.push({url,...options});
      if (options.method === 'POST') {
        if (failPost) throw new Error('Connection lost');
        count++;
        latest = {scenario:url.split('/').pop(),name:'Synthetic attack',run_id:'run-1',created_at:'test time',
          outcome,prevented:outcome==='PREVENTED',unauthorized_execution:outcome==='FAILED',
          protected:{rejected:true,mismatch_verified:outcome==='PREVENTED'},legitimate_control:{executed:true}, explanation:'Evidence'};
        return {ok:true,json:async()=>latest};
      }
      return {ok:true,json:async()=>({scenarios:scenarios.map(s=>({scenario:s,attacks:latest?.scenario===s?count:0,
        prevented:latest?.scenario===s && outcome==='PREVENTED'?count:0,latest:latest?.scenario===s?latest:null}))})};
    }};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/attacks.js'),'utf8'),context);
  return {get,calls,storage,summaryRefreshes:()=>summaryRefreshes};
}
const settle = () => new Promise(resolve=>setImmediate(resolve));
const text = n => n.textContent+' '+n.children.map(text).join(' ');
for (const scenario of scenarios) test(`${scenario}: correct endpoint, duplicate guard, immediate result and counts`, async()=>{
  const h = harness(); await settle();
  assert.equal(h.get(`attacks-${scenario}`).textContent,'Not tested');
  const button = h.get(`simulate-${scenario}`);
  const first = button.handlers.click();
  assert.equal(button.disabled,true);
  await button.handlers.click();
  await first;
  const posts = h.calls.filter(c=>c.method==='POST');
  assert.equal(posts.length,1);
  assert.equal(posts[0].url,`/api/security/attacks/${scenario}`);
  assert.equal(h.get(`attacks-${scenario}`).textContent,'1');
  assert.equal(h.get(`prevented-${scenario}`).textContent,'1');
  assert.equal(h.get(`status-${scenario}`).textContent,'Protected');
  assert(text(h.get(`result-${scenario}`)).includes('PREVENTED'));
  assert.equal(h.summaryRefreshes(),1);
  await h.get('security-refresh').handlers.click();
  assert.equal(h.calls.filter(c=>c.method==='POST').length,1);
});
for (const outcome of ['FAILED','INCOMPLETE']) test(`${outcome} is never green or counted as prevented`,async()=>{
  const h=harness(outcome); await settle();
  await h.get('simulate-recipient-tampering').handlers.click();
  assert.equal(h.get('prevented-recipient-tampering').textContent,'0');
  assert.notEqual(h.get('status-recipient-tampering').textContent,'Protected');
  assert(!h.get('result-recipient-tampering').className.includes('prevented'));
});
test('transport errors preserve idempotency token for retry',async()=>{
  const h=harness('PREVENTED',true); await settle();
  await h.get('simulate-recipient-tampering').handlers.click();
  await h.get('simulate-recipient-tampering').handlers.click();
  const posts=h.calls.filter(c=>c.method==='POST');
  assert.equal(posts[0].body,posts[1].body);
  assert(h.get('attack-error').textContent.includes('Retry uses the same request ID'));
});

test('dashboard without simulation controls still loads and refreshes evidence', async () => {
  const html = fs.readFileSync(path.join(__dirname, '../static/dashboard.html'), 'utf8');
  assert(!html.includes('id="simulate-'));
  const h = harness('PREVENTED', false, true);
  await settle();
  await h.get('security-refresh').handlers.click();
  assert.equal(h.get('attacks-recipient-tampering').textContent, 'Not tested');
  assert.equal(h.calls.filter(c=>c.method==='POST').length, 0);
  assert(h.calls.length >= 2);
});
