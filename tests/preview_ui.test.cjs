// Run the shipped script without browser dependencies or external services.
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const html = readFileSync(join(__dirname, '../src/veil/preview.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];

function element(tag) {
  return {
    tag, children: [], textContent: '', value: '', disabled: true,
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    addEventListener(name, callback) { this['on' + name] = callback; },
    focus() {},
  };
}
const tick = () => new Promise(setImmediate);
async function page(url, fetchImpl) {
  const nodes = Object.fromEntries(['input', 'output', 'status', 'run', 'example', 'clear', 'findings', 'masks', 'restored', 'roundtrip'].map(id => [id, element(id)]));
  const requests = [], history = [];
  const context = vm.createContext({
    location: new URL(url),
    history: { replaceState(...args) { history.push(args); } },
    document: { getElementById: id => nodes[id], createElement: element },
    AbortController, setTimeout, clearTimeout,
    fetch: async (...args) => { requests.push(args); assert.ok(fetchImpl); return fetchImpl(...args); },
  });
  vm.runInContext(script, context);
  await tick();
  return { nodes, requests, history };
}
function ok(body) { return { ok: true, json: async () => body }; }
const result = { masked: '[EMAIL_1]', restored: 'fictional@example.org', masks: [{ placeholder: '[EMAIL_1]', reason: 'Privacy pattern' }], findings: [], unresolved: 0 };

for (const url of ['file:///tmp/preview.html', 'http://127.0.0.1:1234/', 'https://example.org/#token']) {
  test('unsupported or expired URL makes no requests: ' + url, async () => {
    const { nodes, requests, history } = await page(url);
    assert.match(nodes.status.textContent, /Start veil preview/);
    assert.equal(nodes.run.disabled, true);
    assert.equal(requests.length, 0);
    assert.equal(history.length, 0);
  });
}

test('typing does not transmit; explicit run masks; editing clears results and decisions', async () => {
  const { nodes, requests, history } = await page('http://127.0.0.1:1234/#token', (_, options) => ok(options.method === 'POST' ? result : { choices: ['PASSWORD', 'IGNORE'] }));
  assert.deepEqual(history, [[null, '', '/']]);
  nodes.input.value = result.restored; nodes.input.oninput();
  assert.equal(requests.length, 1);
  await nodes.run.onclick();
  assert.equal(nodes.output.textContent, result.masked);
  assert.equal(nodes.restored.textContent, result.restored);
  assert.match(nodes.status.textContent, /rules can miss/);
  nodes.input.oninput();
  assert.equal(nodes.restored.textContent, '');
  assert.equal(nodes.masks.children.length, 0);
  assert.equal(requests.length, 2);
  nodes.clear.onclick();
  assert.equal(nodes.input.value, '');
  for (const [url, options] of requests) {
    assert.equal(url, '/data');
    assert.equal(options.headers['X-Veil-Review'], 'token');
    assert.equal(options.credentials, 'omit');
    assert.equal(options.cache, 'no-store');
  }
});

test('review choices render text safely and apply only to the current text', async () => {
  const value = '<script>fictional</script>';
  const finding = { index: 0, value, reason: 'Possible credential', kind: 'PASSWORD', choice: null };
  const { nodes, requests } = await page('http://127.0.0.1:1234/#token', (_, options) => {
    if (options.method !== 'POST') return ok({ choices: ['PASSWORD', 'IGNORE'] });
    const payload = JSON.parse(options.body);
    finding.choice = payload.choices['0'] || null;
    return ok({ ...result, findings: [finding], unresolved: finding.choice ? 0 : 1 });
  });
  nodes.input.value = value;
  await nodes.run.onclick();
  const card = nodes.findings.children[0];
  assert.equal(card.children[0].textContent, value);
  assert.match(nodes.status.textContent, /still need your decision/);
  const select = card.children[3]; select.value = 'PASSWORD'; select.onchange();
  await tick();
  assert.equal(JSON.parse(requests.at(-1)[1].body).choices['0'], 'PASSWORD');
  nodes.input.oninput();
  await nodes.run.onclick();
  assert.deepEqual(JSON.parse(requests.at(-1)[1].body).choices, {});
});

test('failed requests remove stale output and explain recovery', async () => {
  let offline = false;
  const { nodes } = await page('http://127.0.0.1:1234/#token', (_, options) => {
    if (offline) throw new TypeError('Failed to fetch');
    return ok(options.method === 'POST' ? result : { choices: [] });
  });
  await nodes.run.onclick(); offline = true;
  await nodes.run.onclick();
  assert.equal(nodes.restored.textContent, '');
  assert.match(nodes.status.textContent, /Keep veil preview running/);
  assert.equal(nodes.run.disabled, false);
});

test('an in-flight result never replaces edited or cleared text', async () => {
  let finish;
  const { nodes } = await page('http://127.0.0.1:1234/#token', (_, options) => options.method === 'POST' ? new Promise(resolve => { finish = resolve; }) : ok({ choices: [] }));
  const pending = nodes.run.onclick();
  nodes.clear.onclick();
  finish(ok(result));
  await pending;
  assert.equal(nodes.restored.textContent, '');
  assert.match(nodes.status.textContent, /cleared/);
});
