const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');

const flush = () => new Promise(resolve => setImmediate(resolve));
function backgroundHarness() {
  const storage = {}, events = [], handlers = {}, tabs = new Map();
  const session = { id: 'free', status: 'draft', target_url: 'https://copies.fixture.test/start',
    study: { kind: 'free', tester: 'T01' } };
  tabs.set(7, { id: 7, windowId: 8, url: 'http://127.0.0.1:8765/' });
  let create = async () => ({ id: 90, tabs: [{ id: 91, windowId: 90 }] });
  const noop = { addListener() {} };
  const chrome = {
    runtime: { id: 'extension', getManifest: () => ({ version: '0.6.1' }), onInstalled: noop,
      onMessage: { addListener: fn => { handlers.message = fn; } } },
    storage: { session: { get: async key => ({ [key]: storage[key] }),
      set: async value => Object.assign(storage, structuredClone(value)), remove: async key => delete storage[key] } },
    tabs: { get: async id => tabs.get(id), query: async () => [], remove: async id => tabs.delete(id),
      update: async () => {}, onRemoved: noop, onCreated: { addListener: fn => { handlers.created = fn; } } },
    windows: { create: (...args) => create(...args), update: async () => {}, remove: async () => {} },
    webNavigation: { onCommitted: noop, onHistoryStateUpdated: noop,
      onErrorOccurred: { addListener: fn => { handlers.error = fn; } } },
    action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {}, setTitle: async () => {} },
  };
  const fetch = async (url, options = {}) => {
    const path = new URL(url).pathname;
    if (path === '/api/active-session') return { ok: true, json: async () => ({ session: session.status === 'active' ? structuredClone(session) : null }) };
    if (path === '/api/events') { events.push(JSON.parse(options.body)); return { ok: true, status: 201, json: async () => ({}) }; }
    if (path.endsWith('/start')) { session.status = 'active'; session.started_at = new Date().toISOString(); }
    if (path.endsWith('/stop')) { session.status = 'abandoned'; Object.assign(session, JSON.parse(options.body)); }
    return { ok: true, json: async () => ({ session: structuredClone(session) }) };
  };
  const context = vm.createContext({ URL, Date, Map, Set, WeakMap, Promise, chrome, fetch,
    navigator: { userAgent: 'Chrome/151' }, setTimeout() {} });
  vm.runInContext(fs.readFileSync('browser-extension/background.js', 'utf8'), context);
  const message = (data, sender = { url: 'http://127.0.0.1:8765/', frameId: 0, tab: { id: 7 } }) =>
    new Promise(resolve => handlers.message(data, sender, resolve));
  return { storage, events, session, handlers, tabs,
    setCreate(fn) { create = fn; },
    command: (action, tabId = 7) => message({ kind: 'a11y-study-command', action, sessionId: 'free' },
      { url: 'http://127.0.0.1:8765/', frameId: 0, tab: { id: tabId } }),
    async created(tab) { tabs.set(tab.id, tab); await handlers.created(tab); },
    event(tabId, text) { return message({ kind: 'recorder-event', event: { type: 'focus',
      url: 'https://copies.fixture.test/start', payload: { name: text } } },
      { tab: { id: tabId }, frameId: 0, url: 'https://copies.fixture.test/start' }); },
  };
}

test('free launch verifies the returned window before sending first-load events', async () => {
  const app = backgroundHarness();
  app.setCreate(async () => {
    await app.created({ id: 200, windowId: 201 });
    await app.event(200, 'unrelated browsing');
    await app.created({ id: 91, windowId: 90 });
    await app.event(91, 'owned first page');
    assert.equal(app.events.length, 0, 'unconfirmed windows must not be recorded');
    return { id: 90, tabs: [{ id: 91, windowId: 90 }] };
  });
  await app.command('prepare');
  assert.equal((await app.command('start')).started, true);
  assert.ok(app.events.some(e => e.payload.name === 'owned first page'));
  assert.ok(!app.events.some(e => e.payload.name === 'unrelated browsing'));
  assert.deepEqual([...app.storage.a11yStudyOwnedTab.siteTabIds], [91]);
  assert.equal((await app.event(200, 'still unrelated')).recorded, false);
});

test('failed popup launch is a technical result and releases the active session', async () => {
  const app = backgroundHarness();
  app.setCreate(async () => { throw new Error('fixture popup failure'); });
  await app.command('prepare');
  assert.equal((await app.command('start')).reason, 'free_window_open_failed');
  assert.equal(app.session.status, 'abandoned');
  assert.equal(app.session.study_outcome, 'technical');
  assert.equal(app.session.entered_by, 'system');
  assert.equal(app.storage.a11yStudyOwnedTab, undefined);
});

test('only the owning dashboard can command a free task', async () => {
  const app = backgroundHarness();
  await app.command('prepare');
  assert.equal((await app.command('start', 9)).reason, 'wrong_dashboard_tab');
  assert.equal(app.session.status, 'draft');
});

test('concurrent owned tab creations keep both tabs while unrelated windows stay excluded', async () => {
  const app = backgroundHarness();
  await app.command('prepare'); await app.command('start');
  await Promise.all([app.created({ id: 92, windowId: 90 }), app.created({ id: 93, windowId: 94, openerTabId: 91 }),
    app.created({ id: 95, windowId: 96 })]);
  assert.deepEqual([...app.storage.a11yStudyOwnedTab.siteTabIds].sort(), [91, 92, 93]);
});

test('bounded initial-event overflow is recorded as an explicit metadata gap', async () => {
  const app = backgroundHarness();
  app.setCreate(async () => {
    await app.created({ id: 91, windowId: 90 });
    for (let n = 0; n < 130; n++) await app.event(91, 'initial-' + n);
    return { id: 90, tabs: [{ id: 91, windowId: 90 }] };
  });
  await app.command('prepare'); await app.command('start');
  assert.equal(app.events.filter(e => e.type === 'focus').length, 128);
  assert.equal(app.events.find(e => e.payload.kind === 'free_launch_buffer_overflow').payload.dropped_events, 2);
});

test('free navigation failures are recorded only for owned tabs without changing participant outcome', async () => {
  const app = backgroundHarness();
  await app.command('prepare'); await app.command('start');
  await app.handlers.error({ tabId: 91, frameId: 0, url: 'https://copies.fixture.test/start', error: 'net::ERR_NAME_NOT_RESOLVED' });
  await app.handlers.error({ tabId: 500, frameId: 0, url: 'https://unrelated.fixture.test/', error: 'net::ERR_FAILED' });
  const errors = app.events.filter(e => e.payload.kind === 'free_navigation_error');
  assert.equal(errors.length, 1);
  assert.equal(errors[0].payload.error, 'net::ERR_NAME_NOT_RESOLVED');
  assert.equal(app.session.status, 'active');
  assert.equal(app.session.study_outcome, undefined);
});

function freeUi({ resumeSession = null, failFirstEnd = false, failEndCount = 0, loseEndResponse = false, externallySaved = false } = {}) {
  const nodes = new Map(), windowHandlers = {}, documentHandlers = {}, intervals = new Map(), calls = [];
  let nextId = 0, endCalls = 0;
  let current = resumeSession;
  class Element {
    constructor() { this.hidden = false; this.value = ''; this.listeners = {}; this.children = []; }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    replaceChildren(...children) { this.children = children; this.replacements = (this.replacements || 0) + 1; }
    focus() { this.focused = true; }
  }
  const node = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const location = { origin: 'http://127.0.0.1:8765' };
  const window = { Study: { tester: () => 'T01' }, addEventListener: (name, fn) => { windowHandlers[name] = fn; },
    postMessage(data) {
      if (data.action === 'start') current.status = 'active';
      queueMicrotask(() => windowHandlers.message({ source: window, origin: location.origin,
        data: { kind: 'a11y-study-response', requestId: data.requestId, result: { ok: true } } }));
    } };
  const request = async (path, options = {}) => {
    calls.push(path);
    if (path === '/free-task/current') return { session: structuredClone(current) };
    if (path === '/study/T01/free') { current = { id: 'free', status: 'draft', study: { kind: 'free' }, started_at: new Date().toISOString() }; return { session: structuredClone(current) }; }
    if (path === '/sessions/free') return { session: structuredClone(current) };
    if (path.startsWith('/signals?')) return { seq: 1, signals: [{ kind: 'end', session_id: 'free', source: 'nvda_addon' }] };
    if (path.endsWith('/end-task')) {
      endCalls++; if ((failFirstEnd && endCalls === 1) || endCalls <= failEndCount) throw new Error('temporary error');
      current.status = externallySaved ? 'completed' : 'answering';
      if (loseEndResponse && endCalls === 1) throw new Error('response lost after commit');
      return { session: structuredClone(current) };
    }
    throw new Error('Unexpected request ' + path);
  };
  vm.runInNewContext(fs.readFileSync('static/free-task.js', 'utf8'), { window, location, request, HTMLElement: Element,
    document: { getElementById: node, addEventListener: (name, fn) => { documentHandlers[name] = fn; },
      createElement: () => new Element(), querySelectorAll: () => [], querySelector: () => null },
    crypto: { randomUUID: () => String(++nextId) }, Date, Number, Map, encodeURIComponent,
    setInterval(fn) { const id = ++nextId; intervals.set(id, fn); return id; }, clearInterval: id => intervals.delete(id),
    setTimeout(fn, delay) { if (delay === 250) fn(); return ++nextId; }, clearTimeout() {} });
  documentHandlers.DOMContentLoaded();
  return { node, calls, intervals, async ready() { windowHandlers['a11y-study-ready'](); await flush(); },
    async start() { node('study-free-form').listeners.submit({ preventDefault() {} }); await flush(); },
    async tick() { for (const fn of [...intervals.values()]) await fn(); await flush(); }, endCalls: () => endCalls };
}

test('a transient end request failure keeps a pending gesture and retries without another input', async () => {
  const app = freeUi({ failFirstEnd: true });
  await app.start(); await app.tick();
  assert.equal(app.endCalls(), 1); assert.equal(app.intervals.size, 1);
  await app.tick(); assert.equal(app.endCalls(), 2); assert.equal(app.intervals.size, 0);
  assert.equal(app.node('study-free-result').hidden, false);
});

test('answering free task is restored after the study completion screen is ready', async () => {
  const app = freeUi({ resumeSession: { id: 'free', status: 'answering', study: { kind: 'free' } } });
  assert.equal(app.calls.length, 0);
  await app.ready();
  assert.ok(app.calls.includes('/free-task/current'));
  assert.equal(app.node('study-free-result').hidden, false);
  assert.equal(app.node('study-free-result-heading').focused, true);
});

test('an end response lost after commit restores the result form without a repeated end request', async () => {
  const app = freeUi({ loseEndResponse: true });
  await app.start(); await app.tick();
  assert.equal(app.endCalls(), 1);
  assert.equal(app.intervals.size, 0);
  assert.equal(app.node('study-free-result').hidden, false);
  assert.equal(app.node('study-free-result-heading').focused, true);
});

test('reconciliation with an already saved task stops retrying without overwriting its outcome', async () => {
  const app = freeUi({ loseEndResponse: true, externallySaved: true });
  await app.start(); await app.tick();
  assert.equal(app.endCalls(), 1);
  assert.equal(app.intervals.size, 0);
  assert.equal(app.node('study-free-form').hidden, false);
  assert.ok(!app.calls.some(path => path.endsWith('/stop')));
});

test('repeated end-request failures do not repeat an identical NVDA alert', async () => {
  const app = freeUi({ failEndCount: 2 });
  await app.start(); await app.tick();
  const afterFirst = app.node('study-free-alert').replacements;
  assert.equal(afterFirst, 2, 'clear then one error announcement');
  await app.tick();
  assert.equal(app.endCalls(), 2);
  assert.equal(app.node('study-free-alert').replacements, afterFirst);
  await app.tick();
  assert.equal(app.endCalls(), 3);
  assert.equal(app.intervals.size, 0);
  assert.equal(app.node('study-free-result').hidden, false);
});
