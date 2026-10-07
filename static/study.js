/* Assigned-task view. Progress lives in SQLite; localStorage only remembers the slot. */
window.Study = (() => {
  const view = { tester: null, plan: null, session: null, ready: false, busy: false,
    timer: null, opened: false, pending: new Map(), lastPoll: 0, checking: false };
  const labels = { completed: "과업 종료", blocked: "수행 불가", timeout: "시간 종료", technical: "기술 문제" };
  const el = id => document.getElementById(id);
  const message = (text, error = false) => {
    el("study-message").textContent = text;
    el("study-message").classList.toggle("error", error);
  };
  const api = (path, method = "GET", body) => request(path, { method, ...(body ? { body } : {}) });
  function remember(tester) { try { localStorage.setItem("study-tester", tester); } catch {} }
  function remembered() { try { return localStorage.getItem("study-tester"); } catch { return null; } }
  function bridge(action) {
    return new Promise((resolve, reject) => {
      const requestId = crypto.randomUUID();
      const timeout = setTimeout(() => {
        view.pending.delete(requestId);
        reject(new Error("평가용 브라우저 확장에 연결할 수 없습니다. 새 키트의 평가시작.bat로 연 브라우저인지 확인해 주세요."));
      }, 15000);
      view.pending.set(requestId, { resolve, reject, timeout });
      window.postMessage({ kind: "a11y-study-command", requestId, action, sessionId: view.session.id }, location.origin);
    });
  }
  window.addEventListener("message", event => {
    if (event.source !== window || event.origin !== location.origin || event.data?.kind !== "a11y-study-response") return;
    const pending = view.pending.get(event.data.requestId);
    if (!pending) return;
    clearTimeout(pending.timeout);
    view.pending.delete(event.data.requestId);
    const result = event.data.result;
    if (!result?.ok) pending.reject(new Error(result?.error || "사이트 준비를 확인할 수 없습니다."));
    else pending.resolve(result);
  });

  async function busy(action) {
    if (view.busy) return;
    view.busy = true;
    renderControls();
    try { await action(); } catch (error) {
      // A response may be lost after the server commits start/stop. Read state before retrying.
      try { await reconcileSession(); } catch { /* Keep the original operation error. */ }
      message(error.message, true);
    }
    finally { view.busy = false; renderControls(); }
  }
  function pausePolling() { clearInterval(view.timer); view.timer = null; }
  function resumePolling() {
    pausePolling();
    view.timer = setInterval(async () => {
      if (view.busy || view.checking || el("study-view").hidden) return;
      updateTimer();
      if (Date.now() - view.lastPoll < 4000) return;
      view.lastPoll = Date.now();
      await checkConnection();
      try { await reconcileSession(); } catch (error) { message(error.message, true); }
      if (view.session?.status === "draft" && view.opened) {
        view.checking = true;
        try { await checkSite(); } catch (error) { view.ready = false; message(error.message, true); renderControls(); }
        finally { view.checking = false; }
      }
    }, 1000);
  }
  async function home(tester) {
    pausePolling();
    stopPolling();
    showView("study");
    try {
      const index = await api("/study");
      el("study-version").textContent = `${index.testers.length}개 테스터 배정`;
      el("study-tester").replaceChildren(new Option("번호 선택", ""), ...index.testers.map(t => new Option(`${t.id} (${t.finished}/${t.total})`, t.id)));
      const code = tester || view.tester || new URLSearchParams(location.search).get("tester") || remembered();
      if (index.testers.some(t => t.id === code)) {
        el("study-tester").value = code;
        await selectTester(code);
      } else message("테스터 번호를 선택해 주세요.");
    } catch (error) { message(error.message, true); }
    resumePolling();
  }
  async function selectTester(tester) {
    if (!tester) return;
    const active = (await api("/active-session")).session;
    if (active && active.study?.tester !== tester) {
      el("study-tester").value = view.tester || "";
      throw new Error("다른 과업이 기록 중입니다. 현재 기록을 먼저 종료해 주세요.");
    }
    const plan = await api(`/study/${encodeURIComponent(tester)}`);
    view.tester = tester;
    view.plan = plan;
    view.session = null;
    view.ready = false;
    view.opened = false;
    remember(tester);
    history.replaceState({}, "", `/?tester=${tester}`);
    el("study-answer").value = "";
    el("study-notes").value = "";
    el("study-outcome").value = "";
    // Restore a stopped record at the boundary so reloading never silently starts a task.
    const current = plan.tasks.find(t => t.order === plan.current_order);
    const previous = plan.tasks.find(t => t.order === (plan.current_order || plan.total + 1) - 1);
    const row = current?.session_id ? current : previous?.session_id ? previous : null;
    if (row) view.session = (await api(`/sessions/${row.session_id}`)).session;
    render();
    message("");
  }
  async function refreshPlan() { view.plan = await api(`/study/${view.tester}`); }
  async function reconcileSession() {
    const id = view.session?.id;
    if (!id) return;
    const session = (await api(`/sessions/${id}`)).session;
    if (view.session?.id !== id) return;
    const plan = await api(`/study/${view.tester}`);
    if (view.session?.id !== id) return;
    view.session = session;
    view.plan = plan;
    render();
  }
  function currentTask() {
    return view.plan?.tasks.find(t => t.order === (view.session?.study.order ?? view.plan.current_order));
  }
  function renderControls() {
    const session = view.session;
    const draft = !session || session.status === "draft";
    const active = session?.status === "active";
    const ended = session && ["completed", "abandoned"].includes(session.status);
    const technical = ended && session.study_outcome === "technical";
    const complete = view.plan && view.plan.finished === view.plan.total;
    el("study-prepare").hidden = !draft || view.ready || complete;
    el("study-check").hidden = !draft || !view.opened || view.ready;
    el("study-start").hidden = !draft || !view.ready;
    el("study-next").hidden = !ended || technical || complete;
    el("study-retry").hidden = !technical;
    el("study-finish-form").hidden = !active;
    el("study-review").hidden = !ended;
    el("study-saved").hidden = !ended;
    el("study-tester").disabled = active || view.busy;
    el("study-select").disabled = active || view.busy;
    el("study-view").querySelectorAll(".study-actions button, #study-finish, #study-review").forEach(b => { b.disabled = view.busy; });
  }
  function render() {
    const plan = view.plan;
    if (!plan) return;
    const complete = plan.finished === plan.total;
    el("study-content").hidden = false;
    el("study-complete").hidden = !complete;
    const task = currentTask() || plan.tasks[plan.tasks.length - 1];
    el("study-order").textContent = `${view.tester} · 과업 ${task.order} / ${plan.total} · 블록 ${task.block}`;
    el("study-site").textContent = task.site_name;
    el("study-instruction").textContent = task.instruction;
    el("study-progress-text").textContent = `${plan.finished} / ${plan.total} 종료`;
    el("study-progress-bar").max = plan.total;
    el("study-progress-bar").value = plan.finished;
    const option = [...el("study-tester").options].find(o => o.value === view.tester);
    if (option) option.textContent = `${view.tester} (${plan.finished}/${plan.total})`;
    el("study-task-list").replaceChildren(...plan.tasks.map(t => {
      const li = document.createElement("li");
      const status = t.outcome === "technical" ? "재준비 필요" : t.status === "finished" ? "종료" : t.status === "active" ? "기록 중" : t.order === task.order ? "현재 과업" : "대기";
      li.textContent = `${t.order}. ${t.site_name} · ${status}`;
      if (t.order === task.order) li.setAttribute("aria-current", "step");
      return li;
    }));
    const ended = ["completed", "abandoned"].includes(view.session?.status);
    el("study-break").hidden = !(ended && task.order % 4 === 0 && task.order < plan.total && view.session.study_outcome !== "technical");
    el("study-saved").textContent = ended ? `${labels[view.session.study_outcome] || "종료"} 기록을 저장했습니다.` : "";
    updateTimer();
    renderControls();
  }
  function updateTimer() {
    const task = currentTask();
    if (!task) return;
    const active = view.session?.status === "active";
    const elapsed = active ? Math.max(0, Math.floor((Date.now() - Date.parse(view.session.started_at)) / 1000)) : 0;
    el("study-timer").textContent = active
      ? `경과 ${Math.floor(elapsed / 60)}분 ${elapsed % 60}초 · 제한 ${task.time_limit_seconds / 60}분${elapsed >= task.time_limit_seconds ? " · 제한 시간에 도달했습니다." : ""}`
      : `제한 시간 ${task.time_limit_seconds / 60}분`;
  }
  async function prepare(retry = false) {
    const endpoint = retry ? `/study/${view.tester}/retry` : `/study/${view.tester}/prepare`;
    const data = await api(endpoint, "POST", retry ? { session_id: view.session.id } : {});
    view.session = data.session;
    view.ready = false;
    el("study-answer").value = "";
    el("study-notes").value = "";
    el("study-outcome").value = "";
    await refreshPlan();
    render();
    if (view.session.status !== "draft") return;
    message("배정된 사이트를 준비하고 있습니다.");
    const result = await bridge("prepare");
    view.opened = true;
    applyPreparation(result);
  }
  function applyPreparation(result) {
    view.ready = Boolean(result.ready);
    message(result.ready ? "사이트가 준비되었습니다. 과업을 확인한 뒤 기록을 시작해 주세요."
      : result.reason === "login_required" ? "열린 사이트에서 접속 코드를 입력해 주세요. 접속 확인 후 기록을 시작합니다."
      : "사이트 준비를 확인하고 있습니다. 열린 탭을 닫지 마세요.");
    renderControls();
  }
  async function checkSite() {
    const sessionId = view.session.id;
    const result = await bridge("check");
    if (view.session?.id === sessionId && view.session.status === "draft") applyPreparation(result);
  }
  async function start() {
    await checkConnection();
    if (!state.nvdaConnected) throw new Error("NVDA가 연결되지 않아 기록을 시작하지 않았습니다. NVDA 연결 상태를 확인해 주세요.");
    const result = await bridge("start");
    if (!result.started) throw new Error("사이트가 아직 준비되지 않았습니다.");
    view.session = (await api(`/sessions/${view.session.id}`)).session;
    await refreshPlan();
    render();
    message("기록 중입니다.");
  }
  async function finish() {
    const outcome = el("study-outcome").value;
    if (!outcome) throw new Error("수행 결과를 선택해 주세요.");
    message("기록을 종료하고 마지막 로그를 저장하고 있습니다.");
    const data = await api(`/sessions/${view.session.id}/stop`, "POST", {
      status: outcome === "completed" ? "completed" : "abandoned", study_outcome: outcome,
      answer: el("study-answer").value, notes: el("study-notes").value,
    });
    view.session = data.session;
    await refreshPlan();
    render();
    try { await bridge("close"); } catch { /* Saved result remains valid when the extension has disconnected. */ }
    view.opened = false;
    message("답변과 기록을 저장했습니다.");
    const focus = view.plan.finished === view.plan.total ? el("study-complete").querySelector("h2")
      : outcome === "technical" ? el("study-retry") : el("study-next");
    focus.focus();
  }
  document.addEventListener("DOMContentLoaded", () => {
    el("study-tester-form").addEventListener("submit", e => { e.preventDefault(); busy(() => selectTester(el("study-tester").value)); });
    el("study-prepare").addEventListener("click", () => busy(() => prepare()));
    el("study-check").addEventListener("click", () => busy(checkSite));
    el("study-start").addEventListener("click", () => busy(start));
    el("study-next").addEventListener("click", () => busy(() => prepare()));
    el("study-retry").addEventListener("click", () => busy(() => prepare(true)));
    el("study-finish-form").addEventListener("submit", e => { e.preventDefault(); busy(finish); });
    el("study-review").addEventListener("click", () => busy(async () => { pausePolling(); await openSession(view.session.id); history.replaceState({}, "", `/?session=${view.session.id}&review=1`); }));
  });
  return { home, pausePolling };
})();
