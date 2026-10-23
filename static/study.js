/* Assigned-task view, single-window edition (0.4.0), participant-only.
 * Phases: idle -> ready (draft) -> active (recording, site in the iframe) -> answering
 * (result form) -> ended -> next task. Nobody else is at the computer: the site is prepared
 * and logged in automatically, and the participant starts, ends, saves and moves on with
 * the buttons here or the NVDA add-on gestures (which run the same code). Progress lives in
 * SQLite; localStorage only remembers the slot.
 */
window.Study = (() => {
  const RECORDER_TITLE = "A11y Task Recorder";
  // Signal tones (add-on): accepted 880 Hz (end 660 Hz), refused 220 Hz.
  // Read aloud by NVDA: keys are joined with commas, not "+", which Korean NVDA reads as "플러스".
  // 0.4.2: two announced gestures (end NVDA+Ctrl+E, repeat NVDA+Ctrl+H); start is the button.
  const SHORTCUT_INTRO = "과업은 지시문 아래 '과업 시작' 버튼으로 시작합니다. 끝낼 때는 NVDA, Ctrl, E를 함께 누르세요. 지시문을 다시 듣고 싶으면 NVDA, Ctrl, H를 누르세요. 단축키를 누르면 짧은 확인음이 나고, 지금 쓸 수 없으면 낮은 경고음이 납니다.";
  // The task comes first so a participant who stops listening early still hears it.
  const PRACTICE_INSTRUCTION = `연습입니다. 연습 화면에서 '재료 보기' 버튼을 눌러, 첫 번째 재료를 알려 주세요. ${SHORTCUT_INTRO}`;
  const SITE_UNREACHABLE = "사이트에 접속할 수 없습니다. 연구진에게 알려 주세요.";
  const view = { tester: null, plan: null, session: null, ready: false, busy: false, timer: null, opened: false,
    pending: new Map(), lastPoll: 0, checking: false, signalSeq: 0, practice: null,
    pauses: 0, timeLimitHandled: null, lastTitlePoll: 0, frameTitle: "", editing: false, siteError: false,
    confirmEnd: false, changeTester: new URLSearchParams(location.search).has("change-tester") };
  const labels = { completed: "마쳤습니다", blocked: "마치지 못했습니다", timeout: "제한 시간 종료", technical: "기술 문제" };
  const el = id => document.getElementById(id);
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const message = (text, error = false) => {
    el("study-message").textContent = text;
    el("study-message").classList.toggle("error", error);
  };
  const api = (path, method = "GET", body) => request(path, { method, ...(body ? { body } : {}) });
  // Scale the desktop-size iframe to the stage width and the remaining window height.
  function fitFrame() {
    const stage = el("study-stage");
    if (!stage || stage.hidden) return;
    const style = getComputedStyle(stage);
    const w = parseFloat(style.getPropertyValue("--frame-w")) || 1440;
    const h = parseFloat(style.getPropertyValue("--frame-h")) || 900;
    // Depends on the window size only (not on scroll), so the layout stays still while the page scrolls.
    // 96px stays free below the frame for the "과업 끝내기" button.
    const scale = Math.max(0.4, Math.min(1, stage.clientWidth / w, (window.innerHeight - 96) / h));
    stage.style.setProperty("--frame-scale", scale.toFixed(4));
  }
  window.addEventListener("resize", fitFrame);
  if ("ResizeObserver" in window) document.addEventListener("DOMContentLoaded", () => { const s = el("study-stage"); if (s) new ResizeObserver(fitFrame).observe(s.parentElement || s); });
  function remember(tester) { try { localStorage.setItem("study-tester", tester); } catch {} }
  function remembered() { try { return localStorage.getItem("study-tester"); } catch { return null; } }

  // ---- extension bridge ---------------------------------------------------------
  function bridge(action, sessionId = view.session?.id) {
    return new Promise((resolve, reject) => {
      const requestId = crypto.randomUUID();
      const timeout = setTimeout(() => {
        view.pending.delete(requestId);
        reject(new Error("기록 프로그램에 연결할 수 없습니다. 연구진에게 알려 주세요."));
      }, 15000);
      view.pending.set(requestId, { resolve, reject, timeout });
      window.postMessage({ kind: "a11y-study-command", requestId, action, sessionId }, location.origin);
    });
  }
  window.addEventListener("message", event => {
    if (event.source !== window || event.origin !== location.origin || event.data?.kind !== "a11y-study-response") return;
    const pending = view.pending.get(event.data.requestId);
    if (!pending) return;
    clearTimeout(pending.timeout);
    view.pending.delete(event.data.requestId);
    const result = event.data.result;
    if (!result?.ok) pending.reject(new Error(result?.error || SITE_UNREACHABLE));
    else pending.resolve(result);
  });

  async function busy(action) {
    if (view.busy) return;
    view.busy = true;
    renderControls();
    let focusTarget = null;
    try { focusTarget = await action(); } catch (error) {
      try { await reconcileSession(); } catch { /* Keep the original operation error. */ }
      message(error.message, true);
    }
    finally {
      view.busy = false;
      renderControls();
      if (focusTarget instanceof HTMLElement) focusTarget.focus();
    }
  }

  // ---- phases ---------------------------------------------------------------------
  function phase() {
    if (view.practice) return view.practice.phase;
    const status = view.session?.status;
    if (!view.plan) return "idle";
    if (view.plan.finished === view.plan.total && status !== "answering") return "complete";
    if (status === "active") return "active";
    if (status === "answering") return "answering";
    if (status === "completed" || status === "abandoned") return "ended";
    return "ready";
  }
  // Keep a region visible but out of NVDA's virtual buffer and the Tab order.
  function shelve(region, shelved) {
    region.setAttribute("aria-hidden", shelved ? "true" : "false");
    for (const node of region.querySelectorAll("button, input, select, textarea, a, summary, [tabindex]")) {
      if (shelved) {
        if (!node.dataset.shelvedTabindex) node.dataset.shelvedTabindex = node.getAttribute("tabindex") ?? "none";
        node.setAttribute("tabindex", "-1");
      } else if (node.dataset.shelvedTabindex !== undefined) {
        if (node.dataset.shelvedTabindex === "none") node.removeAttribute("tabindex");
        else node.setAttribute("tabindex", node.dataset.shelvedTabindex);
        delete node.dataset.shelvedTabindex;
      }
    }
  }
  function applyPhase() {
    const current = phase();
    el("study-view").dataset.phase = current;
    const frame = el("study-frame");
    const active = current === "active";
    shelve(el("study-top"), active);
    // During a task Shift+Tab and browse mode stay in the site: the recorder header (home link
    // reloaded the copy) and footer are locked (Windows check F2, 10/20).
    for (const region of document.querySelectorAll(".skip-link, .app-header, body > footer")) region.toggleAttribute("inert", active);
    // The frame stays hidden while a task is prepared: the kit logs it in, nobody types a code.
    const showFrame = active || current === "answering" || current === "ended";
    el("study-stage").hidden = !(showFrame || current === "ready");
    requestAnimationFrame(() => { fitFrame(); if (active && !view.stageShown) { view.stageShown = true; el("study-stage").scrollIntoView({ block: "start" }); } if (!active) view.stageShown = false; });
    frame.hidden = !showFrame;
    // Locked while the result is being entered.
    const parked = current === "answering" || current === "ended";
    frame.toggleAttribute("inert", parked);
    frame.setAttribute("aria-hidden", parked ? "true" : "false");
    frame.tabIndex = parked ? -1 : 0;
    el("study-stage-note").textContent = current === "ready"
      ? "사이트는 과업 시작과 함께 표시됩니다." : parked ? "결과를 입력하는 동안 사이트 화면은 잠겨 있습니다." : "";
    el("study-result").hidden = !(current === "answering" || current === "ended");
    document.title = active && view.frameTitle ? view.frameTitle : RECORDER_TITLE;
  }

  // ---- polling ------------------------------------------------------------------
  function pausePolling() { clearInterval(view.timer); view.timer = null; }
  function resumePolling() {
    pausePolling();
    view.timer = setInterval(async () => {
      if (view.checking || el("study-view").hidden) return;
      updateTimer();
      await pollSignals();
      if (view.busy) return;
      if (phase() === "active" && Date.now() - view.lastTitlePoll > 2000) {
        view.lastTitlePoll = Date.now();
        try { const status = await bridge("status"); if (status.frame_title) { view.frameTitle = status.frame_title; applyPhase(); } } catch { /* title is cosmetic */ }
      }
      if (Date.now() - view.lastPoll < 4000) return;
      view.lastPoll = Date.now();
      await checkConnection();
      try { await reconcileSession(); } catch (error) { message(error.message, true); }
      if (view.busy || view.practice) return;
      // A time-limit end whose automatic save failed (network, reload) is saved again here.
      if (phase() === "answering" && view.session.end_reason === "time_limit") { await busy(saveTimeLimit); return; }
      if (view.session?.status === "draft" && view.opened) {
        view.checking = true;
        try { await checkSite(); } catch (error) { view.ready = false; message(error.message, true); renderControls(); }
        finally { view.checking = false; }
      }
    }, 1000);
  }
  async function pollSignals() {
    let data;
    try { data = await api(`/signals?after=${view.signalSeq}`); } catch { return; }
    view.signalSeq = data.seq;
    for (const signal of data.signals) await handleSignal(signal);
  }
  async function handleSignal(signal) {
    if (view.practice) { practiceSignal(signal); return; }
    if (!view.session || view.busy) return;
    const byParticipant = signal.source === "nvda_addon";
    if (signal.kind === "start" && phase() === "ready" && view.ready) await busy(start);
    else if (signal.kind === "end" && phase() === "active") await busy(() => endTask(byParticipant ? "participant_gesture" : "facilitator_gesture"));
    else if (signal.kind === "repeat") logStudyEvent("instruction_repeat", { method: "nvda_addon", phase: phase() });
  }
  function logStudyEvent(type, payload = {}) {
    if (!view.session) return Promise.resolve();
    return api(`/sessions/${view.session.id}/study-events`, "POST", { type, payload }).catch(() => {});
  }

  // ---- data -----------------------------------------------------------------------
  async function home(tester) {
    pausePolling();
    stopPolling();
    showView("study");
    try {
      const index = await api("/study");
      el("study-version").textContent = `${index.testers.length}개 테스터 배정`;
      el("study-tester").replaceChildren(new Option("번호 선택", ""), ...index.testers.map(t => new Option(`${t.id} (${t.finished}/${t.total})`, t.id)));
      // ?change-tester=1 (researcher only) shows the tester picker instead of reopening the remembered tester.
      const code = view.changeTester ? null : tester || view.tester || new URLSearchParams(location.search).get("tester") || remembered();
      if (index.testers.some(t => t.id === code)) {
        el("study-tester").value = code;
        await selectTester(code);
        // The picker is gone once a tester is known, so the current task (new, or prepared but not
        // started after a reload or reopened kit) is prepared straight away.
        if (phase() === "ready") busy(() => prepare());
      } else message("테스터 번호를 선택한 뒤 배정 확인을 눌러 주세요.");
    } catch (error) { message(error.message, true); }
    resumePolling();
  }
  async function selectTester(tester) {
    if (!tester) return;
    const active = (await api("/active-session")).session;
    if (active && active.study?.tester !== tester) {
      el("study-tester").value = view.tester || "";
      throw new Error("다른 과업이 기록 중입니다. 연구진에게 알려 주세요.");
    }
    const plan = await api(`/study/${encodeURIComponent(tester)}`);
    view.tester = tester;
    view.changeTester = false;
    view.plan = plan;
    view.session = null;
    view.ready = false;
    view.opened = false;
    remember(tester);
    history.replaceState({}, "", `/?tester=${tester}`);
    resetForms();
    const current = plan.tasks.find(t => t.order === plan.current_order);
    const previous = plan.tasks.find(t => t.order === (plan.current_order || plan.total + 1) - 1);
    const row = current?.session_id ? current : previous?.session_id ? previous : null;
    if (row) view.session = (await api(`/sessions/${row.session_id}`)).session;
    if (view.session && ["draft", "active", "answering"].includes(view.session.status)) {
      el("study-frame").src = view.session.target_url;
      view.opened = true;
    }
    render();
    message("");
  }
  // 배정 확인: choose the tester, then prepare the current task straight away.
  async function confirmTester(tester) {
    if (!tester) throw new Error("테스터 번호를 선택해 주세요.");
    await selectTester(tester);
    return phase() === "ready" ? prepare() : phase() === "complete" ? el("study-complete").querySelector("h2") : null;
  }
  function resetForms() {
    el("study-answer").value = "";
    for (const radio of document.querySelectorAll('input[name="study-done"]')) radio.checked = false;
    el("study-answer-form").hidden = false;
    el("study-saved-box").hidden = true;
    view.timeLimitHandled = null;
    view.editing = false;
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
  const firstTask = () => view.plan?.finished === 0 && (currentTask()?.order ?? 1) === 1;

  // ---- rendering ------------------------------------------------------------------
  function renderControls() {
    const current = phase();
    const practice = Boolean(view.practice);
    const ended = current === "ended";
    el("study-start").hidden = !(current === "ready" && view.ready);
    el("study-practice").hidden = practice || current !== "ready" || !firstTask();
    if (current !== "active") view.confirmEnd = false;
    el("study-end").hidden = current !== "active" || view.confirmEnd;
    el("study-end-confirm").hidden = current !== "active" || !view.confirmEnd;
    el("study-shortcuts").hidden = current !== "ready";
    // Once a tester is confirmed the picker leaves the page; researchers reopen with ?change-tester=1 or ?tester=.
    const locked = Boolean(view.tester) && !view.changeTester;
    el("study-tester-form").hidden = locked;
    el("study-records").hidden = locked;
    el("study-next").hidden = practice || !ended;
    el("study-practice-stop").hidden = !(practice && ended);
    el("study-tester").disabled = current === "active" || current === "answering" || view.busy || practice;
    el("study-select").disabled = el("study-tester").disabled;
    el("study-view").querySelectorAll(".study-actions button, #study-submit, #study-end, #study-end-confirm button").forEach(b => { b.disabled = view.busy; });
    applyPhase();
  }
  function render() {
    const plan = view.plan;
    if (!plan) return;
    if (view.practice) { renderPractice(); return; }
    const complete = plan.finished === plan.total && view.session?.status !== "answering";
    el("study-goal").hidden = complete;
    el("study-complete").hidden = !complete;
    const task = currentTask() || plan.tasks[plan.tasks.length - 1];
    el("study-order").textContent = `${view.tester} · 과업 ${task.order} / ${plan.total}`;
    el("study-site").textContent = task.site_name;
    el("study-instruction").textContent = task.instruction;
    el("study-answer-hint").textContent = task.requires_answer === false
      ? "답이 없는 과업입니다. 비워 두셔도 됩니다." : "들은 내용이나 화면에 나온 답을 적어 주세요.";
    const option = [...el("study-tester").options].find(o => o.value === view.tester);
    if (option) option.textContent = `${view.tester} (${plan.finished}/${plan.total})`;
    const current = phase();
    const timeLimit = view.session?.end_reason === "time_limit";
    const blockBreak = current === "ended" && task.order % 4 === 0 && task.order < plan.total;
    el("study-break").hidden = !blockBreak;
    el("study-next").setAttribute("aria-describedby", blockBreak ? "study-saved study-break" : "study-saved");
    if (current === "ended") {
      const outcome = view.session.study_outcome;
      el("study-result-heading").textContent = view.editing ? "결과를 고친 뒤 저장해 주세요."
        : timeLimit ? "제한 시간이 되어 과업을 마쳤습니다." : "결과를 저장했습니다.";
      el("study-saved").textContent = `저장했습니다. ${labels[outcome] || "종료"}${view.session.study_answer ? ` · 답: ${view.session.study_answer}` : ""}`;
      el("study-answer-form").hidden = !view.editing;
      el("study-saved-box").hidden = view.editing;
      el("study-edit").hidden = !["completed", "blocked"].includes(outcome);
    }
    if (current === "answering") {
      el("study-result-heading").textContent = timeLimit
        ? "제한 시간이 되어 과업을 마쳤습니다. 결과를 저장하고 있습니다." : "과업이 끝났습니다. 결과를 입력해 주세요.";
      el("study-answer-form").hidden = timeLimit;
      el("study-saved-box").hidden = true;
    }
    updateTimer();
    renderControls();
  }
  function netElapsedSeconds() {
    const session = view.session;
    if (!session?.started_at) return 0;
    const now = Date.now();
    const pausedNow = session.paused_at ? Math.max(0, now - Date.parse(session.paused_at)) : 0;
    return Math.max(0, Math.floor((now - Date.parse(session.started_at) - view.pauses - pausedNow) / 1000));
  }
  function updateTimer() {
    const task = currentTask();
    if (!task || view.practice) return;
    const active = phase() === "active";
    const limit = task.time_limit_seconds;
    const elapsed = active ? netElapsedSeconds() : 0;
    const timer = el("study-timer");
    timer.textContent = active
      ? `경과 ${Math.floor(elapsed / 60)}분 ${elapsed % 60}초 · 제한 ${limit / 60}분${elapsed >= limit ? " · 제한 시간에 도달했습니다." : ""}`
      : `제한 시간 ${limit / 60}분`;
    timer.classList.toggle("study-timer-over", active && elapsed >= limit);
    if (active && elapsed >= limit && !view.session.paused_at && view.timeLimitHandled !== view.session.id && !view.busy) {
      view.timeLimitHandled = view.session.id;
      logStudyEvent("time_limit_reached", { net_task_seconds: elapsed });
      busy(() => endTask("time_limit"));
    }
  }

  // ---- preparation (automatic; no facilitator) -----------------------------------
  function frameLoaded(timeout = 15000) {
    return new Promise(resolve => {
      const done = () => { clearTimeout(timer); resolve(); };
      const timer = setTimeout(done, timeout);
      el("study-frame").addEventListener("load", done, { once: true });
    });
  }
  // Ask the extension until the site is ready or clearly needs a login; transient errors are retried.
  async function waitForSite(sessionId, timeout = 20000) {
    const deadline = Date.now() + timeout;
    let result = null, failure = null, prepared = false;
    for (;;) {
      try { result = await bridge(prepared ? "check" : "prepare"); prepared = true; failure = null; }
      catch (error) { failure = error; }
      if (view.session?.id !== sessionId) return null;
      if (!failure && (result.ready || result.reason === "login_required")) return result;
      if (Date.now() > deadline) break;
      await sleep(500);
    }
    if (failure) throw failure;
    return result;
  }
  // Log the iframe in with the kit's access code: a hidden form posts to <copy>/access into the frame.
  async function autoLogin(sessionId) {
    const access = await api("/study/access").catch(() => ({ code: null }));
    const target = new URL(view.session.target_url);
    // Never post the code to a site we do not host (tasks on a partner's copies).
    if (!access.code || target.origin !== access.origin) return null;
    const form = document.createElement("form");
    form.method = "post";
    form.action = `${target.origin}/access`;
    form.target = el("study-frame").name;
    form.hidden = true;
    for (const [name, value] of [["code", access.code], ["next", target.pathname]]) {
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = name;
      input.value = value;
      form.append(input);
    }
    document.body.append(form);
    const loaded = frameLoaded();
    try { form.submit(); } finally { form.remove(); }
    await loaded;
    return waitForSite(sessionId);
  }
  async function prepare(retry = false) {
    const endpoint = retry ? `/study/${view.tester}/retry` : `/study/${view.tester}/prepare`;
    const data = await api(endpoint, "POST", retry ? { session_id: view.session.id } : {});
    view.session = data.session;
    view.ready = false;
    view.siteError = false;
    view.pauses = 0;
    resetForms();
    await refreshPlan();
    render();
    if (view.session.status !== "draft") return null;
    const sessionId = view.session.id;
    message("배정된 사이트를 준비하고 있습니다. 잠시 기다려 주세요.");
    const loaded = frameLoaded();
    el("study-frame").src = view.session.target_url;
    view.opened = true;
    await loaded;
    let result = await waitForSite(sessionId);
    // No code in the kit leaves the login page up: that is a site error, never a prompt for the participant.
    if (result?.reason === "login_required") result = (await autoLogin(sessionId)) || result;
    if (!result || view.session?.id !== sessionId) return null;
    view.siteError = !result.ready;
    applyPreparation(result);
    if (!result.ready) throw new Error(SITE_UNREACHABLE);
    return presentInstruction();
  }
  function applyPreparation(result) {
    const wasReady = view.ready;
    view.ready = Boolean(result.ready);
    if (result.frame_title) view.frameTitle = result.frame_title;
    if (view.ready) view.siteError = false;
    // A login page means the kit's code is missing or refused; the participant is never asked for it.
    if (!view.ready && (view.siteError || result.reason === "login_required")) message(SITE_UNREACHABLE, true);
    else message(result.ready ? "사이트가 준비되었습니다. 지시문을 들은 뒤 과업 시작을 누르세요."
      : "사이트 준비를 확인하고 있습니다.");
    renderControls();
    return !wasReady && view.ready;
  }
  async function checkSite() {
    const sessionId = view.session.id;
    const result = await bridge("check");
    if (view.session?.id !== sessionId || view.session.status !== "draft") return;
    // Became ready in the background (e.g. the site came back): read the instruction once.
    if (applyPreparation(result)) presentInstruction().focus();
  }
  // Focus the instruction so NVDA reads it (announce mode "focus"); "과업 시작" follows on Tab.
  function presentInstruction() {
    logStudyEvent("instruction_presented", { method: "focus" });
    return el("study-instruction");
  }

  // ---- task actions ---------------------------------------------------------------
  async function start() {
    await checkConnection();
    if (!state.nvdaConnected) throw new Error("NVDA가 연결되지 않아 과업을 시작하지 않았습니다. 연구진에게 알려 주세요.");
    const result = await bridge("start");
    if (!result.started) throw new Error("사이트가 아직 준비되지 않았습니다. 잠시 뒤 다시 눌러 주세요.");
    if (result.frame_title) view.frameTitle = result.frame_title;
    view.session = (await api(`/sessions/${view.session.id}`)).session;
    view.pauses = 0;
    await refreshPlan();
    render();
    message("기록 중입니다.");
    requestAnimationFrame(focusSite);
    return el("study-frame");
  }
  // After busy() focuses the frame, move focus to the site's first control as on a newly opened
  // page, so NVDA's cursor starts inside the site (Windows check F3, 10/20). The practice page is
  // same-origin; a copy is asked through the extension's content script in the frame.
  function focusSite() {
    const frame = el("study-frame");
    if (phase() !== "active" || document.activeElement !== frame) return;
    const doc = frame.contentDocument;
    if (doc) firstControl(doc)?.focus();
    else frame.contentWindow?.postMessage({ kind: "a11y-study-focus-site" }, "*");
  }
  function firstControl(doc) {
    const selector = 'a[href], button, input:not([type="hidden"]), select, textarea, summary, [tabindex]:not([tabindex="-1"])';
    return [...doc.querySelectorAll(selector)].find(node => !node.disabled && node.getClientRects().length > 0 && !node.closest("[inert], [aria-hidden='true']"));
  }
  async function endTask(reason) {
    const data = await api(`/sessions/${view.session.id}/end-task`, "POST", { reason });
    view.session = data.session;
    await refreshPlan();
    render();
    if (reason === "time_limit") return saveTimeLimit();
    message("과업을 마쳤습니다. 결과를 입력해 주세요.");
    return el("study-result-heading");
  }
  // Time limit: nobody is asked; the result is saved as timeout with an empty answer.
  async function saveTimeLimit() {
    const target = await finish("timeout", "", "system");
    message("제한 시간이 되어 과업을 마쳤습니다.");
    return view.plan.finished === view.plan.total ? target : el("study-next");
  }
  async function finish(outcome, answer, enteredBy) {
    if (!outcome) throw new Error("과업을 마쳤는지 골라 주세요.");
    message("결과와 기록을 저장하고 있습니다.");
    const data = await api(`/sessions/${view.session.id}/stop`, "POST", {
      status: outcome === "completed" ? "completed" : "abandoned", study_outcome: outcome,
      answer, entered_by: enteredBy,
    });
    view.session = data.session;
    await refreshPlan();
    render();
    try { await bridge("close"); } catch { /* Saved result remains valid when the extension has disconnected. */ }
    view.opened = false;
    message("결과와 기록을 저장했습니다.");
    return view.plan.finished === view.plan.total ? el("study-complete").querySelector("h2") : el("study-saved");
  }
  async function submitParticipant() {
    const outcome = document.querySelector('input[name="study-done"]:checked')?.value;
    if (!outcome) throw new Error("과업을 마쳤는지 골라 주세요.");
    return finish(outcome, el("study-answer").value, "participant");
  }
  async function editAnswer() {
    el("study-answer").value = view.session.study_answer || "";
    for (const radio of document.querySelectorAll('input[name="study-done"]')) radio.checked = radio.value === view.session.study_outcome;
    view.editing = true;
    render();
    return el("study-result-heading");
  }
  async function saveEdit() {
    const outcome = document.querySelector('input[name="study-done"]:checked')?.value;
    if (!outcome) throw new Error("과업을 마쳤는지 골라 주세요.");
    const data = await api(`/sessions/${view.session.id}/answer`, "PATCH", {
      study_outcome: outcome, answer: el("study-answer").value, entered_by: "participant",
    });
    view.session = data.session;
    view.editing = false;
    await refreshPlan();
    render();
    message("결과를 고쳐 저장했습니다.");
    return el("study-saved");
  }
  // 다음 과업. A legacy "technical" result is re-prepared instead of skipped.
  function nextTask() { return prepare(view.session?.study_outcome === "technical"); }

  // 과업 끝내기 button: ask first (focus on 계속하기); the E gesture and the time limit end without asking.
  function openEndConfirm() { view.confirmEnd = true; renderControls(); return el("study-end-no"); }
  function closeEndConfirm() { view.confirmEnd = false; renderControls(); return el("study-frame"); }

  // ---- practice (대본 §4.4; nothing is recorded) -------------------------------------
  async function startPractice() {
    await api("/signals/practice", "POST", { on: true });
    view.practice = { phase: "ready" };
    resetForms();
    el("study-frame").src = "/practice.html";
    view.frameTitle = "";
    renderPractice();
    message("연습입니다. 연습은 기록하지 않습니다. 지시문을 들은 뒤 과업 시작을 누르세요.");
    return el("study-instruction");
  }
  // 연습 끝내기: leave practice and prepare the first real task.
  async function stopPractice() {
    await api("/signals/practice", "POST", { on: false });
    view.practice = null;
    el("study-frame").src = "about:blank";
    resetForms();
    render();
    message("연습을 끝냈습니다.");
    return prepare();
  }
  function renderPractice() {
    const current = view.practice.phase;
    el("study-goal").hidden = false;
    el("study-complete").hidden = true;
    el("study-order").textContent = "연습 (기록하지 않음)";
    el("study-site").textContent = "연습 화면: 오늘의 요리";
    el("study-instruction").textContent = PRACTICE_INSTRUCTION;
    el("study-timer").textContent = "연습에는 제한 시간이 없습니다.";
    el("study-answer-hint").textContent = "연습입니다. 첫 번째 재료를 적어 보세요.";
    el("study-result-heading").textContent = current === "ended" ? "연습이 끝났습니다." : "과업이 끝났습니다. 결과를 입력해 주세요.";
    el("study-answer-form").hidden = current !== "answering";
    el("study-saved-box").hidden = current !== "ended";
    el("study-edit").hidden = true;
    el("study-break").hidden = true;
    view.ready = true;
    renderControls();
  }
  function practiceSignal(signal) {
    if (signal.kind === "start" && view.practice.phase === "ready") practiceStart().focus();
    else if (signal.kind === "end" && view.practice.phase === "active") practiceEnd().focus();
  }
  function practiceStart() { view.practice.phase = "active"; renderPractice(); message("연습 중입니다."); requestAnimationFrame(focusSite); return el("study-frame"); }
  function practiceEnd() { view.practice.phase = "answering"; renderPractice(); message("연습 과업이 끝났습니다. 결과를 입력해 주세요."); return el("study-result-heading"); }
  function practiceSubmit() {
    const outcome = document.querySelector('input[name="study-done"]:checked')?.value;
    if (!outcome) throw new Error("과업을 마쳤는지 골라 주세요.");
    view.practice.phase = "ended";
    el("study-saved").textContent = `연습 결과를 확인했습니다. 답: ${el("study-answer").value || "(없음)"}. 연습 끝내기를 누르면 본 과업을 준비합니다.`;
    renderPractice();
    return el("study-saved");
  }

  document.addEventListener("DOMContentLoaded", () => {
    const on = (id, event, handler) => el(id).addEventListener(event, handler);
    on("study-tester-form", "submit", e => { e.preventDefault(); busy(() => confirmTester(el("study-tester").value)); });
    on("study-start", "click", () => busy(view.practice ? async () => practiceStart() : start));
    on("study-end", "click", () => { if (!view.busy && phase() === "active") openEndConfirm().focus(); });
    on("study-end-yes", "click", () => busy(view.practice ? async () => practiceEnd() : () => endTask("participant_button")));
    on("study-end-no", "click", () => closeEndConfirm().focus());
    on("study-end-confirm", "keydown", e => { if (e.key === "Escape" && !view.busy) { e.preventDefault(); closeEndConfirm().focus(); } });
    on("study-next", "click", () => busy(nextTask));
    on("study-answer-form", "submit", e => { e.preventDefault(); busy(view.practice ? async () => practiceSubmit() : view.editing ? saveEdit : submitParticipant); });
    on("study-edit", "click", () => busy(editAnswer));
    on("study-practice", "click", () => busy(startPractice));
    on("study-practice-stop", "click", () => busy(stopPractice));
  });
  return { home, pausePolling };
})();
