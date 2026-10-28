// 0.5.0 participant flow rehearsal (engineering only): synthetic extension replies and NVDA heartbeat,
// real backend and Chromium. node scripts/verify_participant_flow.cjs [repo] [screenshot dir]
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const repo = process.argv[2] || process.cwd();
const out = process.argv[3] || path.resolve(repo, 'dist/qa/participant');
fs.mkdirSync(out, { recursive: true });
const base = 'http://127.0.0.1:28791';
const step = (name) => console.log('ok -', name);

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'rec050-'));
  const server = spawn('/usr/bin/python3', ['run.py', '--no-browser', '--port', '28791', '--db', path.join(temp, 'r.sqlite3')], { cwd: repo });
  let browser;
  const signal = (kind) => fetch(base + '/api/signals', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-A11y-Client': 'nvda-addon' }, body: JSON.stringify({ kind, source: 'nvda_addon' }) }).then(r => r.json());
  try {
    for (let i = 0; i < 80; i++) { try { if ((await fetch(base + '/api/health')).ok) break; } catch {} await new Promise(r => setTimeout(r, 100)); }
    // Keep the NVDA heartbeat alive.
    const beat = setInterval(() => fetch(base + '/api/active-session', { headers: { 'X-A11y-Client': 'nvda-addon' } }).catch(() => {}), 500);
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.addInitScript(() => {
      window.addEventListener('message', async event => {
        const m = event.data;
        if (m?.kind !== 'a11y-study-command') return;
        let result = { ok: true, ready: true, sessionId: m.sessionId, tabId: 1 };
        if (m.action === 'start') {
          const response = await fetch(`/api/sessions/${m.sessionId}/start`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ study_ready: true }) });
          const data = await response.json();
          result = response.ok ? { ...result, started: true } : { ok: false, error: data.error };
        }
        window.postMessage({ kind: 'a11y-study-response', requestId: m.requestId, result }, location.origin);
      });
    });
    const active = () => page.evaluate(() => document.activeElement?.id || document.activeElement?.tagName);
    const waitFocus = (id) => page.waitForFunction(i => document.activeElement?.id === i, id, { timeout: 8000 });

    await page.goto(base);
    await waitFocus('study-tester');
    assert.equal(await page.locator('#study-tester').evaluate(n => n.tagName), 'INPUT');
    assert.equal(await page.locator('.app-header').evaluate(n => n.inert), true);
    step('number field focused, header inert');
    await page.screenshot({ path: path.join(out, '1-login.png') });

    await page.fill('#study-tester', 'x9');
    await page.click('#study-select');
    await page.waitForFunction(() => document.querySelector('#study-alert').textContent.includes('등록되지 않은'));
    step('unknown number refused with an alert');
    await page.fill('#study-tester', '1');
    await page.keyboard.press('Enter');
    await waitFocus('study-instruction');
    assert.equal(await page.textContent('#study-order'), '연습');
    const guide = await page.textContent('#study-instruction');
    assert.ok(guide.includes('NVDA, Ctrl, H') && guide.includes('NVDA, Ctrl, E'));
    step('"1" names T01; practice opens by itself with the guide focused');
    await page.screenshot({ path: path.join(out, '2-practice.png') });

    const repeat = await signal('repeat');
    assert.equal(repeat.speak, "연습 화면에서 '재료 보기' 버튼을 눌러 첫 번째 재료를 확인하세요.");
    await page.click('#study-start');
    await page.waitForFunction(() => document.querySelector('#study-view').dataset.phase === 'active');
    await page.waitForFunction(() => document.activeElement?.id === 'study-frame');
    const frame = page.frameLocator('#study-frame');
    // Tab many times: focus never leaves the frame.
    const inFrame = () => page.evaluate(() => document.activeElement?.id === 'study-frame');
    for (let i = 0; i < 9; i++) { await page.keyboard.press('Tab'); await page.waitForTimeout(60); assert.ok(await inFrame(), `Tab ${i}: ${await active()}`); }
    for (let i = 0; i < 9; i++) { await page.keyboard.press('Shift+Tab'); await page.waitForTimeout(60); assert.ok(await inFrame(), `Shift+Tab ${i}: ${await active()}`); }
    step('Tab / Shift+Tab stay inside the site');
    await frame.locator('#show').click();
    await page.screenshot({ path: path.join(out, '3-practice-active.png') });
    await signal('end');
    await waitFocus('study-result-heading');
    await page.click('#study-return');
    await page.waitForFunction(() => document.querySelector('#study-view').dataset.phase === 'active');
    assert.match(await frame.locator('#ingredients').textContent(), /두부/);
    step('과업으로 돌아가기 returns to the same page state');
    await signal('end');
    await waitFocus('study-result-heading');
    await page.check('input[value="completed"]');
    await page.fill('#study-answer', '두부');
    await page.click('#study-submit');
    await waitFocus('study-saved');
    const feedback = await page.textContent('#study-saved');
    assert.ok(feedback.includes('단축키를 모두 써 보셨습니다'), feedback);
    step('practice result tells whether H and E were used');
    await page.screenshot({ path: path.join(out, '4-practice-end.png') });

    await page.click('#study-practice-stop');
    await waitFocus('study-instruction');
    const plan = await (await fetch(base + '/api/study/T01')).json();
    assert.equal(await page.textContent('#study-instruction'), plan.tasks[0].instruction.trim());
    assert.equal(await page.textContent('#study-order'), `과업 1 / ${plan.total}`);
    const visible = await page.locator('#study-view').innerText();
    assert.doesNotMatch(visible, /NVDA|제한 시간|Alt/, visible);
    step('task 1: instruction is the scenario alone, no shortcut or time text');
    await page.screenshot({ path: path.join(out, '5-task-ready.png') });

    const taskRepeat = await signal('repeat');
    assert.equal(taskRepeat.speak, plan.tasks[0].instruction.trim());
    await page.click('#study-start');
    await page.waitForFunction(() => document.querySelector('#study-view').dataset.phase === 'active');
    await signal('end');
    await waitFocus('study-result-heading');
    assert.equal(await page.textContent('#study-result-heading'), '과업 결과');
    await page.screenshot({ path: path.join(out, '6-result.png') });
    await page.check('input[value="blocked"]');
    await page.click('#study-submit');
    await waitFocus('study-instruction');
    assert.equal(await page.textContent('#study-order'), `과업 2 / ${plan.total}`);
    step('저장 goes straight to the task 2 instruction');
    const record = await (await fetch(base + `/api/sessions/${plan.tasks[0].session_id || (await (await fetch(base + '/api/study/T01')).json()).tasks[0].session_id}/export.json`)).json();
    assert.equal(record.study_outcome, 'blocked');

    assert.equal(await page.locator('#study-practice').isVisible(), true);
    await page.click('#study-practice');
    await waitFocus('study-instruction');
    assert.equal(await page.textContent('#study-order'), '연습');
    await page.click('#study-practice-skip');
    await waitFocus('study-instruction');
    assert.equal(await page.textContent('#study-order'), `과업 2 / ${plan.total}`);
    step('practice can be reopened before task 2 and left again');

    await page.reload();
    await waitFocus('study-instruction');
    assert.equal(await page.textContent('#study-order'), `과업 2 / ${plan.total}`);
    step('reload returns to task 2 without reopening the practice');

    clearInterval(beat);
    assert.deepEqual(errors, []);
    console.log('PASS');
  } finally {
    if (browser) await browser.close();
    server.kill();
    fs.rmSync(temp, { recursive: true, force: true });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
