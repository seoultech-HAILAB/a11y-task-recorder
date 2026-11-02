/* Free task (0.6.0): after every assigned task, the tester may pick a site and a goal of their own.
 * The site opens in a new browser window (most real sites refuse the dashboard frame); the
 * extension records that window only. NVDA+Ctrl+E ends it as in an assigned task, the window
 * closes, and the result form here takes focus. The record is marked study.kind = "free" and is
 * kept apart in analysis. Shown only inside the completion screen (#study-complete).
 */
(() => {
  const TIME_LIMIT_MS = 10 * 60 * 1000;
  const el = id => document.getElementById(id);
  const api = (path, method = "GET", body) => request(path, { method, ...(body ? { body } : {}) });
  const state = { session: null, seq: null, timer: null, startedAt: 0, busy: false };
  const pending = new Map();

  function bridge(action, sessionId) {
    return new Promise((resolve, reject) => {
      const requestId = crypto.randomUUID();
      const timeout = setTimeout(() => {
        pending.delete(requestId);
        reject(new Error("기록 프로그램에 연결할 수 없습니다. 연구진에게 알려 주세요."));
      }, 15000);
      pending.set(requestId, { resolve, reject, timeout });
      window.postMessage({ kind: "a11y-study-command", requestId, action, sessionId }, location.origin);
    });
  }
  window.addEventListener("message", event => {
    if (event.source !== window || event.origin !== location.origin || event.data?.kind !== "a11y-study-response") return;
    const entry = pending.get(event.data.requestId);
    if (!entry) return;
    clearTimeout(entry.timeout);
    pending.delete(event.data.requestId);
    const result = event.data.result;
    if (!result?.ok) entry.reject(new Error("사이트를 열 수 없습니다. 주소를 확인하거나 연구진에게 알려 주세요."));
    else entry.resolve(result);
  });

  function alert(text) {
    const node = el("study-free-alert");
    node.replaceChildren();
    if (text) setTimeout(() => { const line = document.createElement("span"); line.textContent = text; node.replaceChildren(line); }, 250);
  }
  async function run(action) {
    if (state.busy) return;
    state.busy = true;
    let target = null;
    try { target = await action(); } catch (error) { alert(error.message); }
    finally { state.busy = false; if (target instanceof HTMLElement) target.focus(); }
  }
  function show(answering) {
    el("study-free-form").hidden = answering;
    el("study-free-result").hidden = !answering;
  }

  async function start() {
    const tester = window.Study?.tester();
    if (!tester) throw new Error("테스터 번호를 먼저 정해 주세요.");
    alert("");
    const { session } = await api(`/study/${encodeURIComponent(tester)}/free`, "POST",
      { url: el("study-free-url").value, goal: el("study-free-goal").value });
    state.session = session;
    await bridge("prepare", session.id);
    state.seq = (await api(`/signals?after=${Number.MAX_SAFE_INTEGER}`)).seq;
    await bridge("start", session.id);
    state.startedAt = Date.now();
    watch();
    return null; // the new window has focus
  }
  // NVDA+Ctrl+E (the add-on signal) or the time limit ends the free task.
  function watch() {
    clearInterval(state.timer);
    state.timer = setInterval(async () => {
      if (state.busy || state.session?.status !== "active") return;
      if (Date.now() - state.startedAt > TIME_LIMIT_MS) { run(() => end("time_limit")); return; }
      let data;
      try { data = await api(`/signals?after=${state.seq}`); } catch { return; }
      state.seq = data.seq;
      const signal = data.signals.find(item => item.kind === "end" && item.session_id === state.session.id);
      if (signal) run(() => end(signal.source === "nvda_addon" ? "participant_gesture" : "facilitator_gesture"));
    }, 700);
  }
  async function end(reason) {
    clearInterval(state.timer);
    state.session = (await api(`/sessions/${state.session.id}/end-task`, "POST", { reason })).session;
    try { await bridge("leave", state.session.id); } catch { /* the window may already be closed */ }
    show(true);
    return el("study-free-result-heading");
  }
  async function save() {
    const outcome = document.querySelector('input[name="study-free-done"]:checked')?.value;
    if (!outcome) throw new Error("했는지 못 했는지 골라 주세요.");
    const data = await api(`/sessions/${state.session.id}/stop`, "POST", {
      status: outcome === "completed" ? "completed" : "abandoned", study_outcome: outcome,
      answer: el("study-free-note").value, entered_by: "participant",
    });
    try { await bridge("close", data.session.id); } catch { /* saved either way */ }
    state.session = null;
    for (const id of ["study-free-url", "study-free-goal", "study-free-note"]) el(id).value = "";
    for (const radio of document.querySelectorAll('input[name="study-free-done"]')) radio.checked = false;
    show(false);
    alert("저장했습니다. 자유 과업을 하나 더 하거나 담당자에게 알려 주세요.");
    return el("study-free-heading");
  }
  // After a reload: a free task left active or waiting for its result is picked up again.
  async function resume() {
    let active;
    try { active = (await api("/active-session")).session; } catch { return; }
    if (active?.study?.kind !== "free") return;
    state.session = active;
    if (active.status === "answering") { show(true); return; }
    state.seq = (await api(`/signals?after=${Number.MAX_SAFE_INTEGER}`)).seq;
    state.startedAt = Date.parse(active.started_at) || Date.now();
    watch();
  }

  document.addEventListener("DOMContentLoaded", () => {
    if (!el("study-free")) return;
    el("study-free-form").addEventListener("submit", event => { event.preventDefault(); run(start); });
    el("study-free-result").addEventListener("submit", event => { event.preventDefault(); run(save); });
    resume();
  });
})();
