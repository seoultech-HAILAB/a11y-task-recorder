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
  const state = { session: null, seq: null, timer: null, startedAt: 0, busy: false, pendingEnd: null, polling: false };
  const pending = new Map();
  let lastAlert = "";

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
    if (text === lastAlert) return;
    lastAlert = text;
    const node = el("study-free-alert");
    node.replaceChildren();
    if (text) setTimeout(() => { const line = document.createElement("span"); line.textContent = text; node.replaceChildren(line); }, 250);
  }
  async function run(action) {
    if (state.busy) return;
    state.busy = true;
    let target = null;
    try { target = await action(); } catch (error) {
      alert(error.message);
      if (action === start) await resume();
    }
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
    // The created session was a draft; watch() acts only on an active one (Windows 11/02: the end
    // gesture was ignored 2 of 2 until a reload read the session again).
    state.session = (await api(`/sessions/${session.id}`)).session;
    state.startedAt = Date.parse(state.session.started_at) || Date.now();
    state.pendingEnd = null;
    watch();
    return null; // the new window has focus
  }
  // NVDA+Ctrl+E (the add-on signal) or the time limit ends the free task.
  function watch() {
    clearInterval(state.timer);
    state.timer = setInterval(async () => {
      if (state.busy || state.polling || state.session?.status !== "active") return;
      if (state.pendingEnd) { run(() => end(state.pendingEnd)); return; }
      if (Date.now() - state.startedAt > TIME_LIMIT_MS) { run(() => end("time_limit")); return; }
      const sessionId = state.session.id;
      state.polling = true;
      try {
        const data = await api(`/signals?after=${state.seq}`);
        if (state.session?.id !== sessionId || state.session.status !== "active") return;
        state.seq = data.seq;
        const signal = data.signals.find(item => item.kind === "end" && item.session_id === sessionId);
        if (signal) run(() => end(signal.source === "nvda_addon" ? "participant_gesture" : "facilitator_gesture"));
      } catch { /* Keep polling after a transient collector error. */ }
      finally { state.polling = false; }
    }, 700);
  }
  async function end(reason) {
    state.pendingEnd = reason;
    const sessionId = state.session.id;
    try {
      state.session = (await api(`/sessions/${sessionId}/end-task`, "POST", { reason })).session;
    } catch (error) {
      // The collector may have committed before its response was lost. Reconcile
      // the sealed state instead of retrying /end-task forever against answering.
      let current;
      try { current = (await api(`/sessions/${sessionId}`)).session; } catch { throw error; }
      if (current?.status === "answering") state.session = current;
      else if (["completed", "abandoned", "aborted"].includes(current?.status)) {
        clearInterval(state.timer);
        state.pendingEnd = null;
        state.session = null;
        try { await bridge("close", sessionId); } catch { /* Another dashboard already closed it. */ }
        show(false);
        alert("이 과업은 이미 종료되었습니다. 담당자에게 확인해 주세요.");
        return el("study-free-heading");
      } else throw error;
    }
    clearInterval(state.timer);
    state.pendingEnd = null;
    alert("");
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
    try { active = (await api("/free-task/current")).session; } catch { return; }
    clearInterval(state.timer);
    if (active?.study?.kind !== "free") { state.session = null; state.pendingEnd = null; return; }
    state.session = active;
    if (active.status === "answering") {
      state.pendingEnd = null;
      try { await bridge("leave", active.id); } catch { /* Already-closed site windows are harmless. */ }
      show(true);
      el("study-free-result-heading").focus();
      return;
    }
    state.seq = (await api(`/signals?after=${Number.MAX_SAFE_INTEGER}`)).seq;
    state.startedAt = Date.parse(active.started_at) || Date.now();
    watch();
  }

  document.addEventListener("DOMContentLoaded", () => {
    if (!el("study-free")) return;
    el("study-free-form").addEventListener("submit", event => { event.preventDefault(); run(start); });
    el("study-free-result").addEventListener("submit", event => { event.preventDefault(); run(save); });
  });
  // Study initialization first restores the selected tester and completion screen.
  window.addEventListener("a11y-study-ready", () => run(resume));
})();
