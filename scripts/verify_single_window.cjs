// Linux rehearsal of the 0.4.0 participant-only single-window flow with a real Chromium
// extension, the real recorder server, and the real hosted-copy server on temporary ports.
// The main run has the access code "built in" through A11Y_RECORDER_ACCESS_CODE (the
// kit path app/study_access.txt); a second run restarts the recorder without a code and
// checks that nobody is ever asked for one (site error instead). NVDA is a synthetic
// heartbeat only; nothing here is human data or an NVDA verification.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { spawn, execFileSync } = require('node:child_process');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const LAB = process.env.A11Y_LAB_DIR || path.resolve('..', 'a11y-user-context-lab');
const MOUNT = '16a50b10a80c';
const PAGES = ['p/413127f760b3356b.html', 'p/15fd75c5d2246d23.html'];
const ACCESS_CODE = 'e2e-access-code-not-a-secret';
const END_HINT = '답을 찾았거나 더 진행할 수 없다고 판단되면, 그 자리에서 NVDA, Ctrl, E를 함께 눌러 과업을 끝내세요.';
const freePort = () => new Promise(resolve => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); }); });
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function waitFor(fn, label, timeout = 20000) {
  console.error('wait -', label);
  const started = Date.now();
  for (;;) {
    try { const value = await fn(); if (value) return value; } catch { /* retry */ }
    if (Date.now() - started > timeout) throw new Error(`timeout: ${label}`);
    await sleep(150);
  }
}
function copyMount(from, to) {
  fs.mkdirSync(path.join(to, 'p'), { recursive: true });
  for (const file of ['index.html', 'runtime.js', 'unavailable.html']) fs.copyFileSync(path.join(from, file), path.join(to, file));
  for (const file of fs.readdirSync(path.join(from, 'p'))) fs.copyFileSync(path.join(from, 'p', file), path.join(to, 'p', file));
}
// Visibility, aria-hidden and Tab reachability of the participant controls, plus the Tab
// order inside the study view (the app header outside it is not part of the task).
function a11yState() {
  const hiddenish = node => Boolean(node.closest('[hidden]')) || getComputedStyle(node).display === 'none';
  const state = {};
  for (const id of ['study-start', 'study-practice', 'study-end', 'study-end-yes', 'study-end-no', 'study-submit', 'study-next', 'study-edit',
    'study-practice-stop', 'study-frame', 'study-tester', 'study-select', 'study-records', 'study-shortcuts',
    'study-restart', 'study-restart-yes', 'study-restart-no', 'study-return', 'study-redo', 'study-redo-yes', 'study-redo-no',
    'study-tester-change', 'study-tester-yes', 'study-tester-no', 'study-tester-keep']) {
    const node = document.getElementById(id);
    const ariaHidden = Boolean(node.closest('[aria-hidden="true"]'));
    state[id] = { shown: !hiddenish(node), ariaHidden, tabbable: !hiddenish(node) && !ariaHidden && node.tabIndex >= 0 && !node.disabled && !node.closest('[inert]') };
  }
  state.order = [...document.querySelectorAll('#study-view button, #study-view input, #study-view select, #study-view textarea, #study-view iframe, #study-view [tabindex]')]
    .filter(node => !hiddenish(node) && node.tabIndex >= 0 && !node.disabled && !node.closest('[inert]') && !node.closest('[aria-hidden="true"]'))
    .map(node => node.id || node.name || node.tagName.toLowerCase());
  return state;
}

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'recorder-single-window-'));
  const output = path.resolve('dist/qa/single-window');
  const shots = process.env.SELFRUN_DIR || output;
  fs.mkdirSync(output, { recursive: true });
  fs.mkdirSync(shots, { recursive: true });
  const recorderPort = await freePort();
  const hostedPort = await freePort();
  const recorderBase = `http://127.0.0.1:${recorderPort}`;
  const hostedOrigin = `http://localhost:${hostedPort}`;
  const checks = [];
  const check = text => { checks.push(text); console.error('ok -', text); };
  let context, hosted, recorder, heartbeat;
  const spawnHosted = () => spawn('npx', ['tsx', 'scripts/serve-hosted-benchmark.ts', '--dir', path.join(temp, 'public'),
    '--port', String(hostedPort), '--credentials-file', path.join(temp, 'creds.json'), '--origin', hostedOrigin,
    '--upload-dir', path.join(temp, 'uploads'), '--frame-ancestor', recorderBase], { cwd: LAB, stdio: ['ignore', 'pipe', 'pipe'], detached: true });
  // npx spawns tsx as a child; kill the whole process group so the port is really released.
  const stopRecorder = async () => { if (recorder && recorder.exitCode === null) { recorder.kill('SIGTERM'); await new Promise(r => recorder.once('exit', r)); } };
  const stopHosted = async proc => { if (!proc || proc.exitCode !== null) return; try { process.kill(-proc.pid, 'SIGTERM'); } catch { proc.kill('SIGTERM'); } await new Promise(r => proc.once('exit', r)); };
  try {
    copyMount(path.join(LAB, 'runs/hosted-benchmark-v4b-inframe-20261023/public/s', MOUNT), path.join(temp, 'public/s', MOUNT));
    fs.writeFileSync(path.join(temp, 'creds.json'), JSON.stringify({ accessCode: ACCESS_CODE, signingKey: 'e2e-signing-key-not-a-secret-0123456789' }));
    hosted = spawnHosted();
    await waitFor(async () => (await fetch(`${hostedOrigin}/healthz`)).ok, 'hosted server');
    const probe = await fetch(`${hostedOrigin}/s/${MOUNT}/index.html`);
    assert.match(probe.headers.get('content-security-policy'), new RegExp(`frame-ancestors 'self' http://127\\.0\\.0\\.1:8765 http://localhost:8765 ${recorderBase.replace(/[.]/g, '\\.')}`));
    const foreignLogin = await fetch(`${hostedOrigin}/access`, { method: 'POST', redirect: 'manual',
      headers: { Origin: 'https://other.invalid', 'Content-Type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({ code: ACCESS_CODE, next: '/' }) });
    assert.equal(foreignLogin.status, 403);
    check('hosted CSP allows the recorder origin as frame ancestor; access form refuses other origins');

    // Extension copy pointing at the temporary recorder port (the shipped kit uses 8765).
    const extension = path.join(temp, 'extension');
    fs.cpSync(path.resolve('browser-extension'), extension, { recursive: true });
    for (const file of ['background.js', 'content.js']) {
      const target = path.join(extension, file);
      fs.writeFileSync(target, fs.readFileSync(target, 'utf8').replaceAll('http://127.0.0.1:8765', recorderBase));
    }
    const manifest = JSON.parse(fs.readFileSync(path.join(extension, 'manifest.json'), 'utf8'));
    manifest.host_permissions = ['<all_urls>', `${recorderBase}/*`];
    fs.writeFileSync(path.join(extension, 'manifest.json'), JSON.stringify(manifest));

    // Three tasks: task 2 has a 4 second limit to exercise the automatic timeout save.
    const task = (n, page, limit, requiresAnswer) => ({ id: `e2e-a${n}`, task_id: `t${n}`, order: n, block: 1, site_name: '파라타항공',
      instruction: `${n}번 과업: 화면에서 안내 문구를 찾아 주세요.`, start_url: `${hostedOrigin}/s/${MOUNT}/${page}`,
      time_limit_seconds: limit, preparation: [], requires_answer: requiresAnswer });
    const catalog = { schema_version: 1, id: 'e2e-single-window', title: 'E2E', source: { spec_version: 'e2e', spec_sha256: 'a'.repeat(64), assignments_sha256: 'b'.repeat(64) },
      testers: [{ id: 'T01', tasks: [task(1, 'index.html', 600, true), task(2, PAGES[0], 4, false), task(3, PAGES[1], 600, true)] },
        // 0.4.4: T02 exercises tester change, restart and return to the task.
        { id: 'T02', tasks: [{ ...task(1, 'index.html', 600, true), id: 'e2e-b1' }, { ...task(2, PAGES[0], 600, true), id: 'e2e-b2' }] }] };
    fs.writeFileSync(path.join(temp, 'catalog.json'), JSON.stringify(catalog));
    recorder = spawn('python3', ['run.py', '--no-browser', '--port', String(recorderPort), '--db', path.join(temp, 'recorder.sqlite3'), '--catalog', path.join(temp, 'catalog.json')],
      { env: { ...process.env, A11Y_RECORDER_UPLOAD_URL: `${hostedOrigin}/upload/session`, A11Y_RECORDER_ACCESS_CODE: ACCESS_CODE, PYTHONUNBUFFERED: '1' }, stdio: ['ignore', 'pipe', 'pipe'] });
    let recorderOut = '';
    recorder.stdout.on('data', d => { recorderOut += d; });
    await waitFor(async () => (await fetch(`${recorderBase}/api/health`)).ok, 'recorder');
    const api = async (p, method = 'GET', body) => (await fetch(recorderBase + p, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined })).json();
    heartbeat = setInterval(() => fetch(`${recorderBase}/api/active-session`, { headers: { 'X-A11y-Client': 'nvda-addon' } }).catch(() => {}), 1000);
    await fetch(`${recorderBase}/api/active-session`, { headers: { 'X-A11y-Client': 'nvda-addon' } });
    const uploadsStatus = (await api('/api/uploads')).uploads;
    assert.equal(uploadsStatus.configured, true);
    assert.equal(uploadsStatus.code_source, 'kit');
    assert.equal((await fetch(`${recorderBase}/api/study/access`, { headers: { Origin: 'https://other.invalid' } })).status, 403);
    check('built-in code: uploads configured from the kit, /api/study/access refuses other origins');

    context = await chromium.launchPersistentContext(path.join(temp, 'chrome-profile'), {
      headless: true, channel: 'chromium', viewport: { width: 1366, height: 900 },
      args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`],
    });
    const dashboard = await context.newPage();
    const errors = [];
    dashboard.on('pageerror', e => errors.push(e.message));
    const phase = () => dashboard.evaluate(() => document.querySelector('#study-view').dataset.phase);
    const waitPhase = (name, timeout = 15000) => dashboard.waitForFunction(p => document.querySelector('#study-view').dataset.phase === p, name, { timeout });
    const waitFocus = (id, timeout = 15000) => dashboard.waitForFunction(i => document.activeElement?.id === i, id, { timeout });
    const a11y = () => dashboard.evaluate(a11yState);
    await dashboard.goto(recorderBase);
    for (const removed of ['#study-operator', '#study-prepare', '#study-check', '#study-read', '#study-resume-task', '#study-upload-form', '#study-outcome']) {
      assert.equal(await dashboard.locator(removed).count(), 0, removed);
    }
    check('no facilitator panel, prepare/check/read/resume buttons, upload settings or facilitator result form');

    // 배정 확인 prepares the site and logs the iframe in by itself; focus lands on the instruction.
    await dashboard.locator('#study-tester').selectOption('T01');
    await dashboard.locator('#study-select').click();
    try {
      await waitFocus('study-instruction', 40000);
    } catch (error) {
      console.error('debug message:', await dashboard.locator('#study-message').textContent());
      console.error('debug frames:', dashboard.frames().map(f => f.url()));
      throw error;
    }
    const frame = dashboard.frameLocator('#study-frame');
    assert.equal(await dashboard.locator('#study-frame').isHidden(), true);
    assert.equal((await api('/api/active-session')).session, null);
    assert.equal(dashboard.frames().some(f => f.url() === catalog.testers[0].tasks[0].start_url), true);
    let state = await a11y();
    assert.deepEqual([state['study-start'].tabbable, state['study-practice'].tabbable, state['study-end'].shown, state['study-next'].shown], [true, true, false, false]);
    // Tester locked: the picker, 배정 확인 and 기록 관리 are gone; Tab reaches 과업 시작, 연습 시작 and
    // (0.4.4) 테스터 번호 바꾸기.
    assert.deepEqual([state['study-tester'].shown, state['study-select'].shown, state['study-records'].shown], [false, false, false]);
    assert.deepEqual(state.order, ['study-start', 'study-practice', 'study-tester-change']);
    await dashboard.keyboard.press('Tab');
    await waitFocus('study-start', 3000);
    await dashboard.keyboard.press('Tab');
    await waitFocus('study-practice', 3000);
    assert.equal(state['study-shortcuts'].shown, true);
    assert.equal(await dashboard.locator('#study-shortcuts').textContent(),
      '단축키: 끝내기는 NVDA, Ctrl, E. 지시문 다시 듣기는 NVDA, Ctrl, H. 사이트에서 이전 페이지는 Alt, 왼쪽 화살표. 과업 처음 화면은 Alt, Home.');
    // Windows pilot 10/28: the instruction itself says to end right there with the gesture.
    assert.equal(await dashboard.locator('#study-instruction').textContent(),
      `1번 과업: 화면에서 안내 문구를 찾아 주세요. ${END_HINT}`);
    await dashboard.locator('#study-instruction').focus();
    await dashboard.screenshot({ path: path.join(shots, '1-ready.png') });
    const prepared = (await api(`/api/sessions/${(await api('/api/study/T01')).tasks[0].session_id}/events`)).events.filter(e => e.type === 'site_preparation');
    assert.deepEqual(prepared.map(e => [e.payload.result, e.payload.login]), [['ready', 'submitted']]);
    check('배정 확인 -> automatic prepare + hidden-form login (no code typed); focus on instruction; tester picker hidden; Tab order = 과업 시작, 연습 시작, 테스터 번호 바꾸기; shortcut line shown; site_preparation ready');

    // Practice with the same participant buttons; 연습 끝내기 prepares the real task again.
    await dashboard.locator('#study-practice').click();
    await waitFocus('study-instruction');
    assert.match(await dashboard.locator('#study-instruction').textContent(),
      /^연습입니다\. 연습 화면에서 '재료 보기' 버튼을 눌러 첫 번째 재료를 확인해 주세요\. 끝낸 뒤 결과 칸에 그 재료를 적습니다\. 과업은 지시문 아래 '과업 시작' 버튼으로 시작합니다\. 답을 찾았거나 더 진행할 수 없다고 판단되면, 그 자리에서 NVDA, Ctrl, E를 함께 눌러 과업을 끝내세요\. 지시문을 다시 듣고 싶으면 NVDA, Ctrl, H를 누르세요\./);
    const practiceRepeat = (await api('/api/signals', 'POST', { kind: 'repeat', source: 'nvda_addon' })).speak;
    assert.ok(practiceRepeat.includes('연습') && practiceRepeat.endsWith(END_HINT), practiceRepeat);
    await dashboard.locator('#study-start').click();
    await waitPhase('active');
    await waitFocus('study-frame');
    // F3: focus moves to the practice page's first control (same-origin path).
    const practiceFrame = await (await dashboard.$('#study-frame')).contentFrame();
    await practiceFrame.waitForFunction(() => document.activeElement && document.activeElement !== document.body, null, { timeout: 3000 });
    await dashboard.frameLocator('#study-frame').locator('#show').click();
    await api('/api/signals', 'POST', { kind: 'end', source: 'nvda_addon' });
    await waitPhase('answering');
    assert.equal(await dashboard.locator('#study-end-confirm').isHidden(), true);
    await dashboard.locator('input[name="study-done"][value="completed"]').check();
    await dashboard.locator('#study-answer').fill('두부');
    await dashboard.locator('#study-submit').click();
    await waitFocus('study-saved');
    assert.equal((await a11y())['study-practice-stop'].tabbable, true);
    await dashboard.locator('#study-practice-stop').click();
    await waitFocus('study-instruction', 40000);
    assert.equal(await phase(), 'ready');
    assert.equal((await api('/api/sessions')).sessions.length, 1);
    check('practice: instruction starts with the shortcut guide; T repeat ends with the E hint; 연습 시작 -> 과업 시작 -> NVDA end (no confirm) -> 저장 -> 연습 끝내기 -> real task ready again; no session created');

    // Task 1: start by NVDA-style signal.
    assert.equal((await api('/api/signals', 'POST', { kind: 'start', source: 'nvda_addon' })).accepted, true);
    await waitPhase('active');
    await waitFocus('study-frame');
    // F3: the copy's first control gets focus through the content script, as on a newly opened page.
    const copyFrame = await (await dashboard.$('#study-frame')).contentFrame();
    await copyFrame.waitForFunction(() => document.activeElement && document.activeElement !== document.body, null, { timeout: 5000 });
    const firstFocus = await copyFrame.evaluate(() => document.activeElement.outerHTML.slice(0, 60));
    // F2: header, skip link and footer are inert; Shift+Tab from the first control never reaches the home link.
    for (const selector of ['.skip-link', '.app-header', 'body > footer']) assert.equal(await dashboard.locator(selector).getAttribute('inert'), '', selector);
    await dashboard.keyboard.press('Shift+Tab');
    await sleep(300);
    const beforeFrame = await dashboard.evaluate(() => document.activeElement?.closest('.app-header, .skip-link, footer') ? 'header' : document.activeElement?.id || document.activeElement?.tagName);
    assert.notEqual(beforeFrame, 'header');
    await copyFrame.evaluate(() => document.activeElement.blur());
    await dashboard.locator('#study-frame').focus();
    state = await a11y();
    assert.equal(await dashboard.locator('#study-top').getAttribute('aria-hidden'), 'true');
    assert.equal(await dashboard.locator('#study-start').getAttribute('tabindex'), '-1');
    // 과업 끝내기 is visible and in the accessibility tree, but out of the Tab order.
    assert.deepEqual([state['study-end'].shown, state['study-end'].ariaHidden, state['study-end'].tabbable], [true, false, false]);
    assert.equal(await dashboard.locator('#study-end').getAttribute('tabindex'), '-1');
    assert.deepEqual([state['study-end-yes'].shown, state['study-end-no'].shown, state['study-shortcuts'].shown], [false, false, false]);
    assert.deepEqual(state.order, ['study-frame']);
    assert.equal(state['study-submit'].shown || state['study-next'].shown, false);
    // Tab from the last focusable element of the site leaves the frame without landing on 과업 끝내기.
    await frame.locator('body').evaluate(() => {
      const nodes = [...document.querySelectorAll('a[href], button, input, select, textarea, [tabindex]')]
        .filter(n => n.tabIndex >= 0 && !n.disabled && n.getClientRects().length);
      nodes[nodes.length - 1].focus();
    });
    await dashboard.keyboard.press('Tab');
    await sleep(300);
    const afterFrame = await dashboard.evaluate(() => document.activeElement?.id || document.activeElement?.tagName);
    assert.ok(!['study-end', 'study-end-yes', 'study-end-no'].includes(afterFrame), `Tab after the site went to ${afterFrame}`);
    const endBox = await dashboard.locator('#study-end').boundingBox();
    assert.ok(endBox.y + endBox.height <= 900, `end button inside the viewport (${Math.round(endBox.y)})`);
    await dashboard.waitForFunction(() => document.title !== 'A11y Task Recorder', null, { timeout: 10000 });
    const mirrored = await dashboard.title();
    check(`active: first site control focused (${firstFocus}); header/skip link/footer inert, Shift+Tab went to ${beforeFrame}`);
    check(`active: top shelved; Tab order in view = frame only (과업 끝내기 visible, not aria-hidden, tabindex -1; Tab after the site went to ${afterFrame}); title mirrors copy: ${mirrored}`);

    // The button asks first; focus lands on 계속하기, and only then are 끝내기/계속하기 tabbable.
    await dashboard.locator('#study-end').click();
    await waitFocus('study-end-no', 3000);
    state = await a11y();
    assert.deepEqual([state['study-end'].shown, state['study-end-yes'].tabbable, state['study-end-no'].tabbable], [false, true, true]);
    assert.deepEqual(state.order, ['study-frame', 'study-end-yes', 'study-end-no']);
    assert.equal(await dashboard.locator('#study-end-question').textContent(), '과업을 끝낼까요?');
    assert.equal(await phase(), 'active');
    const confirmBox = await dashboard.locator('#study-end-confirm').boundingBox();
    assert.ok(confirmBox.y + confirmBox.height <= 900, `confirm step inside the viewport (${Math.round(confirmBox.y)})`);
    await dashboard.screenshot({ path: path.join(shots, '2-active-confirm.png') });
    // 계속하기 (Enter on the focused button) closes the step and returns to the site; Escape does the same.
    await dashboard.keyboard.press('Enter');
    await waitFocus('study-frame', 3000);
    state = await a11y();
    assert.deepEqual([state['study-end'].shown, state['study-end-yes'].shown, state['study-end-no'].shown], [true, false, false]);
    assert.deepEqual(state.order, ['study-frame']);
    await dashboard.locator('#study-end').click();
    await waitFocus('study-end-no', 3000);
    await dashboard.keyboard.press('Escape');
    await waitFocus('study-frame', 3000);
    assert.equal(await phase(), 'active');
    assert.equal((await api('/api/active-session')).session?.status, 'active');
    check('과업 끝내기 click -> "과업을 끝낼까요?" with focus on 계속하기; 계속하기 (Enter) and Escape return focus to the frame, still recording');
    const session = (await api('/api/active-session')).session;
    assert.equal(session.study.order, 1);
    assert.equal(session.study.ui_mode, 'single_window');

    // Keyboard inside the copy; the extension records only that frame.
    await dashboard.locator('#study-frame').focus();
    const eventsBefore = (await api(`/api/sessions/${session.id}/events`)).events.filter(e => e.source === 'browser').length;
    const pageCount = context.pages().length;
    for (let i = 0; i < 4; i++) await frame.locator('body').press('Tab');
    await frame.locator('body').press('Enter').catch(() => {});
    await sleep(1500);
    if (await phase() !== 'active') throw new Error('site navigation ended the task unexpectedly');
    const popup = await frame.locator('body').evaluate(() => window.open('https://example.invalid/') === null);
    assert.equal(popup, true);
    assert.equal(context.pages().length, pageCount);
    check('sandbox blocks window.open from the copy; no new page');
    const events = (await api(`/api/sessions/${session.id}/events`)).events;
    const browser = events.filter(e => e.source === 'browser');
    assert.ok(browser.length > eventsBefore, `recording continued after 계속하기 (${eventsBefore} -> ${browser.length})`);
    assert.ok(browser.every(e => e.url.startsWith(`${hostedOrigin}/`)), 'all browser URLs are the copy');
    assert.ok(browser.filter(e => e.type !== 'page_ready' || e.payload.kind !== 'study_recording_start').every(e => Number.isInteger(e.payload.frame_id) && e.payload.frame_id !== 0));
    check(`browser events only from the copy frame (${browser.length} events, frame ids tagged)`);

    // Windows pilot 10/28: Alt+Home reopens the task's first page and Alt+Left goes back inside
    // the frame, never past that page and never out of the recorder.
    const copyUrl = () => dashboard.frames().find(f => f.parentFrame() === dashboard.mainFrame())?.url();
    await frame.locator('body').press('Alt+Home');
    await waitFor(async () => copyUrl() === session.target_url && (await api(`/api/sessions/${session.id}/events`)).events.some(e => e.type === 'site_navigation'), 'Alt+Home');
    let backs = 0;
    for (; backs < 6; backs++) {
      await dashboard.frameLocator('#study-frame').locator('body').press('Alt+ArrowLeft');
      await sleep(700);
      if (/더 돌아갈 페이지가 없습니다/.test(await dashboard.locator('#study-site-status').textContent())) break;
    }
    assert.ok(backs < 6, 'back stops at the task start page');
    assert.equal(copyUrl(), session.target_url);
    assert.equal(new URL(dashboard.url()).origin, recorderBase);
    assert.equal(await phase(), 'active');
    const navigationLog = (await api(`/api/sessions/${session.id}/events`)).events.filter(e => e.type === 'site_navigation').map(e => e.payload);
    assert.deepEqual(navigationLog.at(0), { action: 'start', trigger: 'site_key', moved: true });
    assert.deepEqual(navigationLog.at(-1), { action: 'back', trigger: 'site_key', moved: false });
    assert.equal(await dashboard.locator('#study-site-back').isVisible(), true);
    check(`Alt+Home -> task start page; Alt+Left went back ${backs} time(s) then stopped at the start page (announced); recorder stays; site_navigation logged`);

    // 과업 끝내기 button -> 끝내기 -> result form.
    await dashboard.locator('#study-end').click();
    await waitFocus('study-end-no', 3000);
    await dashboard.locator('#study-end-yes').click();
    await waitPhase('answering');
    await waitFocus('study-result-heading');
    assert.equal(await dashboard.locator('#study-frame').getAttribute('inert'), '');
    assert.equal(await dashboard.locator('#study-frame').getAttribute('aria-hidden'), 'true');
    assert.equal(await dashboard.title(), 'A11y Task Recorder');
    state = await a11y();
    assert.deepEqual([state['study-end'].shown, state['study-end-yes'].shown, state['study-submit'].tabbable, state['study-next'].shown, state['study-frame'].tabbable], [false, false, true, false, false]);
    assert.deepEqual([state['study-return'].tabbable, state['study-redo'].tabbable, state['study-restart'].shown, state['study-tester-change'].shown], [true, true, false, false]);
    await dashboard.evaluate(() => document.querySelector('#study-result').scrollIntoView({ block: 'center', behavior: 'instant' }));
    await dashboard.screenshot({ path: path.join(shots, '3-result.png') });
    check('과업 끝내기 -> 끝내기 -> answering: heading focused, frame inert/aria-hidden, end button and confirm gone, 저장 tabbable, 다음 과업 not yet');
    const rejected = await fetch(`${recorderBase}/api/events`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: session.id, source: 'nvda', type: 'input', timestamp: new Date(Date.now() + 60000).toISOString(), payload: {} }) });
    assert.equal(rejected.status, 409);
    check('events after task end are refused (409)');
    await dashboard.locator('input[name="study-done"][value="completed"]').check();
    await dashboard.locator('#study-answer').fill('ENGINEERING REHEARSAL answer');
    await dashboard.locator('#study-submit').click();
    await waitPhase('ended');
    await waitFocus('study-saved');
    state = await a11y();
    assert.deepEqual([state['study-next'].tabbable, state['study-edit'].tabbable, state['study-end'].shown, state['study-submit'].shown], [true, true, false, false]);
    let record = await api(`/api/sessions/${session.id}/export.json`);
    assert.equal(record.study_outcome, 'completed');
    assert.equal(record.study_answer_entered_by, 'participant');
    assert.equal(record.end_reason, 'participant_button');
    assert.ok(record.task_ended_at && record.net_task_seconds >= 0);
    const types = record.events.filter(e => e.source === 'dashboard').map(e => e.type);
    for (const expected of ['instruction_presented', 'signal', 'task_end', 'answer_saved']) assert.ok(types.includes(expected), expected);
    check('participant answer saved (end_reason participant_button); 다음 과업 and 결과 수정 tabbable in the result area');

    // Upload with the built-in code: sent once, duplicate flush adds nothing, correction is revision 2.
    let uploads = await waitFor(async () => { const u = (await api('/api/uploads')).uploads; return u.sent === 1 ? u : null; }, 'upload sent');
    assert.equal(uploads.pending, 0);
    const indexPath = path.join(temp, 'uploads', 'index.jsonl');
    const lineCount = () => fs.readFileSync(indexPath, 'utf8').trim().split('\n').length;
    assert.equal(lineCount(), 1);
    await api('/api/uploads/flush', 'POST', {});
    assert.equal(lineCount(), 1);
    check('session uploaded once with the built-in code; duplicate flush stores nothing');
    await dashboard.locator('#study-edit').click();
    await waitFocus('study-result-heading');
    await dashboard.locator('input[name="study-done"][value="blocked"]').check();
    await dashboard.locator('#study-submit').click();
    try {
      await dashboard.waitForFunction(() => document.querySelector('#study-message').textContent.includes('고쳐'), null, { timeout: 15000 });
    } catch (error) {
      console.error('debug edit:', JSON.stringify(await dashboard.evaluate(() => ({ message: document.querySelector('#study-message').textContent,
        phase: document.querySelector('#study-view').dataset.phase, checked: document.querySelector('input[name="study-done"]:checked')?.value,
        formHidden: document.querySelector('#study-answer-form').hidden, submitDisabled: document.querySelector('#study-submit').disabled,
        active: document.activeElement?.id }))));
      throw error;
    }
    record = await api(`/api/sessions/${session.id}/export.json`);
    assert.equal(record.study_outcome, 'blocked');
    assert.equal(record.answer_history.length, 2);
    await waitFor(async () => lineCount() === 2, 'revision upload');
    const lines = fs.readFileSync(indexPath, 'utf8').trim().split('\n').map(JSON.parse);
    assert.equal(lines[1].revision, 2);
    assert.equal(lines[1].sessionId, session.id);
    check('corrected answer uploaded as revision 2 of the same session');

    // Task 2: 다음 과업 prepares without a new login; 4 s limit ends and saves by itself.
    await dashboard.locator('#study-next').click();
    await waitFocus('study-instruction', 40000);
    state = await a11y();
    assert.deepEqual([state['study-start'].tabbable, state['study-practice'].shown], [true, false]);
    check('다음 과업 -> second task ready (cookie kept), 연습 시작 only before the first task');
    await stopHosted(hosted);
    await waitFor(async () => { try { await fetch(`${hostedOrigin}/healthz`); return false; } catch { return true; } }, 'hosted server down');
    await dashboard.locator('#study-start').click();
    await waitPhase('active');
    await waitPhase('ended', 25000);
    await waitFocus('study-next');
    const second = (await api('/api/study/T01')).tasks[1];
    record = await api(`/api/sessions/${second.session_id}/export.json`);
    assert.deepEqual([record.end_reason, record.study_outcome, record.study_answer, record.study_answer_entered_by], ['time_limit', 'timeout', '', 'system']);
    assert.match(await dashboard.locator('#study-message').textContent(), /제한 시간이 되어 과업을 마쳤습니다/);
    assert.match(await dashboard.locator('#study-next').getAttribute('aria-describedby'), /study-saved/);
    assert.equal((await a11y())['study-edit'].shown, false);
    check('time limit: automatic end + save (timeout, empty answer, entered_by system); focus on 다음 과업');
    try {
      uploads = await waitFor(async () => { const u = (await api('/api/uploads')).uploads; return u.items.find(i => i.session_id === second.session_id)?.attempts ? u : null; }, 'failed attempt recorded');
    } catch (error) {
      console.error('debug uploads:', JSON.stringify((await api('/api/uploads')).uploads));
      throw error;
    }
    assert.equal(uploads.pending, 1);
    assert.equal(uploads.sent, 1);
    check('upload failure keeps the session queued without affecting the flow');
    hosted = spawnHosted();
    await waitFor(async () => (await fetch(`${hostedOrigin}/healthz`)).ok, 'hosted restart');
    const flushed = await api('/api/uploads/flush', 'POST', {});
    assert.equal(flushed.result.sent, 1);
    assert.equal(flushed.uploads.pending, 0);
    assert.equal(lineCount(), 3);
    check('retry after server returns delivers the queued session');

    // Task 3: start by button, end by NVDA gesture, save, then everything is complete.
    await dashboard.locator('#study-next').click();
    await waitFocus('study-instruction', 40000);
    await dashboard.locator('#study-start').click();
    await waitPhase('active');
    const taskRepeat = (await api('/api/signals', 'POST', { kind: 'repeat', source: 'nvda_addon' })).speak;
    assert.equal(taskRepeat, `3번 과업: 화면에서 안내 문구를 찾아 주세요. ${END_HINT}`);
    assert.equal((await api('/api/signals', 'POST', { kind: 'end', source: 'nvda_addon' })).accepted, true);
    await waitPhase('answering');
    assert.equal(await dashboard.locator('#study-end-confirm').isHidden(), true);
    await dashboard.locator('input[name="study-done"][value="completed"]').check();
    await dashboard.locator('#study-submit').click();
    await waitPhase('complete');
    await dashboard.waitForFunction(() => document.activeElement === document.querySelector('#study-complete h2'));
    assert.match(await dashboard.locator('#study-complete').textContent(), /더 하실 일은 없습니다/);
    const third = (await api('/api/study/T01')).tasks[2];
    assert.equal((await api(`/api/sessions/${third.session_id}`)).session.end_reason, 'participant_gesture');
    await waitFor(async () => lineCount() === 4, 'third upload');
    check('third task: button start, T repeat ends with the E hint, NVDA end without a confirm step, saved; complete notice says uploads are automatic');
    const listing = spawn('npx', ['tsx', 'scripts/list-uploads.ts', '--dir', path.join(temp, 'uploads'), '--json'], { cwd: LAB });
    let listed = '';
    listing.stdout.on('data', d => listed += d);
    await new Promise(r => listing.once('exit', r));
    assert.equal(JSON.parse(listed).sessions, 3);
    check('list-uploads CLI reports 3 sessions / 4 files');

    assert.ok(!recorderOut.includes(ACCESS_CODE), 'recorder output never prints the code');
    assert.match(recorderOut, /사이트 접속 코드: 키트에 내장됨/);
    await dashboard.setViewportSize({ width: 390, height: 844 });
    assert.equal(await dashboard.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await dashboard.screenshot({ path: path.join(output, 'complete-mobile.png'), fullPage: true });
    await dashboard.setViewportSize({ width: 1366, height: 900 });

    // Researcher-only tester change: a plain reload keeps the picker hidden; ?change-tester=1 shows it; ?tester= reopens.
    await dashboard.goto(recorderBase);
    await waitPhase('complete');
    assert.equal(await dashboard.locator('#study-tester-form').isHidden(), true);
    await dashboard.goto(`${recorderBase}/?change-tester=1`);
    await dashboard.waitForFunction(() => document.querySelector('#study-tester').options.length > 1);
    assert.equal(await phase(), 'idle');
    assert.deepEqual([await dashboard.locator('#study-tester-form').isVisible(), await dashboard.locator('#study-records').isVisible()], [true, true]);
    await dashboard.goto(`${recorderBase}/?tester=T01`);
    await waitPhase('complete');
    assert.equal(await dashboard.locator('#study-tester-form').isHidden(), true);
    check('tester picker: hidden after reload, shown with ?change-tester=1, ?tester=T01 reopens locked');

    // ---- 0.4.4: tester change, restart, return to the task -------------------------------
    const sentBeforeUndo = lineCount();
    const openPicker = async () => {
      await dashboard.locator('#study-tester-change').click();
      if (await dashboard.locator('#study-tester-confirm').isVisible()) {
        await waitFocus('study-tester-no', 3000);
        await dashboard.locator('#study-tester-yes').click();
      }
      await waitFocus('study-tester', 3000);
    };
    state = await a11y();
    assert.equal(state['study-tester-change'].tabbable, true);
    assert.equal(await dashboard.locator('#study-tester-change').textContent(), '테스터 번호 바꾸기 (지금 T01)');
    await dashboard.locator('#study-tester-change').click();
    await waitFocus('study-tester-no', 3000);
    assert.equal(await dashboard.locator('#study-tester-confirm').getAttribute('role'), 'group');
    assert.equal(await dashboard.locator('#study-tester-question').textContent(), 'T01로 저장된 결과 3개는 지워지지 않고 그대로 남습니다. 다른 테스터 번호로 바꿀까요?');
    state = await a11y();
    assert.deepEqual([state['study-tester-change'].shown, state['study-tester-yes'].tabbable, state['study-tester-no'].tabbable], [false, true, true]);
    await dashboard.keyboard.press('Escape');
    await waitFocus('study-tester-change', 3000);
    await dashboard.locator('#study-tester-change').click();
    await waitFocus('study-tester-no', 3000);
    await dashboard.keyboard.press('Enter');
    await waitFocus('study-tester-change', 3000);
    assert.equal(await dashboard.locator('#study-tester-form').isHidden(), true);
    await openPicker();
    assert.equal(await dashboard.locator('#study-tester-keep').isVisible(), true);
    await dashboard.locator('#study-tester-keep').click();
    await waitFocus('study-tester-change', 3000);
    assert.equal(await dashboard.locator('#study-tester-form').isHidden(), true);
    await openPicker();
    await dashboard.locator('#study-tester').selectOption('T02');
    await dashboard.locator('#study-select').click();
    await waitFocus('study-instruction', 40000);
    assert.equal(await dashboard.locator('#study-order').textContent(), 'T02 · 과업 1 / 2');
    assert.equal((await api('/api/study/T01')).finished, 3);
    check('tester change with saved results: confirm step (group, focus 취소; Escape and 취소 keep T01), 번호 바꾸기 shows the picker, 바꾸지 않기 keeps T01; T02 prepared; T01 results untouched');
    const wrongDraft = (await api('/api/study/T02')).tasks[0].session_id;
    await dashboard.locator('#study-tester-change').click();
    await waitFocus('study-tester', 3000);
    await dashboard.locator('#study-tester').selectOption('T01');
    await dashboard.locator('#study-select').click();
    await waitPhase('complete');
    const cancelledDraft = (await api(`/api/sessions/${wrongDraft}`)).session;
    assert.deepEqual([cancelledDraft.status, cancelledDraft.study_outcome, cancelledDraft.abort_reason, cancelledDraft.study_attempt.analysis],
      ['aborted', 'cancelled', 'tester_change', 'exclude']);
    await openPicker();
    await dashboard.locator('#study-tester').selectOption('T02');
    await dashboard.locator('#study-select').click();
    await waitFocus('study-instruction', 40000);
    const t02First = (await api('/api/study/T02')).tasks[0].session_id;
    assert.notEqual(t02First, wrongDraft);
    assert.deepEqual((await api(`/api/sessions/${t02First}`)).session.study_attempt, { attempt: 2, retry_of: wrongDraft, next_attempt: null, analysis: 'include' });
    check('tester change with nothing saved: no confirm step; the unstarted draft is kept as aborted/cancelled (tester_change); reopening T02 makes attempt 2');

    // Practice: 과업으로 돌아가기 and 처음부터 다시 하기 work there too (nothing recorded).
    await dashboard.locator('#study-practice').click();
    await waitFocus('study-instruction');
    await dashboard.locator('#study-start').click();
    await waitPhase('active');
    await api('/api/signals', 'POST', { kind: 'end', source: 'nvda_addon' });
    await waitPhase('answering');
    await dashboard.locator('#study-return').click();
    await waitPhase('active');
    await waitFocus('study-frame', 3000);
    await dashboard.locator('#study-restart').click();
    await waitFocus('study-restart-no', 3000);
    await dashboard.locator('#study-restart-yes').click();
    await waitPhase('ready');
    await waitFocus('study-instruction', 3000);
    assert.match(await dashboard.locator('#study-message').textContent(), /연습을 처음부터 다시 합니다/);
    await dashboard.locator('#study-start').click();
    await waitPhase('active');
    await api('/api/signals', 'POST', { kind: 'end', source: 'nvda_addon' });
    await waitPhase('answering');
    await dashboard.locator('input[name="study-done"][value="completed"]').check();
    await dashboard.locator('#study-submit').click();
    await waitFocus('study-saved');
    await dashboard.locator('#study-practice-stop').click();
    await waitFocus('study-instruction', 40000);
    assert.equal((await api('/api/study/T02')).tasks[0].session_id, t02First);
    check('practice: 과업으로 돌아가기 and 과업 다시 시작 return to the practice task or its instruction; no session created');

    // Restart during a task: 과업 다시 시작 below the site (out of the Tab order) asks first.
    await dashboard.locator('#study-start').click();
    await waitPhase('active');
    await waitFocus('study-frame');
    const firstAttempt = (await api('/api/active-session')).session;
    state = await a11y();
    assert.deepEqual([state['study-restart'].shown, state['study-restart'].ariaHidden, state['study-restart'].tabbable], [true, false, false]);
    assert.deepEqual(state.order, ['study-frame']);
    await dashboard.locator('#study-restart').click();
    await waitFocus('study-restart-no', 3000);
    state = await a11y();
    assert.deepEqual(state.order, ['study-frame', 'study-restart-yes', 'study-restart-no']);
    assert.deepEqual([state['study-end'].shown, state['study-restart'].shown], [false, false]);
    const restartBox = await dashboard.locator('#study-restart-confirm').boundingBox();
    assert.ok(restartBox.y + restartBox.height <= 900, `restart confirm inside the viewport (${Math.round(restartBox.y)})`);
    await dashboard.screenshot({ path: path.join(shots, '4-restart-confirm.png') });
    await dashboard.keyboard.press('Escape');
    await waitFocus('study-frame', 3000);
    assert.equal((await api('/api/active-session')).session?.id, firstAttempt.id);
    await dashboard.locator('#study-restart').click();
    await waitFocus('study-restart-no', 3000);
    await dashboard.locator('#study-restart-yes').click();
    await waitPhase('ready');
    await waitFocus('study-instruction', 40000);
    assert.match(await dashboard.locator('#study-message').textContent(), /과업을 처음부터 다시 합니다/);
    const restarted = (await api(`/api/sessions/${firstAttempt.id}`)).session;
    assert.deepEqual([restarted.status, restarted.study_outcome, restarted.abort_reason, restarted.end_reason, restarted.study_attempt.analysis],
      ['aborted', 'restarted', 'restart', 'restart', 'exclude']);
    assert.equal((await api('/api/active-session')).session, null);
    const secondAttempt = (await api('/api/study/T02')).tasks[0].session_id;
    assert.notEqual(secondAttempt, firstAttempt.id);
    assert.equal((await api(`/api/sessions/${secondAttempt}`)).session.study_attempt.retry_of, firstAttempt.id);
    check('과업 다시 시작 during a task: in-page confirm (focus 계속하기, Escape keeps recording); 다시 시작 keeps the attempt as aborted/restarted and prepares a new attempt with focus on the instruction');

    // End pressed by mistake: 과업으로 돌아가기 resumes recording on the same page.
    await dashboard.locator('#study-start').click();
    await waitPhase('active');
    await waitFocus('study-frame');
    await frame.locator('body').press('Tab');
    await api('/api/signals', 'POST', { kind: 'end', source: 'nvda_addon' });
    await waitPhase('answering');
    await waitFocus('study-result-heading');
    state = await a11y();
    assert.deepEqual(state.order.slice(-3), ['study-submit', 'study-return', 'study-redo']);
    await dashboard.locator('#study-return').focus();
    await dashboard.keyboard.press('Enter');
    await waitPhase('active');
    await waitFocus('study-frame', 3000);
    await dashboard.waitForFunction(() => /과업으로 돌아왔습니다/.test(document.querySelector('#study-site-status').textContent), null, { timeout: 3000 });
    let resumed = (await api(`/api/sessions/${secondAttempt}`)).session;
    assert.deepEqual([resumed.status, resumed.end_reason], ['active', '']);
    assert.ok(resumed.paused_seconds > 0, `result-form time not counted (${resumed.paused_seconds}s)`);
    // Recording is open again: an NVDA event is taken as a normal event, and a page change in the
    // copy (Alt+Home) is recorded by the extension.
    const nvdaAfter = await fetch(`${recorderBase}/api/events`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: secondAttempt, source: 'nvda', type: 'input', timestamp: new Date().toISOString(), payload: { gesture: 'tab' } }) });
    assert.equal(nvdaAfter.status, 201);
    assert.notEqual((await nvdaAfter.json()).event.payload.received_after_stop, true);
    const browserCount = async () => (await api(`/api/sessions/${secondAttempt}/events`)).events.filter(e => e.source === 'browser' && !e.payload.received_after_stop).length;
    const beforeResume = await browserCount();
    await frame.locator('body').press('Alt+Home');
    await waitFor(async () => (await browserCount()) > beforeResume, 'recording after return');
    check(`end by mistake: result form offers 과업으로 돌아가기 and 처음부터 다시 하기 after 저장 (group "잘못 끝냈을 때"); return resumes recording on the same page, announced, result-form time excluded (${resumed.paused_seconds}s)`);

    // 처음부터 다시 하기 from the result form asks first, then a third attempt is saved normally.
    await api('/api/signals', 'POST', { kind: 'end', source: 'nvda_addon' });
    await waitPhase('answering');
    await dashboard.locator('#study-redo').click();
    await waitFocus('study-redo-no', 3000);
    state = await a11y();
    assert.deepEqual([state['study-return'].shown, state['study-redo-yes'].tabbable, state['study-redo-no'].tabbable], [false, true, true]);
    await dashboard.keyboard.press('Escape');
    await waitFocus('study-redo', 3000);
    assert.equal(await phase(), 'answering');
    await dashboard.locator('#study-redo').click();
    await waitFocus('study-redo-no', 3000);
    await dashboard.locator('#study-redo-yes').click();
    await waitFocus('study-instruction', 40000);
    const redone = (await api(`/api/sessions/${secondAttempt}`)).session;
    assert.deepEqual([redone.status, redone.study_outcome, redone.end_reason], ['aborted', 'restarted', 'participant_gesture']);
    assert.deepEqual((await api(`/api/sessions/${secondAttempt}/events`)).events.filter(e => e.source === 'dashboard').map(e => e.type).filter(t => t.startsWith('task_')),
      ['task_end', 'task_resume', 'task_end', 'task_abort']);
    await dashboard.locator('#study-start').click();
    await waitPhase('active');
    await api('/api/signals', 'POST', { kind: 'end', source: 'nvda_addon' });
    await waitPhase('answering');
    await dashboard.locator('input[name="study-done"][value="completed"]').check();
    await dashboard.locator('#study-submit').click();
    await waitPhase('ended');
    const thirdAttempt = (await api('/api/study/T02')).tasks[0];
    assert.equal(thirdAttempt.outcome, 'completed');
    assert.equal((await api(`/api/sessions/${thirdAttempt.session_id}`)).session.study_attempt.attempt, 4);
    await waitFor(async () => lineCount() === sentBeforeUndo + 1, 'only the saved attempt is uploaded');
    const uploadedIds = (await api('/api/uploads')).uploads.items.map(item => item.session_id);
    assert.ok(![firstAttempt.id, secondAttempt, wrongDraft].some(id => uploadedIds.includes(id)), 'aborted attempts are not uploaded');
    check('처음부터 다시 하기: confirm (focus 취소, Escape stays), attempt kept with its end reason; the next attempt saves normally and only it is uploaded');

    // Researcher side: the package keeps aborted attempts apart and lists every attempt.
    const pkg = (await api('/api/export-package', 'POST', { open_folder: false })).package;
    assert.equal(pkg.aborted_session_count, 2);
    const pkgListing = JSON.parse(execFileSync('python3', ['-c', [
      'import csv, io, json, sys, zipfile',
      'z = zipfile.ZipFile(sys.argv[1])',
      'rows = list(csv.DictReader(io.StringIO(z.read("attempts.csv").decode("utf-8-sig"))))',
      'print(json.dumps({"names": z.namelist(), "rows": rows, "info": z.read("패키지정보.txt").decode("utf-8")}))'].join('\n'), pkg.path], { encoding: 'utf8' }));
    assert.equal(pkgListing.names.filter(n => n.startsWith('sessions/aborted/') && n.endsWith('.json')).length, 2);
    const byId = Object.fromEntries(pkgListing.rows.map(r => [r.session_id, r]));
    assert.deepEqual([byId[wrongDraft].analysis, byId[firstAttempt.id].analysis, byId[secondAttempt].analysis, byId[thirdAttempt.session_id].analysis],
      ['exclude', 'exclude', 'exclude', 'include']);
    assert.match(pkgListing.info, /다시 시작으로 취소된 시도: 2건/);
    check('result package: sessions/aborted/ holds the 2 restarted attempts, attempts.csv marks 3 aborted attempts analysis=exclude');

    // Back to T01 from the saved state (ended), so the run below opens T01 again.
    await dashboard.goto(`${recorderBase}/?tester=T02`);
    await waitPhase('ended');
    await openPicker();
    await dashboard.locator('#study-tester').selectOption('T01');
    await dashboard.locator('#study-select').click();
    await waitPhase('complete');
    check('tester change from a saved task (ended) back to T01');

    // Kit built without a code (not for distribution): nobody is ever asked for one. Same port
    // (the copy's CSP lists it), fresh DB, cookies cleared: preparation stops with the site error,
    // the frame and its login page stay hidden, and nothing is uploaded.
    await stopRecorder();
    await context.clearCookies();
    const noCodeEnv = { ...process.env, A11Y_RECORDER_UPLOAD_URL: `${hostedOrigin}/upload/session`, PYTHONUNBUFFERED: '1' };
    delete noCodeEnv.A11Y_RECORDER_ACCESS_CODE;
    delete noCodeEnv.A11Y_RECORDER_ACCESS_CODE_FILE;
    recorder = spawn('python3', ['run.py', '--no-browser', '--port', String(recorderPort), '--db', path.join(temp, 'recorder-nocode.sqlite3'), '--catalog', path.join(temp, 'catalog.json')],
      { env: noCodeEnv, stdio: ['ignore', 'pipe', 'pipe'] });
    let noCodeOut = '';
    recorder.stdout.on('data', d => { noCodeOut += d; });
    await waitFor(async () => (await fetch(`${recorderBase}/api/health`)).ok, 'recorder without code');
    assert.equal((await api('/api/study/access')).code, null);
    await dashboard.goto(recorderBase);
    try {
      await dashboard.waitForFunction(() => /사이트에 접속할 수 없습니다/.test(document.querySelector('#study-message').textContent), null, { timeout: 40000 });
    } catch (error) {
      console.error('debug message:', await dashboard.locator('#study-message').textContent());
      throw error;
    }
    const noCodeState = async () => {
      const s = await a11y();
      assert.deepEqual([s['study-frame'].shown, s['study-start'].shown, s['study-tester'].shown], [false, false, false]);
      assert.equal(await dashboard.locator('#study-message').evaluate(n => n.classList.contains('error')), true);
      assert.equal(await dashboard.evaluate(() => document.querySelector('#study-view').innerText.includes('접속 코드')), false);
    };
    await noCodeState();
    assert.match(await dashboard.locator('#study-message').textContent(), /사이트에 접속할 수 없습니다\(로그인 필요\)/);
    const noCodeDraft = (await api('/api/study/T01')).tasks[0].session_id;
    const noCodePrep = (await api(`/api/sessions/${noCodeDraft}/events`)).events.filter(e => e.type === 'site_preparation').map(e => e.payload);
    assert.deepEqual(noCodePrep.map(p => [p.result, p.cause_label, p.reason, p.login]), [['failed', '로그인 필요', 'login_required', 'no_code']]);
    assert.match(noCodeOut, /사이트 준비 실패: 테스터 T01 과업 1 · 로그인 필요 \(login_required, no_code\)/);
    // The background check keeps the error up instead of ever offering the login page.
    await sleep(5000);
    await noCodeState();
    await dashboard.screenshot({ path: path.join(output, 'no-code-error.png') });
    check('kit without a code: no code prompt anywhere; frame (login page) stays hidden; "사이트에 접속할 수 없습니다(로그인 필요)" stays up; site_preparation cause 로그인 필요 (no_code) and one server-window line');
    const noCodeUploads = (await api('/api/uploads')).uploads;
    assert.deepEqual([noCodeUploads.configured, noCodeUploads.code_source], [false, '']);
    assert.equal((await api('/api/uploads/flush', 'POST', {})).result.skipped, 'not_configured');
    assert.equal(lineCount(), sentBeforeUndo + 1);
    assert.match(noCodeOut, /사이트 접속 코드: 내장되지 않음/);
    check('kit without a code: uploads not configured, nothing reaches the server; server window says the code is not built in');
    assert.deepEqual(errors, []);
    const result = { status: 'PASS', actual_windows_nvda: 'NOT_RUN', human_data: false,
      evidence: 'real Chromium extension + real recorder + real hosted server (temp ports); synthetic NVDA heartbeat; participant-only flow', checks };
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result, null, 2));
  } finally {
    clearInterval(heartbeat);
    if (context) await context.close();
    await stopRecorder();
    await stopHosted(hosted);
    fs.rmSync(temp, { recursive: true, force: true });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
