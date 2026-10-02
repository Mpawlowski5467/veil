// Execute the shipped inline script with a small DOM stub; no browser dependencies.
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const html = readFileSync(join(__dirname, '../src/veil/review.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];

function element(tag) {
  return {
    tag, children: [], textContent: '', disabled: false,
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    setAttribute() {},
    add(option) { this.children.push(option); },
  };
}

async function page(url, fetchImpl) {
  const nodes = Object.fromEntries(['status', 'reviews', 'refresh'].map(id => [id, element(id)]));
  const requests = [];
  const history = [];
  const context = vm.createContext({
    location: new URL(url),
    history: { replaceState(...args) { history.push(args); } },
    document: { getElementById: id => nodes[id], createElement: element },
    Option: function (text, value) { return { text, value }; },
    fetch: async (...args) => {
      requests.push(args);
      assert.ok(fetchImpl, 'this page must not make any request');
      return fetchImpl(...args);
    },
  });
  vm.runInContext(script, context);
  // Let the initial refresh and its promise callbacks finish.
  await new Promise(setImmediate);
  return { nodes, requests, history };
}

// The page exchanges its one-time launch code once, then uses the session.
function withSession(impl) {
  return (url, options) => url === '/session'
    ? { ok: true, status: 200, json: async () => ({ token: 'fictional-session' }) }
    : impl(url, options);
}

test('direct file preview gives launch instructions without fetch or history changes', async () => {
  const { nodes, requests, history } = await page('file:///tmp/review.html');
  assert.match(nodes.status.textContent, /file is a preview/);
  assert.match(nodes.status.textContent, /Run veil review/);
  assert.equal(nodes.refresh.disabled, true);
  assert.equal(requests.length, 0);
  assert.equal(history.length, 0);
});

test('a reloaded page without its token asks for a fresh private link without fetching', async () => {
  const { nodes, requests, history } = await page('http://127.0.0.1:12345/');
  assert.match(nodes.status.textContent, /missing its private access link/);
  assert.equal(nodes.refresh.disabled, true);
  assert.equal(requests.length, 0);
  assert.equal(history.length, 0);
});

test('a copy on a different origin never sends the capability', async () => {
  const { requests } = await page('https://example.com/#fictional-launch');
  assert.equal(requests.length, 0);
});

test('connection failures explain recovery and clear previously displayed findings', async () => {
  let offline = false;
  const { nodes } = await page('http://127.0.0.1:12345/#fictional-launch', withSession(() => {
    if (offline) throw new TypeError('Failed to fetch');
    return { ok: true, status: 200, json: async () => ({ reviews: [] }) };
  }));
  nodes.reviews.append(element('old private value'));
  offline = true;
  await nodes.refresh.onclick();
  assert.match(nodes.status.textContent, /Cannot reach the local review server/);
  assert.match(nodes.status.textContent, /Run veil review/);
  assert.equal(nodes.reviews.children.length, 0);
});

test('an already-used launch link asks for a new private page', async () => {
  const { nodes, requests } = await page('http://127.0.0.1:12345/#fictional-launch', () => ({ ok: false, status: 403 }));
  assert.match(nodes.status.textContent, /already used or has expired/);
  assert.match(nodes.status.textContent, /Run veil review/);
  assert.deepEqual(requests.map(([url]) => url), ['/session']);
});

test('an unreachable server during startup explains recovery', async () => {
  const { nodes } = await page('http://127.0.0.1:12345/#fictional-launch', () => {
    throw new TypeError('Failed to fetch');
  });
  assert.match(nodes.status.textContent, /Cannot reach the local review server/);
  assert.match(nodes.status.textContent, /Run veil review/);
});

test('a refused session after startup says the link is no longer accepted', async () => {
  const { nodes } = await page('http://127.0.0.1:12345/#fictional-launch', withSession(() => ({ ok: false, status: 403 })));
  assert.match(nodes.status.textContent, /link is no longer accepted/);
  assert.match(nodes.status.textContent, /Run veil review/);
});

test('a refresh during startup reuses the single launch exchange', async () => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const { nodes, requests } = await page('http://127.0.0.1:12345/#fictional-launch', (url) => url === '/session'
    ? pending
    : { ok: true, status: 200, json: async () => ({ reviews: [] }) });
  const clicked = nodes.refresh.onclick();
  release({ ok: true, status: 200, json: async () => ({ token: 'fictional-session' }) });
  await clicked;
  await new Promise(setImmediate);
  assert.deepEqual(requests.map(([url]) => url), ['/session', '/data', '/data']);
  assert.match(nodes.status.textContent, /No requests awaiting review/);
});

test('gateway errors explain that both the gateway and original request need checking', async () => {
  const { nodes } = await page('http://127.0.0.1:12345/#fictional-launch', withSession(() => ({ ok: false, status: 400 })));
  assert.match(nodes.status.textContent, /gateway is running/);
  assert.match(nodes.status.textContent, /retry your original request/);
});

test('authenticated review still loads, saves choices, and keeps the token out of the URL', async () => {
  const finding = { index: 0, kind: 'PASSWORD', reason: 'fictional test', value: '<script>fictional</script>', choice: null };
  const review = { id: 'fictional-id', remaining_seconds: 600, complete: false, findings: [finding] };
  const { nodes, requests, history } = await page('http://127.0.0.1:12345/#fictional-launch', withSession((_url, options) => {
    if (options.method === 'POST') {
      assert.deepEqual(JSON.parse(options.body), { id: review.id, index: 0, choice: 'PASSWORD' });
      finding.choice = 'PASSWORD';
      review.complete = true;
    }
    return { ok: true, status: 200, json: async () => ({ reviews: [review] }) };
  }));
  assert.deepEqual(history, [[null, '', '/']]);
  assert.equal(nodes.refresh.disabled, false);
  assert.match(nodes.status.textContent, /1 finding needs/);
  const card = nodes.reviews.children[0].children[2];
  assert.equal(card.children[1].children[1].textContent, finding.value);
  const select = card.children[2];
  const save = card.children[3];
  assert.equal(save.disabled, true);
  select.value = 'PASSWORD';
  select.onchange();
  assert.equal(save.disabled, false);
  await save.onclick();
  assert.match(nodes.status.textContent, /All choices saved/);
  assert.equal(requests.length, 4);
  const [[sessionUrl, sessionOptions], ...rest] = requests;
  assert.equal(sessionUrl, '/session');
  assert.equal(sessionOptions.method, 'POST');
  assert.deepEqual({ ...sessionOptions.headers }, { 'X-Veil-Review': 'fictional-launch' });
  assert.equal(sessionOptions.credentials, 'omit');
  assert.equal(sessionOptions.cache, 'no-store');
  assert.equal(sessionOptions.body, undefined);
  for (const [url, options] of rest) {
    assert.equal(url, '/data');
    assert.equal(options.headers['X-Veil-Review'], 'fictional-session');
    assert.equal(options.cache, 'no-store');
  }
});
