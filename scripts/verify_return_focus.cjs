// 0.5.11 과업으로 돌아가기 focus check (engineering only): the site is served from another origin, as in
// the study, and a stand-in for content.js answers a11y-study-focus-site. Fails if focus rests on the
// recorder page body while the return is in flight (NVDA read the whole site as one line there).
// node scripts/verify_return_focus.cjs [repo]
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const repo = process.argv[2] || process.cwd();
const base = 'http://127.0.0.1:28794';
const sitePort = 28795;
const RETURNS = 3;

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'rec0511-'));
  fs.mkdirSync(path.join(temp, 'site'));
  fs.writeFileSync(path.join(temp, 'site', 'site.html'), '<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>사이트</title></head><body><a href="#a">메뉴 바로가기</a><h1>사이트</h1><a href="#b" id="target">재단소개</a><p>본문</p></body></html>');
  const catalog = JSON.parse(fs.readFileSync(path.join(repo, 'recorder/study_catalog.json'), 'utf8'));
  for (const tester of catalog.testers) for (const task of tester.tasks) task.start_url = `http://localhost:${sitePort}/site.html`;
  fs.writeFileSync(path.join(temp, 'catalog.json'), JSON.stringify(catalog));
  const site = spawn('/usr/bin/python3', ['-m', 'http.server', String(sitePort), '--bind', '127.0.0.1'], { cwd: path.join(temp, 'site'), stdio: 'ignore' });
  const server = spawn('/usr/bin/python3', ['run.py', '--no-browser', '--port', '28794', '--db', path.join(temp, 'r.sqlite3'), '--catalog', path.join(temp, 'catalog.json')], { cwd: repo });
  let browser, beat;
  try {
    for (let i = 0; i < 80; i++) { try { if ((await fetch(base + '/api/health')).ok) break; } catch {} await new Promise(r => setTimeout(r, 100)); }
    beat = setInterval(() => fetch(base + '/api/active-session', { headers: { 'X-A11y-Client': 'nvda-addon' } }).catch(() => {}), 500);
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    await page.addInitScript((port) => {
      if (location.port === String(port)) {
        // The site frame: keep the last focused control and restore it on resume, as content.js does.
        let last = null;
        addEventListener('focusin', e => { last = e.target; }, true);
        addEventListener('message', e => {
          if (e.data?.kind !== 'a11y-study-focus-site') return;
          const kept = e.data.edge === 'resume' && last?.isConnected ? last : null;
          setTimeout(() => { (kept || document.querySelector('a')).focus(); parent.postMessage({ kind: 'a11y-study-site-entry', key: 'k' }, '*'); }, 30);
        });
        return;
      }
      addEventListener('message', async event => {
        const m = event.data;
        if (m?.kind !== 'a11y-study-command') return;
        let result = { ok: true, ready: true, sessionId: m.sessionId, tabId: 1 };
        if (m.action === 'start') {
          const r = await fetch(`/api/sessions/${m.sessionId}/start`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ study_ready: true }) });
          result = r.ok ? { ...result, started: true } : { ok: false, error: (await r.json()).error };
        }
        postMessage({ kind: 'a11y-study-response', requestId: m.requestId, result }, location.origin);
      });
    }, sitePort);
    await page.goto(base);
    await page.evaluate(() => localStorage.setItem('study-practiced-T01', '1'));
    await page.goto(base);
    await page.waitForFunction(() => document.activeElement?.id === 'study-tester', null, { timeout: 15000 });
    await page.fill('#study-tester', '1');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.activeElement?.id === 'study-instruction', null, { timeout: 15000 });
    await page.click('#study-start');
    for (let i = 1; i <= RETURNS; i++) {
      await page.waitForFunction(() => document.querySelector('#study-view').dataset.phase === 'active', null, { timeout: 20000 });
      const frame = page.frames().find(f => f.url().includes(`:${sitePort}/`));
      await frame.locator('#target').focus();
      await fetch(base + '/api/signals', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-A11y-Client': 'nvda-addon' }, body: JSON.stringify({ kind: 'end', source: 'nvda_addon' }) });
      await page.waitForFunction(() => document.activeElement?.id === 'study-result-heading', null, { timeout: 10000 });
      await page.focus('#study-return');
      await page.evaluate(() => { window.__drops = 0; window.__watch = setInterval(() => { if (document.activeElement === document.body) window.__drops++; }, 1); });
      await page.keyboard.press('Enter');
      await page.waitForFunction(() => document.querySelector('#study-view').dataset.phase === 'active');
      await page.waitForTimeout(700);
      const drops = await page.evaluate(() => { clearInterval(window.__watch); return window.__drops; });
      assert.equal(drops, 0, `return ${i}: focus rested on the recorder page ${drops} times`);
      assert.equal(await frame.evaluate(() => document.activeElement?.id), 'target', `return ${i}: the kept control has focus`);
      console.log(`ok - return ${i}: focus went from 과업으로 돌아가기 to the kept control without resting on the page`);
    }
  } finally {
    clearInterval(beat);
    if (browser) await browser.close();
    server.kill();
    site.kill();
    fs.rmSync(temp, { recursive: true, force: true });
  }
})().catch(e => { console.error('not ok -', e.message); process.exitCode = 1; });
