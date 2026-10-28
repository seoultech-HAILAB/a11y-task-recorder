// 0.4.4 participant controls in static/index.html and their wiring in static/study.js.
// Behaviour runs end to end in scripts/verify_single_window.cjs; these are the static rules.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');

const html = fs.readFileSync('static/index.html', 'utf8');
const script = fs.readFileSync('static/study.js', 'utf8');
const tagOf = id => {
  const match = html.match(new RegExp(`<[a-z]+[^>]*\\bid="${id}"[^>]*>`));
  assert.ok(match, `#${id} exists`);
  return match[0];
};
const attr = (tag, name) => tag.match(new RegExp(`\\b${name}="([^"]*)"`))?.[1];
const block = id => {
  const start = html.indexOf(tagOf(id));
  return html.slice(start, html.indexOf('</div>', start));
};

test('every element id study.js looks up exists in index.html', () => {
  const ids = new Set([...script.matchAll(/\bel\("([a-z0-9-]+)"\)/g)].map(m => m[1]));
  for (const call of script.matchAll(/\b(?:openConfirm|closeConfirm|escape)\("[a-z-]+",\s*"([a-z0-9-]+)"\)|\bcloseConfirm\("([a-z0-9-]+)"\)/g)) ids.add(call[1] || call[2]);
  for (const call of script.matchAll(/\bon\("([a-z0-9-]+)"/g)) ids.add(call[1]);
  assert.ok(ids.size > 40, `found ${ids.size} ids`);
  for (const id of ids) assert.match(html, new RegExp(`\\bid="${id}"`), `#${id} referenced by study.js`);
});

test('confirm steps are in-page groups named by their question, hidden until opened', () => {
  for (const [group, question, yes, no] of [
    ['study-end-confirm', 'study-end-question', '끝내기', '계속하기'],
    ['study-restart-confirm', 'study-restart-question', '다시 시작', '계속하기'],
    ['study-redo-confirm', 'study-redo-question', '다시 시작', '취소'],
    ['study-tester-confirm', 'study-tester-question', '번호 바꾸기', '취소']]) {
    const tag = tagOf(group);
    assert.equal(attr(tag, 'role'), 'group', group);
    assert.equal(attr(tag, 'aria-labelledby'), question, group);
    assert.match(tag, /\bhidden\b/, `${group} starts hidden`);
    tagOf(question);
    const buttons = [...block(group).matchAll(/<button[^>]*>([^<]*)<\/button>/g)].map(m => m[1]);
    assert.deepEqual(buttons, [yes, no], group);
  }
  assert.doesNotMatch(script, /\b(?:window\.)?(?:confirm|alert|prompt)\(/, 'no browser dialogs (they interrupt NVDA)');
});

test('confirm steps open on the safe choice and Escape closes them', () => {
  for (const [name, focus] of [['end', 'study-end-no'], ['restart', 'study-restart-no'], ['redo', 'study-redo-no'], ['tester', 'study-tester-no']]) {
    assert.match(script, new RegExp(`openConfirm\\("${name}", "${focus}"\\)`), name);
  }
  for (const group of ['study-end-confirm', 'study-restart-confirm', 'study-redo-confirm', 'study-tester-confirm']) {
    assert.match(script, new RegExp(`escape\\("${group}"`), group);
  }
});

test('controls below the site stay out of the Tab order; undo and tester change are tabbable', () => {
  for (const id of ['study-end', 'study-site-back', 'study-site-start', 'study-restart']) assert.equal(attr(tagOf(id), 'tabindex'), '-1', id);
  for (const id of ['study-return', 'study-redo', 'study-tester-change', 'study-tester-keep']) {
    const tag = tagOf(id);
    assert.equal(attr(tag, 'tabindex'), undefined, id);
    assert.equal(attr(tag, 'type'), 'button', `${id} never submits a form`);
  }
  const undo = tagOf('study-undo');
  assert.equal(attr(undo, 'role'), 'group');
  assert.equal(attr(undo, 'aria-labelledby'), 'study-undo-label');
  assert.match(tagOf('study-undo-label'), /<p\b/);
  assert.ok(html.indexOf('id="study-submit"') < html.indexOf('id="study-undo"'), '저장 comes first in the result form');
  assert.ok(html.indexOf('id="study-shortcuts"') < html.indexOf('id="study-tester-tools"'), '테스터 번호 바꾸기 follows 과업 시작 and 연습 시작');
});

test('restart and tester change call the abort API with their reasons; return uses resume-task', () => {
  assert.match(script, /\/abort`, "POST", \{ reason: "restart", trigger/);
  assert.match(script, /\/abort`, "POST", \{ reason: "tester_change", trigger: "tester_picker"/);
  assert.match(script, /\/resume-task`, "POST", \{ by: "participant" \}/);
  // A time-limit end is saved by itself and cannot be undone from the result form.
  assert.match(script, /const undo = current === "answering" && \(practice \|\| view\.session\?\.end_reason !== "time_limit"\)/);
});
