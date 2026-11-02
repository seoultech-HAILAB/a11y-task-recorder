const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const script = fs.readFileSync('static/study.js', 'utf8');
function extract(name, next, scope) {
  const start = script.indexOf(`  async function ${name}(`);
  const end = script.indexOf(next, start);
  assert.ok(start >= 0 && end > start);
  return vm.runInNewContext(`${script.slice(start, end)};${name}`, scope);
}
test('tester change cannot abandon a different tester free answering session', async () => {
  const field = {};
  const fn = extract('assertNoOtherRecording', '  async function selectTester', {
    api: async path => ({ session: path === '/active-session' ? null : { status: 'answering', study: { kind: 'free', tester: 'T02' } } }),
    view: { tester: 'T02' }, el: () => field,
  });
  await assert.rejects(fn('T01'), /다른 과업/);
  assert.equal(field.value, 'T02');
  await fn('T02');
});
test('free task resume notification waits for assigned dashboard initialization', async () => {
  const order = [];
  const fn = extract('home', '  // Typed number', {
    pausePolling() {}, stopPolling() {}, showView() {},
    view: { signalSeq: 0, testerIds: [], changeTester: false },
    api: async () => ({ testers: [{ id: 'T01' }] }),
    selectTester: async () => { await Promise.resolve(); order.push('selected'); },
    busy: async action => { await action(); }, afterTester: async () => { order.push('prepared'); },
    message() {}, resumePolling() { order.push('polling'); },
    Event: class { constructor(type) { this.type = type; } },
    window: { dispatchEvent(event) { order.push(event.type); } },
  });
  await fn('T01');
  assert.deepEqual(order, ['selected', 'prepared', 'polling', 'a11y-study-ready']);
});
