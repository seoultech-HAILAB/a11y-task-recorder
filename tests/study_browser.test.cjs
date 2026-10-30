const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');

const target = 'https://study.example/s/0123456789ab/index.html';
const origin = 'https://study.example';
const DASHBOARD = 7;
const FRAME = 41;
// webNavigation listeners do not return their promise; let their async work settle.
const flush = () => new Promise(resolve => setImmediate(resolve));

// 0.4.0 harness: the copy lives in an iframe of the dashboard tab; no tabs are created.
function background({ frameUrl = target } = {}) {
  const listeners = [];
  const storage = {};
  const calls = [];
  const navigation = {};
  const sessions = { one: { id: 'one', status: 'draft', target_url: target, study: { preparation: [] } },
    two: { id: 'two', status: 'draft', target_url: target, study: { preparation: [] } } };
  let active = null;
  let page = { ready: true, setup: { method: 'operator_automation', actions: [] } };
  const frames = [{ frameId: 0, parentFrameId: -1, url: 'http://127.0.0.1:8765/' },
    { frameId: FRAME, parentFrameId: 0, url: frameUrl }];
  const listener = { addListener() {} };
  const context = vm.createContext({ URL, Date, Map, Promise,
    setTimeout: fn => fn(), navigator: { userAgent: 'Chrome/151' },
    chrome: { runtime: { id: 'extension', onMessage: { addListener: fn => listeners.push(fn) },
      onInstalled: listener, getManifest: () => ({ version: '0.5.12' }) },
      storage: { session: { get: async key => ({ [key]: storage[key] }),
        set: async data => Object.assign(storage, data), remove: async key => delete storage[key] } },
      tabs: { onRemoved: listener, get: async id => { if (id !== DASHBOARD) throw Error('missing'); return { id, url: 'http://127.0.0.1:8765/' }; },
        create: async options => { calls.push(['create', options]); throw Error('tabs.create must not be used'); },
        update: async (id, options) => { calls.push(['update', id, options]); },
        remove: async id => { calls.push(['remove', id]); },
        query: async () => [{ id: DASHBOARD, url: 'http://127.0.0.1:8765/', title: 'A11y Task Recorder' }],
        sendMessage: async (id, msg, options) => { calls.push(['page', id, msg, options]); return page; } },
      action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {}, setTitle: async () => {} },
      webNavigation: { getAllFrames: async ({ tabId }) => tabId === DASHBOARD ? frames : [],
        onCommitted: { addListener: fn => navigation.committed = fn }, onHistoryStateUpdated: { addListener: fn => navigation.history = fn },
        onErrorOccurred: { addListener: fn => navigation.error = fn } } },
    fetch: async (url, options = {}) => {
      calls.push(['fetch', url, options]);
      if (url.endsWith('/api/active-session')) return { ok: true, json: async () => ({ session: active }) };
      if (url.endsWith('/api/events')) return { ok: true, status: 201, json: async () => ({}) };
      const id = url.split('/')[5];
      if (url.endsWith('/start')) { sessions[id].status = 'active'; active = sessions[id]; }
      return { ok: true, json: async () => ({ session: sessions[id] }) };
    } });
  vm.runInContext(fs.readFileSync('browser-extension/background.js', 'utf8'), context);
  const sender = { url: 'http://127.0.0.1:8765/', frameId: 0, tab: { id: DASHBOARD } };
  return { calls, sessions, frames, storage, navigation, chrome: context.chrome, sender,
    setActive: value => active = value, setPage: value => page = value,
    async message(message, from = sender) {
      let resolve; const result = new Promise(r => resolve = r);
      let handled = false;
      for (const fn of listeners) handled = fn(message, from, resolve) || handled;
      return handled ? result : { reason: 'unhandled' };
    },
    command(action, sessionId = 'one', from = sender) {
      return this.message({ kind: 'a11y-study-command', action, sessionId }, from);
    },
    events() { return calls.filter(c => c[0] === 'fetch' && c[1].endsWith('/api/events')).map(c => JSON.parse(c[2].body)); } };
}

test('background only accepts the exact dashboard origin in the top frame', async () => {
  const app = background();
  for (const from of [{ url: 'https://127.0.0.1:8765/', frameId: 0, tab: { id: DASHBOARD } },
    { url: 'http://localhost:8765/', frameId: 0, tab: { id: DASHBOARD } },
    { url: 'https://evil.example/', frameId: 0, tab: { id: DASHBOARD } },
    { url: 'http://127.0.0.1:8765/', frameId: 3, tab: { id: DASHBOARD } }]) {
    assert.equal((await app.command('prepare', 'one', from)).reason, 'untrusted_origin');
  }
});

test('prepare claims the dashboard tab, finds the copy frame, and never creates a tab', async () => {
  const app = background();
  const result = await app.command('prepare');
  assert.equal(result.ready, true);
  assert.equal(result.frameId, FRAME);
  assert.equal(result.tabId, DASHBOARD);
  assert.equal(app.calls.filter(c => c[0] === 'create').length, 0);
  const page = app.calls.find(c => c[0] === 'page');
  assert.equal(JSON.stringify(page[3]), JSON.stringify({ frameId: FRAME }));
  assert.equal(page[1], DASHBOARD);
  const again = await app.command('prepare');
  assert.equal(again.ready, true);
  assert.equal(app.calls.filter(c => c[0] === 'page').length, 2);
});

test('frame still loading or showing the login page cannot start; another dashboard tab is refused', async () => {
  const app = background({ frameUrl: `${origin}/access` });
  app.setPage({ ready: false, reason: 'login_required' });
  const result = await app.command('prepare');
  assert.equal(result.ready, false);
  assert.equal(result.reason, 'login_required');
  assert.equal((await app.command('start')).reason, 'login_required');
  app.frames[1].url = 'https://other.example/';
  assert.equal((await app.command('check')).reason, 'wrong_url');
  app.frames.length = 1;
  assert.equal((await app.command('check')).reason, 'loading');
  assert.equal((await app.command('check', 'one', { ...app.sender, tab: { id: 99 } })).reason, 'wrong_dashboard_tab');
});

test('start gates on readiness, posts the start, and records frame metadata without focusing tabs', async () => {
  const app = background();
  await app.command('prepare');
  await app.message({ kind: 'recorder-frame-title', title: '파라타항공 예약조회', url: target }, { tab: { id: DASHBOARD }, frameId: FRAME, url: target });
  const result = await app.command('start');
  assert.equal(result.started, true);
  assert.equal(result.frame_title, '파라타항공 예약조회');
  const posted = app.calls.find(c => c[1]?.endsWith?.('/start'));
  assert.equal(JSON.parse(posted[2].body).study_ready, true);
  assert.equal(app.calls.filter(c => c[0] === 'update').length, 0);
  const events = app.events();
  assert.equal(events.length, 1);
  assert.equal(events[0].type, 'page_ready');
  assert.equal(events[0].page_title, '파라타항공 예약조회');
  assert.equal(events[0].payload.ui_mode, 'single_window');
  assert.equal(events[0].payload.frame_id, FRAME);
  assert.equal((await app.command('status')).frame_title, '파라타항공 예약조회');
  assert.equal((await app.command('start')).reason, 'already_started');
  assert.equal((await app.command('prepare', 'two')).reason, 'active_session_conflict');
});

test('recording accepts browser events only from the owned frame (R6, single window)', async () => {
  const app = background();
  await app.command('prepare');
  await app.command('start');
  const before = app.events().length;
  const event = { type: 'dom_mutation', timestamp: new Date().toISOString(), url: 'https://other.example/mail', page_title: 'Private mail', payload: {} };
  for (const from of [{ tab: { id: DASHBOARD + 1 }, frameId: FRAME, url: 'https://other.example/mail' },
    { tab: { id: DASHBOARD }, frameId: 0, url: 'http://127.0.0.1:8765/' },
    { tab: { id: DASHBOARD }, frameId: FRAME + 9, url: 'https://other.example/popup' }, {}]) {
    const result = await app.message({ kind: 'recorder-event', event }, from);
    assert.equal(result.recorded, false);
    assert.equal(result.reason, 'outside_task_frame');
  }
  await app.navigation.committed({ frameId: 0, tabId: DASHBOARD, url: 'http://127.0.0.1:8765/?x', transitionQualifiers: [] });
  await app.navigation.committed({ frameId: FRAME, tabId: DASHBOARD + 1, url: 'https://other.example/next', transitionQualifiers: [] });
  await flush();
  assert.equal(app.events().length, before);
  const own = await app.message({ kind: 'recorder-event', event: { ...event, url: target } }, { tab: { id: DASHBOARD }, frameId: FRAME, url: target });
  assert.equal(own.recorded, true);
  await app.navigation.committed({ frameId: FRAME, tabId: DASHBOARD, url: target.replace('index', 'p/0123456789abcdef'), transitionQualifiers: [] });
  await app.navigation.history({ frameId: FRAME, tabId: DASHBOARD, url: target + '#a' });
  await flush();
  const posted = app.events().slice(before);
  assert.deepEqual(posted.map(e => e.type), ['dom_mutation', 'navigation', 'history']);
  assert.ok(posted.every(e => e.payload.tab_id === DASHBOARD && e.payload.frame_id === FRAME));
  assert.ok(posted.every(e => !e.url.includes('other.example')));
});

test('frame titles are accepted only from the dashboard tab and the copy origin', async () => {
  const app = background();
  await app.command('prepare');
  assert.equal((await app.message({ kind: 'recorder-frame-title', title: 'x', url: 'https://evil.example/' }, { tab: { id: DASHBOARD }, frameId: 77, url: 'https://evil.example/' })).accepted, false);
  assert.equal((await app.message({ kind: 'recorder-frame-title', title: 'x', url: target }, { tab: { id: DASHBOARD + 1 }, frameId: FRAME, url: target })).accepted, false);
  assert.equal((await app.message({ kind: 'recorder-frame-title', title: '제목', url: target }, { tab: { id: DASHBOARD }, frameId: FRAME, url: target })).accepted, true);
  assert.equal(app.storage.a11yStudyOwnedTab.frameTitle, '제목');
});

test('close releases ownership only for finished sessions and removes no tabs', async () => {
  const app = background();
  await app.command('prepare');
  assert.equal((await app.command('close')).reason, 'session_not_finished');
  app.sessions.one.status = 'completed';
  assert.equal((await app.command('close')).closed, true);
  assert.equal(app.storage.a11yStudyOwnedTab, undefined);
  assert.equal(app.calls.filter(c => c[0] === 'remove').length, 0);
  assert.equal((await app.command('close')).closed, false);
});

test('manifest runs the content script in all frames and declares no browser command', () => {
  const manifest = JSON.parse(fs.readFileSync('browser-extension/manifest.json', 'utf8'));
  assert.equal(manifest.commands, undefined);
  assert.ok(manifest.content_scripts.every(entry => entry.all_frames === true));
  assert.equal(manifest.version, '0.5.12');
  assert.equal(/chrome\.commands/.test(fs.readFileSync('browser-extension/background.js', 'utf8')), false);
});

test('content script in a sub-frame reports its title and marks page_ready as in_frame', async () => {
  const messages = [];
  const timers = [];
  const window = { location: new URL(target), addEventListener() {} };
  window.top = {};
  const document = { readyState: 'complete', title: '예약조회', documentElement: { lang: 'ko' }, addEventListener() {} };
  vm.runInNewContext(fs.readFileSync('browser-extension/content.js', 'utf8'), {
    URL, Date, Set, Element: class {}, Node: { ELEMENT_NODE: 1 }, window, document,
    MutationObserver: class { observe() {} }, setTimeout: fn => fn(), setInterval: fn => timers.push(fn),
    chrome: { runtime: { sendMessage: async msg => { messages.push(msg); } } } });
  assert.equal(messages[0].kind, 'recorder-event');
  assert.equal(messages[0].event.payload.in_frame, true);
  assert.equal(JSON.stringify(messages[1]), JSON.stringify({ kind: 'recorder-frame-title', title: '예약조회', url: target }));
  document.title = '예약 결과';
  timers[0]();
  assert.equal(messages.at(-1).title, '예약 결과');
  timers[0]();
  assert.equal(messages.length, 3);
});

test('dashboard bridge ignores foreign window/origin and removes arbitrary payload fields', async () => {
  const handlers = {};
  const messages = [];
  const responses = [];
  const window = { location: new URL('http://127.0.0.1:8765/'),
    addEventListener: (name, fn) => handlers[name] = fn,
    postMessage: (...args) => responses.push(args) };
  window.top = window;
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

function studyPage({ preparation = [], login = false, capture = true, url = target, allow = true, assigned = target } = {}) {
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
      return new Promise(resolve => handler({ kind: 'a11y-study-page', targetUrl: assigned,
        preparation, allowPreparation: allow }, sender, resolve));
    } };
}

// A partner's test copy (SNC, http only, no /s/<id>/ path, no capture meta) is ready once the
// assigned URL has loaded; our own copies keep the https and capture-meta rules.
test('a partner test copy over http is ready at its assigned URL without a capture meta', async () => {
  const partner = 'http://partner.example/koref/web/user/';
  const page = studyPage({ url: partner, assigned: partner, capture: false });
  const result = await page.run();
  assert.equal(result.ready, true);
  assert.equal(result.setup.actions.length, 0);
  assert.equal(result.setup.excluded_from_participant_recording, true);
  assert.equal(page.clicks.length, 0);
  assert.equal((await studyPage({ url: `${partner}other.do`, assigned: partner, capture: false }).run()).reason, 'wrong_url');
  assert.equal((await studyPage({ url: partner, assigned: partner, capture: false, preparation: ['cart precondition'] }).run()).reason,
    'preparation_scope_mismatch');
  const plainCopy = 'http://study.example/s/0123456789ab/index.html';
  assert.equal((await studyPage({ url: plainCopy, assigned: plainCopy }).run()).reason, 'wrong_url');
});

test('background accepts a partner test copy over http as the assigned URL, still not our copies over http', async () => {
  const partner = 'http://partner.example/koref/web/user/';
  const app = background({ frameUrl: partner });
  app.sessions.one.target_url = partner;
  const result = await app.command('prepare');
  assert.equal(result.ready, true);
  assert.equal(result.frameId, FRAME);
  for (const bad of [`${partner}?x=1`, `${partner}#top`, 'http://user:pw@partner.example/koref/', 'http://study.example/s/0123456789ab/index.html', 'ftp://partner.example/']) {
    app.sessions.two.target_url = bad;
    assert.equal((await app.command('prepare', 'two')).reason, 'invalid_assignment_url', bad);
  }
});

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

// Windows pilot 10/28: Alt+Left / Alt+Home in the study frame go through the recorder, and
// back never goes past the history entry the task started on.
// Objects made inside the vm context have another Object prototype.
const plain = value => JSON.parse(JSON.stringify(value));
function studyFrame({ ancestor = 'http://127.0.0.1:8765', entries = ['s0', 'k1', 'k2'], current = 2, controls = null } = {}) {
  const handlers = {};
  const posted = [];
  const backs = [];
  const focused = [];
  const parent = { postMessage: (data, origin) => posted.push([data, origin]) };
  const location = new URL(target);
  location.ancestorOrigins = [ancestor];
  const nav = { entries: () => entries.map((key, index) => ({ key, index })),
    get currentEntry() { return { key: entries[current], index: current }; },
    back() { backs.push(current); return { committed: Promise.resolve(), finished: Promise.resolve() }; } };
  const window = { location, parent, top: parent, navigation: nav,
    addEventListener: (name, fn) => { (handlers[name] ||= []).push(fn); } };
  // A control that refuses focus (visibility:hidden) leaves document.activeElement where it was.
  const control = (name, focusable = true) => { const node = { disabled: false, getClientRects: () => [{}], closest: () => null,
    focus: () => { if (focusable) { focused.push(name); document.activeElement = node; } } }; return node; };
  const document = { readyState: 'complete', title: '사본', documentElement: { lang: 'ko' }, addEventListener() {}, activeElement: null,
    querySelectorAll: () => controls ? controls.map(([name, focusable]) => control(name, focusable)) : [control('link'), control('button')] };
  vm.runInNewContext(fs.readFileSync('browser-extension/content.js', 'utf8'), {
    URL, Date, Set, Element: class {}, Node: { ELEMENT_NODE: 1 }, window, document,
    MutationObserver: class { observe() {} }, setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0,
    chrome: { runtime: { sendMessage: async () => ({ accepted: true }) } } });
  const key = (init) => { let prevented = false;
    for (const fn of handlers.keydown || []) fn({ ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, ...init, preventDefault: () => { prevented = true; } });
    return prevented; };
  const message = data => { for (const fn of handlers.message || []) fn({ source: parent, origin: 'http://127.0.0.1:8765', data }); };
  return { posted, backs, focused, key, message, setCurrent: value => { current = value; } };
}

test('study frame sends Alt+Left and Alt+Home to the recorder instead of the browser', () => {
  const frame = studyFrame();
  assert.equal(frame.key({ altKey: true, key: 'ArrowLeft' }), true);
  assert.equal(frame.key({ altKey: true, key: 'Home' }), true);
  assert.equal(frame.key({ key: 'BrowserBack' }), true);
  assert.equal(frame.key({ key: 'ArrowLeft' }), false, 'plain arrows stay with the site and NVDA');
  assert.equal(frame.key({ altKey: true, shiftKey: true, key: 'ArrowLeft' }), false);
  assert.deepEqual(frame.posted.map(([data, origin]) => [data.action, origin]),
    [['back', 'http://127.0.0.1:8765'], ['start', 'http://127.0.0.1:8765'], ['back', 'http://127.0.0.1:8765']]);
  const other = studyFrame({ ancestor: 'https://news.example' });
  assert.equal(other.key({ altKey: true, key: 'ArrowLeft' }), false, 'frames outside the recorder keep the browser key');
  assert.equal(other.posted.length, 0);
});

test('recorder back goes back inside the frame but never past the task start entry', () => {
  const frame = studyFrame({ entries: ['login', 's0', 'k1'], current: 2 });
  frame.message({ kind: 'a11y-study-site-back', startKey: 's0' });
  assert.deepEqual(frame.backs, [2]);
  assert.deepEqual(plain(frame.posted.at(-1)[0]), { kind: 'a11y-study-site-back', moved: true });
  frame.setCurrent(1);
  frame.message({ kind: 'a11y-study-site-back', startKey: 's0' });
  assert.deepEqual(plain(frame.posted.at(-1)[0]), { kind: 'a11y-study-site-back', moved: false });
  frame.setCurrent(2);
  frame.message({ kind: 'a11y-study-site-back', startKey: 'missing' });
  assert.deepEqual(plain(frame.posted.at(-1)[0]), { kind: 'a11y-study-site-back', moved: false });
  assert.deepEqual(frame.backs, [2], 'only the first request moved');
});

test('focus request reports the start entry and focuses only when asked', () => {
  const frame = studyFrame({ current: 0 });
  frame.message({ kind: 'a11y-study-focus-site', focus: false });
  assert.deepEqual(frame.focused, []);
  assert.deepEqual(plain(frame.posted.at(-1)[0]), { kind: 'a11y-study-site-entry', key: 's0' });
  frame.message({ kind: 'a11y-study-focus-site' });
  assert.deepEqual(frame.focused, ['link']);
  // 0.5.0 focus trap: Shift+Tab out of the top of the site comes back in at its last control.
  frame.message({ kind: 'a11y-study-focus-site', edge: 'last' });
  assert.deepEqual(frame.focused, ['link', 'button']);
});

test('a control that refuses focus is skipped, so focus never stays on the document (0.5.12)', () => {
  // CJ: the consented cookie dialog is still listed but hidden with visibility:hidden.
  const frame = studyFrame({ controls: [['쿠키 사용 고지', false], ['동의', false], ['본문 바로가기', true], ['푸터', true]] });
  frame.message({ kind: 'a11y-study-focus-site', edge: 'resume' });
  assert.deepEqual(frame.focused, ['본문 바로가기']);
  frame.message({ kind: 'a11y-study-focus-site', edge: 'last' });
  assert.deepEqual(frame.focused, ['본문 바로가기', '푸터']);
});

test('a frame Chrome refused reports the net error instead of loading forever', async () => {
  const app = background({ frameUrl: 'chrome-error://chromewebdata/' });
  assert.equal((await app.command('prepare')).reason, 'loading');
  app.navigation.error({ tabId: DASHBOARD, frameId: FRAME, parentFrameId: 0, url: target, error: 'net::ERR_BLOCKED_BY_CSP' });
  const blocked = await app.command('check');
  assert.deepEqual([blocked.ok, blocked.ready, blocked.reason, blocked.net_error], [true, false, 'frame_error', 'net::ERR_BLOCKED_BY_CSP']);
  // An embed inside the copy (a video frame, parent = the copy frame) does not replace it.
  app.navigation.error({ tabId: DASHBOARD, frameId: 99, parentFrameId: FRAME, url: 'https://video.example/embed', error: 'net::ERR_NAME_NOT_RESOLVED' });
  assert.equal((await app.command('check')).net_error, 'net::ERR_BLOCKED_BY_CSP');
  app.navigation.committed({ tabId: DASHBOARD, frameId: FRAME, parentFrameId: 0, url: target, transitionQualifiers: [] });
  await flush();
  app.frames[1].url = target;
  assert.equal((await app.command('check')).ready, true, 'a later successful load clears the error');
});

// 0.5.8: Chrome drops cookies set by a cross-site page in the recorder frame, so a consent the
// participant gave never stuck (CJ copy, Windows 10/29). The frame reads it as given.
test('in the recorder frame only, the page reads the cookie consent as given', () => {
  const manifest = JSON.parse(fs.readFileSync('browser-extension/manifest.json', 'utf8'));
  const entry = manifest.content_scripts.find(item => item.js.includes('site-cookies.js'));
  assert.deepEqual([entry.world, entry.run_at], ['MAIN', 'document_start']);
  const run = ({ inFrame, origin, jar = '' }) => {
    let stored = jar;
    class Document {}
    Object.defineProperty(Document.prototype, 'cookie', { configurable: true, get() { return stored; }, set(value) { stored = value.split(';')[0]; } });
    const top = {};
    const window = inFrame ? { top, parent: top } : null;
    const self = window || top;
    if (!inFrame) { top.top = top; top.parent = top; }
    vm.runInNewContext(fs.readFileSync('browser-extension/site-cookies.js', 'utf8'),
      { Document, window: self, location: { ancestorOrigins: origin ? [origin] : [] } });
    return new Document();
  };
  assert.equal(run({ inFrame: true, origin: 'http://127.0.0.1:8765' }).cookie, 'isCookieAgree=1');
  assert.equal(run({ inFrame: true, origin: 'http://127.0.0.1:8765', jar: 'a=b' }).cookie, 'a=b; isCookieAgree=1');
  assert.equal(run({ inFrame: true, origin: 'http://127.0.0.1:8765', jar: 'isCookieAgree=0' }).cookie, 'isCookieAgree=0');
  assert.equal(run({ inFrame: true, origin: 'https://elsewhere.example' }).cookie, '');
  assert.equal(run({ inFrame: false }).cookie, '');
  const doc = run({ inFrame: true, origin: 'http://127.0.0.1:8765' });
  doc.cookie = 'x=1; path=/';
  assert.equal(doc.cookie, 'x=1; isCookieAgree=1', 'the page still writes its own cookies');
});
