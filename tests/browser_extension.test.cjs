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
    url: 'https://example.com/task?secret=PRIVATE#PRIVATE' });
  assert.equal(sent[0].session_id, 'old');
  assert.equal(sent[0].url, 'https://example.com/task');
  assert.equal(sent[0].timestamp, '2026-10-07T00:00:09Z');
});

test('continuous mutations flush within the first 450 ms without raw content', () => {
  const callbacks = [];
  const timers = new Map();
  const messages = [];
  let timerId = 0;
  class Element {}
  const target = Object.assign(new Element(), { id: 'PRIVATE-ID' });
  vm.runInNewContext(fs.readFileSync('browser-extension/content.js', 'utf8'), {
    URL, Date, Set, Element, Node: { ELEMENT_NODE: 1 },
    window: { location: new URL('https://example.com/task?q=PRIVATE-QUERY#secret') },
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
  assert.equal(event.url, 'https://example.com/task');
  assert.ok(!JSON.stringify(event).includes('PRIVATE'));
});
