const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');

test('delayed pre-stop browser event retains the original session after switching', async () => {
  const listener = { addListener() {} };
  let current = { id: 'old', started_at: '2026-10-07T00:00:00Z' };
  const sent = [];
  const context = vm.createContext({ URL, Date, Map,
    navigator: { userAgent: 'Chrome/151.0.0.0' },
    chrome: { runtime: { onMessage: listener, onInstalled: listener,
      getManifest: () => ({ version: '0.2.4' }) },
      action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {}, setTitle: async () => {} },
      webNavigation: { onCommitted: listener, onHistoryStateUpdated: listener },
      tabs: { onRemoved: listener }, commands: { onCommand: listener } },
    fetch: async (url, options) => {
      if (url.endsWith('/api/active-session')) return { ok: true, json: async () => ({ session: current }) };
      if (url.endsWith('/api/events')) sent.push(JSON.parse(options.body));
      return { ok: true, status: 201 };
    },
  });
  vm.runInContext(fs.readFileSync('browser-extension/background.js', 'utf8'), context);
  await context.getActiveSession(true);
  current = { id: 'new', started_at: '2026-10-07T00:00:10Z' };
  await context.getActiveSession(true);
  await context.sendEvent({ type: 'navigation', timestamp: '2026-10-07T00:00:09Z',
    url: 'https://example.com/task?q=FAKE-QUERY#section' });
  assert.equal(sent[0].session_id, 'old');
  // 0.3.3 records the full URL, query and hash included.
  assert.equal(sent[0].url, 'https://example.com/task?q=FAKE-QUERY#section');
  assert.equal(sent[0].timestamp, '2026-10-07T00:00:09Z');
});

test('continuous mutations flush within the first 450 ms with the full page URL', () => {
  const callbacks = [];
  const timers = new Map();
  const messages = [];
  let timerId = 0;
  class Element {}
  const target = Object.assign(new Element(), { id: 'DOM-ID' });
  vm.runInNewContext(fs.readFileSync('browser-extension/content.js', 'utf8'), {
    URL, Date, Set, Element, Node: { ELEMENT_NODE: 1 },
    window: { location: new URL('https://example.com/task?q=FAKE-QUERY#section') },
    document: { title: 'Task', readyState: 'complete', documentElement: { lang: 'en' } },
    chrome: { runtime: { sendMessage: (message) => { messages.push(message); return Promise.resolve(); } } },
    MutationObserver: class { constructor(callback) { callbacks.push(callback); } observe() {} },
    setTimeout: (callback) => { timers.set(++timerId, callback); return timerId; },
    clearTimeout: (id) => timers.delete(id),
  });
  const mutation = { target, type: 'characterData', addedNodes: [], removedNodes: [] };
  callbacks[0]([mutation]);
  const firstTimer = [...timers.keys()][0];
  callbacks[0]([mutation]);
  assert.equal([...timers.keys()][0], firstTimer);
  timers.get(firstTimer)();
  const event = messages.at(-1).event;
  assert.equal(event.payload.character_data_changes, 2);
  assert.equal(event.url, 'https://example.com/task?q=FAKE-QUERY#section');
  // DOM changes are still summarized as counts, not raw paths or text.
  assert.ok(!JSON.stringify(event).includes('DOM-ID'));
});

test('a study frame re-sends an unchanged title until the background accepts it', async () => {
  const messages = [];
  const replies = [{ accepted: false }, { accepted: true }];
  let tick;
  const window = { location: new URL('http://partner.example/koref/web/user/'), addEventListener() {} };
  window.top = {};
  vm.runInNewContext(fs.readFileSync('browser-extension/content.js', 'utf8'), {
    URL, Date, Set, Element: class {}, Node: { ELEMENT_NODE: 1 }, window,
    document: { title: '한국에너지재단', readyState: 'complete', documentElement: { lang: 'ko' } },
    chrome: { runtime: { sendMessage: (message) => { messages.push(message); return Promise.resolve(message.kind === 'recorder-frame-title' ? replies.shift() : undefined); } } },
    MutationObserver: class { observe() {} },
    setTimeout: () => 0, clearTimeout: () => {}, setInterval: (callback) => { tick = callback; return 1; },
  });
  const titles = () => messages.filter(m => m.kind === 'recorder-frame-title').length;
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(titles(), 1);
  tick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(titles(), 2, 'sent again after a refused report');
  tick();
  tick();
  assert.equal(titles(), 2, 'not sent again once accepted and unchanged');
  assert.equal(messages.find(m => m.kind === 'recorder-frame-title').title, '한국에너지재단');
});
