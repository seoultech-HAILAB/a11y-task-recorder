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
const { spawn } = require('node:child_process');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const LAB = process.env.A11Y_LAB_DIR || path.resolve('..', 'a11y-user-context-lab');
const MOUNT = '16a50b10a80c';
const PAGES = ['p/413127f760b3356b.html', 'p/15fd75c5d2246d23.html'];
const ACCESS_CODE = 'e2e-access-code-not-a-secret';
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
    'study-practice-stop', 'study-frame', 'study-tester', 'study-select', 'study-records', 'study-shortcuts']) {
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
    copyMount(path.join(LAB, 'runs/hosted-benchmark-v3-perfe-20261009/public/s', MOUNT), path.join(temp, 'public/s', MOUNT));
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
      testers: [{ id: 'T01', tasks: [task(1, 'index.html', 600, true), task(2, PAGES[0], 4, false), task(3, PAGES[1], 600, true)] }] };
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
    // Tester locked: the picker, 배정 확인 and 기록 관리 are gone; Tab reaches only 과업 시작 and 연습 시작.
    assert.deepEqual([state['study-tester'].shown, state['study-select'].shown, state['study-records'].shown], [false, false, false]);
    assert.deepEqual(state.order, ['study-start', 'study-practice']);
    await dashboard.keyboard.press('Tab');
    await waitFocus('study-start', 3000);
    await dashboard.keyboard.press('Tab');
    await waitFocus('study-practice', 3000);
    assert.equal(state['study-shortcuts'].shown, true);
    assert.equal(await dashboard.locator('#study-shortcuts').textContent(),
      '단축키: 끝내기는 NVDA, Ctrl, E. 지시문 다시 듣기는 NVDA, Ctrl, H.');
    await dashboard.locator('#study-instruction').focus();
    await dashboard.screenshot({ path: path.join(shots, '1-ready.png') });
    check('배정 확인 -> automatic prepare + hidden-form login (no code typed); focus on instruction; tester picker hidden; Tab order = 과업 시작, 연습 시작; shortcut line shown');

    // Practice with the same participant buttons; 연습 끝내기 prepares the real task again.
    await dashboard.locator('#study-practice').click();
    await waitFocus('study-instruction');
    assert.match(await dashboard.locator('#study-instruction').textContent(),
      /^연습입니다\. 연습 화면에서 '재료 보기' 버튼을 눌러, 첫 번째 재료를 알려 주세요\. 과업은 지시문 아래 '과업 시작' 버튼으로 시작합니다\. 끝낼 때는 NVDA, Ctrl, E를 함께 누르세요\. 지시문을 다시 듣고 싶으면 NVDA, Ctrl, H를 누르세요\./);
    const practiceRepeat = (await api('/api/signals', 'POST', { kind: 'repeat', source: 'nvda_addon' })).speak;
    assert.ok(practiceRepeat.includes('연습') && practiceRepeat.endsWith('끝낼 때는 NVDA, Ctrl, E를 함께 누르세요.'), practiceRepeat);
    await dashboard.locator('#study-start').click();
    await waitPhase('active');
    await waitFocus('study-frame');
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
    assert.equal(taskRepeat, '3번 과업: 화면에서 안내 문구를 찾아 주세요. 끝낼 때는 NVDA, Ctrl, E를 함께 누르세요.');
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
    // The background check keeps the error up instead of ever offering the login page.
    await sleep(5000);
    await noCodeState();
    await dashboard.screenshot({ path: path.join(output, 'no-code-error.png') });
    check('kit without a code: no code prompt anywhere; frame (login page) stays hidden; "사이트에 접속할 수 없습니다" error stays up');
    const noCodeUploads = (await api('/api/uploads')).uploads;
    assert.deepEqual([noCodeUploads.configured, noCodeUploads.code_source], [false, '']);
    assert.equal((await api('/api/uploads/flush', 'POST', {})).result.skipped, 'not_configured');
    assert.equal(lineCount(), 4);
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
