// Engineering-only UI rehearsal. Extension replies and NVDA heartbeat are synthetic.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'recorder-study-'));
  const output = path.resolve('dist/qa/study');
  fs.mkdirSync(output, { recursive: true });
  const base = 'http://127.0.0.1:28767';
  const server = spawn('python3', ['run.py', '--no-browser', '--port', '28767', '--db', path.join(temp, 'recorder.sqlite3')]);
  let browser;
  try {
    for (let i = 0; i < 60; i++) {
      try { if ((await fetch(base + '/api/health')).ok) break; } catch {}
      await new Promise(r => setTimeout(r, 100));
    }
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.addInitScript(() => {
      window.addEventListener('message', async event => {
        const m = event.data;
        if (m?.kind !== 'a11y-study-command') return;
        let result = { ok: true, ready: true, sessionId: m.sessionId, tabId: 123 };
        if (m.action === 'start') {
          const response = await fetch(`/api/sessions/${m.sessionId}/start`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ study_ready: true }),
          });
          const data = await response.json();
          result = response.ok
            ? window.simulateLostStart ? { ok: false, error: 'Synthetic lost start response' } : { ...result, started: true }
            : { ok: false, error: data.error };
        }
        window.postMessage({ kind: 'a11y-study-response', requestId: m.requestId, result }, location.origin);
      });
    });
    await page.goto(base);
    await page.locator('#study-tester').waitFor({ timeout: 3000 });
    assert.equal(await page.locator('#title').isVisible(), false);
    await page.locator('#study-tester').selectOption('T01');
    await page.locator('#study-select').click();
    await page.locator('#study-prepare').click();
    await page.waitForFunction(() => !document.querySelector('#study-start').hidden);
    await page.locator('#study-start').click();
    await page.waitForFunction(() => document.querySelector('#study-message').textContent.includes('NVDA'));
    assert.equal((await (await fetch(base + '/api/active-session')).json()).session, null);
    await fetch(base + '/api/active-session', { headers: { 'X-A11y-Client': 'nvda-addon' } });
    await page.locator('#study-start').click();
    await page.locator('#study-finish-form').waitFor();
    const session = (await (await fetch(base + '/api/active-session')).json()).session;
    assert.equal(session.participant, 'T01');
    assert.equal(session.study.order, 1);
    await page.locator('#study-answer').fill('ENGINEERING ONLY answer');
    await page.locator('#study-outcome').selectOption('blocked');
    await page.locator('#study-finish').click();
    await page.locator('#study-next').waitFor({ timeout: 10000 });
    const record = await (await fetch(base + `/api/sessions/${session.id}/export.json`)).json();
    assert.equal(record.study_outcome, 'blocked');
    assert.equal(record.study_answer, 'ENGINEERING ONLY answer');
    assert.equal(record.study_evaluation.status, 'NOT_SCORED');
    await page.reload();
    await page.locator('#study-next').waitFor();
    await page.locator('#study-next').click();
    await page.waitForFunction(() => document.querySelector('#study-order').textContent.includes('2 / 12'));
    await page.screenshot({ path: path.join(output, 'study-desktop.png'), fullPage: true });
    assert.equal(await page.locator('#study-view').innerText().then(t => /reference_answer|정상판|FE판|Actionability/.test(t)), false);
    await page.locator('#study-tester').selectOption('T02');
    await page.locator('#study-select').click();
    await page.waitForFunction(() => document.querySelector('#study-order').textContent.includes('1 / 12'));
    await page.locator('#study-prepare').click();
    await page.locator('#study-start').waitFor();
    await page.evaluate(() => { window.simulateLostStart = true; });
    let failPlanRead = true;
    await page.route('**/api/study/T02', async route => {
      if (failPlanRead) { failPlanRead = false; await route.abort('failed'); }
      else await route.continue();
    });
    await fetch(`${base}/api/active-session`, { headers: { 'X-A11y-Client': 'nvda-addon' } });
    await page.locator('#study-start').click();
    await page.locator('#study-finish-form').waitFor();
    await page.unroute('**/api/study/T02');
    await page.route('**/api/sessions/*/stop', async route => {
      await route.fetch();
      await route.abort('failed');
    });
    await page.locator('#study-answer').fill('Technical recovery rehearsal');
    await page.locator('#study-outcome').selectOption('technical');
    await page.locator('#study-finish').click();
    await page.locator('#study-retry').waitFor({ timeout: 12000 });
    await page.unroute('**/api/sessions/*/stop');
    await page.locator('#study-retry').click();
    await page.locator('#study-start').waitFor();
    assert.equal((await (await fetch(base + '/api/study/T02')).json()).current_order, 1);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(output, 'study-mobile.png'), fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.deepEqual(errors, []);
    const result = { status: 'PASS', evidence: 'Synthetic extension and NVDA heartbeat; real local backend and Chromium UI',
      actual_nvda: 'NOT_RUN', checks: ['default assigned view', 'T01/T02 distinct assignments', 'NVDA gate', 'blocked result and answer', 'restart progress', 'next task', 'lost start/stop response reconciliation', 'technical retry preserves order', 'no GT in participant view', 'desktop/mobile layout'] };
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result));
  } finally {
    if (browser) await browser.close();
    server.kill();
    if (server.exitCode === null) await new Promise(resolve => server.once('exit', resolve));
    fs.rmSync(temp, { recursive: true, force: true });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
