// Real Chromium extension + hosted pages, synthetic NVDA heartbeat. No human evidence.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const base = 'http://127.0.0.1:8765';
  try { await fetch(base + '/api/health'); throw new Error('Port 8765 already occupied; refusing to touch existing recorder'); }
  catch (error) { if (!String(error.cause || error).includes('ECONNREFUSED')) throw error; }
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'recorder-extension-'));
  const output = path.resolve('dist/qa/study-extension');
  fs.mkdirSync(output, { recursive: true });
  const server = spawn('python3', ['run.py', '--no-browser', '--db', path.join(temp, 'recorder.sqlite3')]);
  let context, heartbeat;
  const checks = [];
  try {
    for (let i = 0; i < 60; i++) {
      try { if ((await fetch(base + '/api/health')).ok) break; } catch {}
      await new Promise(r => setTimeout(r, 100));
    }
    const extension = path.resolve('browser-extension');
    context = await chromium.launchPersistentContext(path.join(temp, 'chrome-profile'), {
      headless: true, channel: 'chromium', viewport: { width: 1366, height: 900 },
      args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`],
    });
    const dashboard = await context.newPage();
    await dashboard.goto(base);
    await dashboard.locator('#study-tester').selectOption('T18');
    await dashboard.locator('#study-select').click();
    await dashboard.locator('#study-prepare').click();
    for (let i = 0; i < 100 && context.pages().filter(p => p.url().startsWith('https://')).length === 0; i++) await dashboard.waitForTimeout(100);
    const login = context.pages().find(p => p.url().startsWith('https://'));
    assert.ok(login, 'Extension should create hosted tab');
    await login.locator('#code').waitFor();
    await dashboard.waitForFunction(() => document.querySelector('#study-message').textContent.includes('접속 코드'));
    assert.equal((await (await fetch(base + '/api/active-session')).json()).session, null);
    checks.push('real login gate before recording');
    const code = fs.readFileSync(process.env.STUDY_ACCESS_CODE_FILE || '/home/ys/.local/share/a11y-study-hosting/access-code.txt', 'utf8').trim();
    await login.locator('#code').fill(code);
    await login.locator('button[type=submit]').click();
    await dashboard.bringToFront();
    await dashboard.locator('#study-start').waitFor({ timeout: 30000 });
    heartbeat = setInterval(() => fetch(base + '/api/active-session', { headers: { 'X-A11y-Client': 'nvda-addon' } }).catch(() => {}), 1500);
    await fetch(base + '/api/active-session', { headers: { 'X-A11y-Client': 'nvda-addon' } });
    const records = [];
    for (let order = 1; order <= 4; order++) {
      if (order > 1) {
        await dashboard.locator('#study-next').click();
        await dashboard.locator('#study-start').waitFor({ timeout: 30000 });
      }
      let taskPage = context.pages().find(p => p.url().startsWith('https://'));
      assert.ok(taskPage);
      if (order === 2 || order === 4) {
        assert.equal(await taskPage.locator('#catalog-cart-quantity').inputValue(), '3');
        assert.equal(await taskPage.locator('#catalog-cart-name').innerText(), '팬트리 수납 바구니 1호');
        assert.equal(await taskPage.locator('#catalog-cart-dialog').evaluate(d => d.open), true);
        if (order === 2) {
          await taskPage.reload();
          await dashboard.bringToFront();
          await dashboard.waitForFunction(() => !document.querySelector('#study-prepare').hidden, { timeout: 15000 });
          await dashboard.locator('#study-prepare').click();
          await dashboard.locator('#study-start').waitFor({ timeout: 30000 });
          taskPage = context.pages().find(p => p.url().startsWith('https://'));
          assert.equal(await taskPage.locator('#catalog-cart-quantity').inputValue(), '3');
          checks.push('prepared cart reload recovers via fresh draft tab');
        }
        checks.push(`cart setup order ${order}, decrement label: ${await taskPage.locator('#catalog-cart-decrease').getAttribute('aria-label')}`);
      }
      const prepared = (await (await fetch(base + '/api/sessions')).json()).sessions.find(s => s.status === 'draft');
      assert.equal((await (await fetch(base + `/api/sessions/${prepared.id}/events`)).json()).events.length, 0);
      await dashboard.locator('#study-start').click();
      await dashboard.locator('#study-finish-form').waitFor({ timeout: 10000 });
      const session = (await (await fetch(base + '/api/active-session')).json()).session;
      assert.equal(session.study.order, order);
      assert.equal(session.study_setup.excluded_from_participant_recording, true);
      await taskPage.keyboard.press('Tab');
      await dashboard.bringToFront();
      await dashboard.locator('#study-answer').fill('ENGINEERING REHEARSAL ONLY');
      await dashboard.locator('#study-outcome').selectOption('blocked');
      await dashboard.locator('#study-finish').click();
      await dashboard.locator('#study-next').waitFor({ timeout: 12000 });
      const record = await (await fetch(base + `/api/sessions/${session.id}/export.json`)).json();
      assert.equal(record.study_evaluation.status, 'NOT_SCORED');
      assert.equal(record.events.filter(e => e.source === 'nvda').length, 0);
      assert.ok(record.events.some(e => e.source === 'browser'), 'Real extension emits browser evidence');
      records.push({ order, task_id: record.study.task_id, setup: record.study_setup,
        browser_events: record.events.filter(e => e.source === 'browser').length, human_data: false });
    }
    assert.equal(await dashboard.locator('#study-break').isVisible(), true);
    await dashboard.reload();
    await dashboard.locator('#study-next').waitFor();
    await dashboard.screenshot({ path: path.join(output, 'after-block-one.png'), fullPage: true });
    const result = { status: 'PASS', actual_windows_nvda: 'NOT_RUN', human_data: false,
      evidence: 'real Chromium extension + hosted HTTPS pages; synthetic NVDA heartbeat only', checks, records };
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result));
  } finally {
    clearInterval(heartbeat);
    if (context) await context.close();
    server.kill();
    if (server.exitCode === null) await new Promise(r => server.once('exit', r));
    fs.rmSync(temp, { recursive: true, force: true });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
