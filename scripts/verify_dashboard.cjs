// Optional browser smoke using an already installed Playwright module.
// All NVDA traffic here is synthetic and cannot validate real NVDA behavior.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'recorder-dashboard-'));
  const output = path.resolve('dist/qa');
  fs.mkdirSync(output, { recursive: true });
  const port = process.env.RECORDER_QA_PORT || '28766';
  const base = `http://127.0.0.1:${port}`;
  const server = spawn(process.env.PYTHON || 'python', ['run.py', '--no-browser', '--port', port,
    '--db', path.join(temporary, 'data', 'recorder.sqlite3')], { stdio: 'ignore' });
  let browser;
  try {
    for (let attempt = 0; attempt < 50; attempt++) {
      if (server.exitCode !== null) throw new Error('Temporary collector failed to start');
      try { if ((await fetch(`${base}/api/health`)).ok) break; } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base);
    await page.locator('[data-action=operator-home]').click();
    await page.locator('#title').fill('Synthetic dashboard QA');
    await page.locator('#participant').fill('ENGINEERING-CHECK');
    await page.locator('#session-form button[type=submit]').click();
    await page.waitForURL(/session=/);
    const id = new URL(page.url()).searchParams.get('session');
    await page.locator('#start-button').click();
    await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('NVDA가 연결되지 않아'));
    const request = async (endpoint, method = 'GET', body) => {
      const response = await fetch(`${base}/api${endpoint}`, {
        method, headers: { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      assert.ok(response.ok, `${method} ${endpoint}: ${response.status}`);
      return response.json();
    };
    assert.equal((await request(`/sessions/${id}`)).session.status, 'draft');
    await fetch(`${base}/api/active-session`, { headers: { 'X-A11y-Client': 'nvda-addon' } });
    await page.locator('#start-button').click();
    await page.waitForFunction(() => document.querySelector('#session-status').textContent === '기록 중');
    await page.locator('#step-title').fill('Synthetic step');
    await page.locator('#step-form button[type=submit]').click();
    const session = (await request(`/sessions/${id}`)).session;
    await request(`/sessions/${id}/steps/${session.steps[0].id}/start`, 'POST', {});
    await request('/events', 'POST', { session_id: id, source: 'nvda', type: 'input',
      payload: { identifiers: ['kb:tab'], gesture: 'Tab', evidence_kind: 'synthetic' } });
    await page.locator('[data-action=refresh-session]').click();
    await page.locator('#hint-text').fill('Synthetic hint');
    await page.locator('#hint-form button[type=submit]').click();
    await page.locator('#stop-button').click();
    await page.waitForFunction(() => document.querySelector('#session-status').textContent === '완료');
    const exported = await (await fetch(`${base}/api/sessions/${id}/export.json`)).json();
    assert.ok(exported.events.some(event => event.type === 'input'));
    assert.ok(exported.events.some(event => event.type === 'hint'));
    const rejected = await fetch(`${base}/api/events`, { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
        session_id: id, source: 'nvda', type: 'input', allow_inactive: true,
      }) });
    assert.equal(rejected.status, 409);
    const packaged = await request('/export-package', 'POST', { open_folder: false });
    assert.equal(packaged.package.session_count, 1);
    assert.ok(fs.existsSync(packaged.package.path));
    fs.copyFileSync(packaged.package.path, path.join(output, 'synthetic-result-package.zip'));
    await page.screenshot({ path: path.join(output, 'dashboard-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(output, 'dashboard-mobile.png'), fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.deepEqual(errors, []);
    const result = { status: 'PASS', evidence_kind: 'Chromium dashboard with synthetic NVDA API traffic',
      actual_nvda_validation: 'NOT_RUN', events: exported.events.length, checks: [
        'disconnected start blocked', 'create/start/step/hint/stop', 'JSON export',
        'post-stop rejection', 'result package', 'mobile horizontal overflow', 'no page errors',
      ] };
    fs.writeFileSync(path.join(output, 'dashboard-smoke.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result));
  } finally {
    if (browser) await browser.close();
    server.kill();
    if (server.exitCode === null) await new Promise(resolve => server.once('exit', resolve));
    fs.rmSync(temporary, { recursive: true, force: true });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
