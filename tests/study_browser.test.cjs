const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');

const target = 'https://study.example/s/0123456789ab/index.html';
function background() {
  const listeners = [];
  const tabs = new Map();
  const storage = {};
  const calls = [];
  const sessions = { one: { id: 'one', status: 'draft', target_url: target,
    study: { preparation: [] } }, two: { id: 'two', status: 'draft',
    target_url: target, study: { preparation: [] } } };
  let active = null;
  let nextTab = 10;
  let page = { ready: true, setup: { method: 'operator_automation', actions: [] } };
  const listener = { addListener() {} };
  const context = vm.createContext({ URL, Date, Map, Promise,
    setTimeout: fn => fn(), navigator: { userAgent: 'Chrome/151' },
    chrome: { runtime: { id: 'extension', onMessage: { addListener: fn => listeners.push(fn) },
      onInstalled: listener, getManifest: () => ({ version: '0.2.5' }) },
      storage: { session: { get: async key => ({ [key]: storage[key] }),
        set: async data => Object.assign(storage, data), remove: async key => delete storage[key] } },
      tabs: { onRemoved: listener, get: async id => { if (!tabs.has(id)) throw Error('missing'); return tabs.get(id); },
        create: async options => { calls.push(['create', options]); const tab = { id: nextTab++, status: 'complete', ...options }; tabs.set(tab.id, tab); return tab; },
        update: async (id, options) => { calls.push(['update', id, options]); Object.assign(tabs.get(id), options); },
        remove: async id => { calls.push(['remove', id]); tabs.delete(id); },
        sendMessage: async (id, msg) => { calls.push(['page', id, msg]); return page; } },
      action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {}, setTitle: async () => {} },
      webNavigation: { onCommitted: listener, onHistoryStateUpdated: listener }, commands: { onCommand: listener } },
    fetch: async (url, options = {}) => {
      calls.push(['fetch', url, options]);
      if (url.endsWith('/api/active-session')) return { ok: true, json: async () => ({ session: active }) };
      const id = url.split('/')[5];
      if (url.endsWith('/start')) { sessions[id].status = 'active'; active = sessions[id]; }
      return { ok: true, json: async () => ({ session: sessions[id] }) };
    } });
  vm.runInContext(fs.readFileSync('browser-extension/background.js', 'utf8'), context);
  return { calls, tabs, sessions, chrome: context.chrome,
    restart() { listeners.length = 0; vm.runInNewContext(fs.readFileSync('browser-extension/background.js', 'utf8'), { ...context }); },
    setActive: value => active = value, setPage: value => page = value,
    async command(action, sessionId = 'one', sender = { url: 'http://127.0.0.1:8765/', frameId: 0 }, extra = {}) {
      let resolve; const result = new Promise(r => resolve = r);
      let handled = false;
      for (const fn of listeners) handled = fn({ kind: 'a11y-study-command', action, sessionId, ...extra }, sender, resolve) || handled;
      return handled ? result : { reason: 'unhandled' };
    } };
}

test('background only accepts the exact dashboard origin', async () => {
  const app = background();
  for (const url of ['https://127.0.0.1:8765/', 'http://localhost:8765/', 'https://evil.example/']) {
    assert.equal((await app.command('prepare', 'one', { url, frameId: 0 })).reason, 'untrusted_origin');
  }
  assert.equal(app.calls.filter(c => c[0] === 'create').length, 0);
});

test('prepare gets assignment from collector, opens fresh tab without opener, and is idempotent', async () => {
  const app = background();
  const first = await app.command('prepare', 'one', undefined, { url: 'https://evil.example/' });
  assert.equal(first.ready, true);
  assert.equal((await app.command('prepare')).tabId, first.tabId);
  const creates = app.calls.filter(c => c[0] === 'create');
  assert.equal(creates.length, 1);
  assert.equal(creates[0][1].url, target);
  assert.equal(creates[0][1].active, false);
  assert.equal('openerTabId' in creates[0][1], false);
});

test('an active session blocks preparation and never closes its tab', async () => {
  const app = background();
  await app.command('prepare');
  app.setActive(app.sessions.one);
  app.sessions.one.status = 'active';
  assert.equal((await app.command('prepare', 'two')).reason, 'active_session_conflict');
  assert.equal(app.calls.some(c => c[0] === 'remove'), false);
});

test('worker restart reuses saved ownership and does not repeat setup', async () => {
  const app = background();
  const prepared = await app.command('prepare');
  app.restart();
  const restored = await app.command('prepare');
  assert.equal(restored.tabId, prepared.tabId);
  assert.equal(app.calls.filter(c => c[0] === 'create').length, 1);
  assert.equal(app.calls.filter(c => c[0] === 'page').at(-1)[2].allowPreparation, false);
});

test('explicit prepare replaces an invalid owned draft tab while checks never reset it', async () => {
  for (const reason of ['preparation_required', 'preparation_failed', 'wrong_url', 'capture_missing']) {
    const app = background();
    const first = await app.command('prepare');
    app.setPage({ ready: false, reason });
    assert.equal((await app.command('check')).reason, reason);
    assert.equal(app.calls.some(c => c[0] === 'remove'), false);
    assert.equal(app.calls.filter(c => c[0] === 'page').at(-1)[2].allowPreparation, false);
    const create = app.chrome.tabs.create;
    app.chrome.tabs.create = async options => {
      app.setPage({ ready: true, setup: { method: 'operator_automation', actions: ['cart_add'] } });
      return create(options);
    };
    const recovered = await app.command('prepare');
    assert.equal(recovered.ready, true, reason);
    assert.notEqual(recovered.tabId, first.tabId);
    assert.equal(app.tabs.has(first.tabId), false);
    assert.equal(app.calls.filter(c => c[0] === 'page').at(-1)[2].allowPreparation, true);
    assert.equal(recovered.setup.actions[0], 'cart_add');
  }
});

test('recovery never closes a task whose recording became active', async () => {
  const app = background();
  await app.command('prepare');
  app.setPage({ ready: false, reason: 'preparation_required' });
  app.setActive({ ...app.sessions.one, status: 'active' });
  assert.equal((await app.command('prepare')).reason, 'already_started');
  assert.equal(app.calls.some(c => c[0] === 'remove'), false);
});

test('active assigned task cannot perform preparation or start again', async () => {
  const app = background();
  await app.command('prepare');
  app.sessions.one.status = 'active';
  app.setActive(app.sessions.one);
  const pageCount = app.calls.filter(c => c[0] === 'page').length;
  for (const action of ['prepare', 'check', 'start']) {
    assert.equal((await app.command(action)).reason, 'already_started');
  }
  assert.equal(app.calls.filter(c => c[0] === 'page').length, pageCount);
});

test('failed tab creation returns an explicit popup failure', async () => {
  const app = background();
  app.chrome.tabs.create = async () => { throw Error('blocked'); };
  assert.equal((await app.command('prepare')).reason, 'popup_blocked');
});

test('wrong URL and login prevent start; login focuses the owned tab', async () => {
  const app = background();
  const first = await app.command('prepare');
  app.tabs.get(first.tabId).url = 'https://study.example/other';
  assert.equal((await app.command('start')).reason, 'wrong_url');
  app.tabs.get(first.tabId).url = target;
  app.setPage({ ready: false, reason: 'login_required' });
  assert.equal((await app.command('check')).reason, 'login_required');
  assert.equal(app.calls.at(-1)[0], 'update');
  assert.equal(app.calls.some(c => c[1]?.endsWith?.('/start')), false);
});

test('login failure at hosted /access remains recognizable and cannot start recording', async () => {
  const app = background();
  const prepared = await app.command('prepare');
  app.tabs.get(prepared.tabId).url = 'https://study.example/access';
  app.setPage({ ready: false, reason: 'login_required' });
  assert.equal((await app.command('start')).reason, 'login_required');
  assert.equal(app.calls.some(c => c[1]?.endsWith?.('/start')), false);
  const page = studyPage({ login: true, url: 'https://study.example/access' });
  assert.equal((await page.run()).reason, 'login_required');
});

test('start checks readiness, sends explicit gate, then focuses task', async () => {
  const app = background();
  const prepared = await app.command('prepare');
  const result = await app.command('start');
  assert.equal(result.started, true);
  const posted = app.calls.find(c => c[1]?.endsWith?.('/start'));
  assert.equal(JSON.parse(posted[2].body).study_ready, true);
  assert.equal(app.calls.filter(c => c[0] === 'update').at(-1)[1], prepared.tabId);
  assert.equal((await app.command('close')).reason, 'session_not_finished');
});

test('successful start records the already loaded task as recorder metadata without reloading', async () => {
  const app = background();
  const prepared = await app.command('prepare');
  const update = app.chrome.tabs.update;
  app.chrome.tabs.update = async (...args) => {
    await update(...args);
    app.tabs.get(prepared.tabId).title = 'Current task title';
  };
  await app.command('start');
  const events = app.calls.filter(c => c[1]?.endsWith?.('/api/events'));
  assert.equal(events.length, 1);
  const event = JSON.parse(events[0][2].body);
  assert.equal(event.session_id, 'one');
  assert.equal(event.url, target);
  assert.equal(event.page_title, 'Current task title');
  assert.equal(event.source, 'browser');
  assert.equal(event.type, 'page_ready');
  assert.equal(event.payload.kind, 'study_recording_start');
  assert.equal(event.payload.evidence_kind, 'recorder_metadata');
  assert.equal(app.calls.filter(c => c[0] === 'create').length, 1);
  assert.equal(app.calls.findIndex(c => c === events[0]) > app.calls.findIndex(c => c[0] === 'update'), true);
});

test('only owned finished tabs close; a new idle task uses a new tab', async () => {
  const app = background();
  const first = await app.command('prepare');
  const second = await app.command('prepare', 'two');
  assert.equal(app.calls.filter(c => c[0] === 'create').length, 2);
  assert.equal(app.calls.filter(c => c[0] === 'remove')[0][1], first.tabId);
  app.sessions.two.status = 'completed';
  assert.equal((await app.command('close', 'two')).closed, true);
  assert.equal(app.tabs.has(second.tabId), false);
});

test('dashboard bridge ignores foreign window/origin and removes arbitrary payload fields', async () => {
  const handlers = {};
  const messages = [];
  const responses = [];
  const window = { location: new URL('http://127.0.0.1:8765/'),
    addEventListener: (name, fn) => handlers[name] = fn,
    postMessage: (...args) => responses.push(args) };
  vm.runInNewContext(fs.readFileSync('browser-extension/content.js', 'utf8'), {
    URL, window, chrome: { runtime: { sendMessage: async msg => { messages.push(msg); return { ready: true }; } } } });
  assert.equal(typeof handlers.message, 'function');
  const data = { kind: 'a11y-study-command', action: 'prepare', sessionId: 'one', requestId: 'req', url: 'https://evil.example/' };
  await handlers.message({ source: {}, origin: window.location.origin, data });
  await handlers.message({ source: window, origin: 'https://evil.example', data });
  assert.equal(messages.length, 0);
  await handlers.message({ source: window, origin: window.location.origin, data });
  assert.equal(messages.length, 1);
  assert.equal(messages[0].url, undefined);
  assert.equal(responses[0][0].requestId, 'req');
  assert.equal(responses[0][1], window.location.origin);
});

function studyPage({ preparation = [], login = false, capture = true, url = target, allow = true } = {}) {
  let handler;
  const clicks = [];
  const quantity = { value: '0' };
  const name = { textContent: '' };
  const dialog = { open: false };
  const decrease = { getAttribute: () => '수량 늘리기' };
  const feedback = { getAttribute: attr => attr === 'aria-live' ? 'off' : null };
  const elements = {
    'meta[name="capture-source"]': capture ? { content: 'https://www.daisomall.co.kr/ds/dst/SCR_DST_0015?searchTerm=test' } : null,
    'form[action="/access"] input[type="password"]': login ? {} : null,
    '#catalog-cart-quantity': quantity, '#catalog-cart-name': name,
    '#catalog-cart-dialog': dialog, '#catalog-cart-decrease': decrease, '#catalog-cart-feedback': feedback,
    '.product-card[data-blux-item-id="1034450"] .button-item button': { click() { clicks.push('add'); quantity.value = '1'; name.textContent = '팬트리 수납 바구니 1호'; } },
    '#catalog-cart-trigger': { click() { clicks.push('open'); dialog.open = true; } },
    '#catalog-cart-increase': { click() { clicks.push('increase'); quantity.value = String(Number(quantity.value) + 1); } },
  };
  const window = { location: new URL(url) };
  const script = 'browser-extension/study-page.js';
  if (fs.existsSync(script)) vm.runInNewContext(fs.readFileSync(script, 'utf8'), {
    URL, Date, window, document: { readyState: 'complete', querySelector: selector => elements[selector] || null },
    chrome: { runtime: { id: 'extension', onMessage: { addListener: fn => handler = fn } } },
  });
  return { clicks, quantity, name, dialog, decrease, feedback, elements,
    run(sender = { id: 'extension' }) {
      if (!handler) return Promise.resolve({ reason: 'unimplemented' });
      return new Promise(resolve => handler({ kind: 'a11y-study-page', targetUrl: target,
        preparation, allowPreparation: allow }, sender, resolve));
    } };
}

test('study page refuses login, missing capture, and a nonassigned URL before actions', async () => {
  for (const [options, reason] of [[{ login: true }, 'login_required'],
    [{ capture: false }, 'capture_missing'], [{ url: 'https://study.example/s/999999999999/index.html' }, 'wrong_url']]) {
    const page = studyPage(options);
    assert.equal((await page.run()).reason, reason);
    assert.equal(page.clicks.length, 0);
  }
});

test('ordinary tasks have no automated participant actions', async () => {
  const page = studyPage();
  const result = await page.run();
  assert.equal(result.ready, true);
  assert.equal(result.setup.excluded_from_participant_recording, true);
  assert.equal(page.clicks.length, 0);
});

test('cart preparation clicks existing UI once, reaches three units, and preserves FE attributes', async () => {
  const page = studyPage({ preparation: ['cart precondition'] });
  const result = await page.run();
  assert.equal(result.ready, true);
  assert.deepEqual(page.clicks, ['add', 'open', 'increase', 'increase']);
  assert.equal(page.quantity.value, '3');
  assert.equal(result.setup.method, 'operator_automation');
  assert.equal(page.decrease.getAttribute('aria-label'), '수량 늘리기');
  assert.equal(page.feedback.getAttribute('aria-live'), 'off');
  assert.equal((await page.run()).ready, true);
  assert.equal(page.clicks.length, 4);
});

test('a readiness check cannot repair or reset cart state after preparation', async () => {
  const page = studyPage({ preparation: ['cart precondition'], allow: false });
  assert.equal((await page.run()).reason, 'preparation_required');
  assert.equal(page.clicks.length, 0);
});

test('cart preparation is restricted to the captured Daiso catalog page', async () => {
  const page = studyPage({ preparation: ['cart precondition'] });
  page.elements['meta[name="capture-source"]'].content = 'http://ai.testatoz.com/cj/';
  assert.equal((await page.run()).reason, 'preparation_scope_mismatch');
  assert.equal(page.clicks.length, 0);
});
