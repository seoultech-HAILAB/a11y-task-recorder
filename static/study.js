/* Assigned-task view, single-window edition (0.4.0), participant-only.
 * Phases: idle -> ready (draft) -> active (recording, site in the iframe) -> answering
 * (result form) -> next task. Nobody else is at the computer: the site is prepared
 * and logged in automatically, and the participant starts, ends, saves and moves on with
 * the buttons here or the NVDA add-on gestures (which run the same code). Progress lives in
 * SQLite; localStorage only remembers the slot.
 * 0.4.4: mistakes can be undone without losing data. A wrong tester number is changed, a
 * task is restarted (the attempt is kept as `aborted` and a new attempt is prepared), and
 * an end pressed by mistake goes back to the task from the result form.
 * 0.5.0 (participant feedback 10/28): the participant hears only what the task needs. The
 * tester types the number they were given; the gestures are taught once, in the practice,
 * which opens by itself the first time and can be repeated before any task; an instruction is
 * the scenario sentence alone and is read as soon as the task screen appears; saving the
 * result goes straight to the next instruction; status lines are not announced; and focus
 * stays inside the site until NVDA+Ctrl+E.
 */
window.Study = (() => {
  const RECORDER_TITLE = "A11y Task Recorder";
  // Signal tones (add-on): accepted 880 Hz (end 660 Hz), refused 220 Hz.
  // Read aloud by NVDA: keys are joined with commas, not "+", which Korean NVDA reads as "플러스".
  // The practice is the only place the gestures are explained (0.5.0). NVDA+Ctrl+H in the
  // practice reads PRACTICE_TASK alone, as it reads the scenario alone in a real task
  // (signals.PRACTICE_INSTRUCTION is the same sentence).
  const PRACTICE_TASK = "연습 화면에서 '재료 보기' 버튼을 눌러 첫 번째 재료를 확인하세요.";
  const PRACTICE_GUIDE = "연습입니다. 기록하지 않습니다. 본 과업에서 쓰는 키를 익힙니다. "
    + "지시문을 다시 들으려면 NVDA, Ctrl, H를 누르세요. 과업 중에도 됩니다. "
    + "답을 찾았거나 더 할 수 없으면 그 자리에서 NVDA, Ctrl, E를 눌러 과업을 끝내세요. "
    + "사이트에서 이전 페이지는 Alt, 왼쪽 화살표, 과업 첫 화면은 Alt, Home입니다. "
    + "본 과업에서는 이 안내를 다시 하지 않습니다. "
    + `연습 과업: ${PRACTICE_TASK} 지금 NVDA, Ctrl, H를 한 번 눌러 본 뒤 과업 시작을 누르세요.`;
  const SITE_UNREACHABLE = "사이트에 접속할 수 없습니다. 과업 시작을 다시 누르거나 연구진에게 알려 주세요.";
  // The participant hears the short message with the cause label; the researcher gets the
  // extension's reason in the site_preparation event (server window, result package).
  // 0.5.2: 과업 시작 loads the site again (Windows 10/28, 17-1), so the sentence says so.
  const siteUnreachable = label => `사이트에 접속할 수 없습니다${label ? `(${label})` : ""}. 과업 시작을 다시 누르거나 연구진에게 알려 주세요.`;
  const FRAME_TITLE = "과업 사이트";
  const view = { tester: null, testerIds: [], plan: null, session: null, ready: false, busy: false, timer: null, opened: false,
    pending: new Map(), lastPoll: 0, checking: false, signalSeq: 0, practice: null,
    timeLimitHandled: null, lastTitlePoll: 0, frameTitle: "", siteError: false,
    // The site of a prepared task loads in the background (0.5.0); a newer preparation or the
    // practice bumps loadToken so an older load never reports into it.
    siteLoading: null, loadToken: 0,
    // The open in-page confirm step: "end", "restart" (below the site), "redo" (result form), "tester".
    confirm: null, changeTester: new URLSearchParams(location.search).has("change-tester"),
    siteStartKey: null, pendingBack: null, refocusUntil: 0, siteCause: "", prepBegan: 0 };
  const labels = { completed: "성공", blocked: "실패", timeout: "제한 시간 종료", technical: "기술 문제" };
  const el = id => document.getElementById(id);
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  // Errors go to the alert line and NVDA reads them; progress lines are on screen only
  // (aria-hidden), so "준비하고 있습니다"-style status never interrupts the participant (0.5.0).
  const message = (text, error = false) => {
    setText("study-alert", error ? text : "");
    setText("study-message", error ? "" : text);
  };
  function alertAgain(text) {
    const alert = el("study-alert");
    alert.replaceChildren();
    setText("study-message", "");
    setTimeout(() => { const line = document.createElement("span"); line.textContent = text; alert.replaceChildren(line); }, 250);
  }
  function remembered() { try { return localStorage.getItem("study-tester"); } catch { return null; } }
  const practicedKey = tester => `study-practiced-${tester}`;
  function markPracticed() { try { localStorage.setItem(practicedKey(view.tester), "1"); } catch {} }
  function practiced() { try { return localStorage.getItem(practicedKey(view.tester)) === "1"; } catch { return false; } }
  const api = (path, method = "GET", body) => request(path, { method, ...(body ? { body } : {}) });
  // The 1 s poll re-renders; unchanged text and attributes are left alone so NVDA's buffer is
  // not refreshed under the participant's cursor every few seconds.
  const setText = (id, text) => { const node = el(id); if (node.textContent !== text) node.textContent = text; };
  const setAttr = (node, name, value) => { if (node.getAttribute(name) !== value) node.setAttribute(name, value); };
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

  // ---- extension bridge ---------------------------------------------------------
  function bridge(action, sessionId = view.session?.id) {
    return new Promise((resolve, reject) => {
      const requestId = crypto.randomUUID();
      const timeout = setTimeout(() => {
        view.pending.delete(requestId);
        reject(Object.assign(new Error("기록 프로그램에 연결할 수 없습니다. 연구진에게 알려 주세요."), { reason: "bridge_timeout" }));
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
    if (!result?.ok) pending.reject(Object.assign(new Error(result?.error || SITE_UNREACHABLE), { reason: result?.reason || "unknown" }));
    else pending.resolve(result);
  });

  // Buttons are not disabled while an action runs: a focused button that becomes disabled drops
  // focus to the page and NVDA reads the page again (0.5.0). busy() itself refuses re-entry.
  async function busy(action) {
    if (view.busy) return;
    view.busy = true;
    el("study-view").toggleAttribute("data-busy", true);
    let focusTarget = null;
    try { focusTarget = await action(); } catch (error) {
      try { await reconcileSession(); } catch { /* Keep the original operation error. */ }
      // The same error after the same action is read again: NVDA did not repeat an alert whose
      // text was emptied and set back within 50 ms (Windows 10/28, 17-1), so a new node is added.
      alertAgain(error.message);
    }
    finally {
      view.busy = false;
      el("study-view").toggleAttribute("data-busy", false);
      renderControls();
      if (focusTarget instanceof HTMLElement) focusTarget.focus();
      if (view.startPending) {
        view.startPending = false;
        if (phase() === "ready" && !view.practice) setTimeout(() => busy(start), 0);
      }
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
    setAttr(region, "aria-hidden", shelved ? "true" : "false");
    for (const node of region.querySelectorAll("button, input, select, textarea, a, summary, [tabindex]")) {
      if (shelved) {
        if (!node.dataset.shelvedTabindex) node.dataset.shelvedTabindex = node.getAttribute("tabindex") ?? "none";
        setAttr(node, "tabindex", "-1");
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
    // Leaving the practice: NVDA read "A11y Task Recorder 문서, 알 수 없음, 프레임 끝 …" before
    // the result heading (10/28, 10/29). Focus goes to the heading first and the same-origin
    // practice page stays shown 0.4 s longer. Real tasks keep the 0.5.0 order (site locked,
    // then the heading): moving focus first there added the same noise (10/29, 3 of 4).
    const parking = (current === "answering" || current === "ended") && !frame.hasAttribute("inert");
    if (parking && view.practice && (document.activeElement === frame || el("study-stage").contains(document.activeElement))) {
      el("study-result").hidden = false;
      el("study-result-heading").focus();
      view.holdStageUntil = Date.now() + 400;
      setTimeout(applyPhase, 450);
    }
    const holding = Date.now() < (view.holdStageUntil || 0);
    shelve(el("study-top"), active);
    // The recorder header and footer are locked whenever this view is shown (app.js showView).
    // The site shows only during the task (0.5.0): it is prepared out of sight, and while the
    // result is entered it is put away with its page kept, so 과업으로 돌아가기 returns to it.
    el("study-stage").hidden = !(active || holding);
    requestAnimationFrame(() => { fitFrame(); if (active && !view.stageShown) { view.stageShown = true; el("study-stage").scrollIntoView({ block: "start" }); } if (!active) view.stageShown = false; });
    frame.hidden = !(active || current === "answering" || current === "ended");
    // Locked while the result is being entered.
    const parked = (current === "answering" || current === "ended") && !holding;
    if (frame.hasAttribute("inert") !== parked) frame.toggleAttribute("inert", parked);
    setAttr(frame, "aria-hidden", parked ? "true" : "false");
    setAttr(frame, "tabindex", parked ? "-1" : "0");
    setAttr(frame, "title", FRAME_TITLE);
    for (const id of ["study-trap-start", "study-trap-end"]) setAttr(el(id), "tabindex", active ? "0" : "-1");
    for (const id of ["study-page-start", "study-page-end"]) setAttr(el(id), "tabindex", active ? "-1" : "0");
    el("study-result").hidden = !(current === "answering" || current === "ended");
    const title = active && view.frameTitle ? view.frameTitle : RECORDER_TITLE;
    if (document.title !== title) document.title = title;
  }

  // ---- polling ------------------------------------------------------------------
  function pausePolling() { clearInterval(view.timer); view.timer = null; }
  function resumePolling() {
    pausePolling();
    view.timer = setInterval(async () => {
      if (view.checking || el("study-view").hidden) return;
      checkTimeLimit();
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
      if (view.session?.status === "draft" && view.opened && !view.siteLoading) {
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
    if (signal.kind === "start" && phase() === "ready") await busy(start);
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
      view.testerIds = index.testers.map(t => t.id);
      // ?change-tester=1 (researcher only) shows the number field instead of reopening the remembered tester.
      const code = view.changeTester ? null : tester || view.tester || new URLSearchParams(location.search).get("tester") || remembered();
      if (view.testerIds.includes(code)) {
        await selectTester(code);
        // The number field is gone once a tester is known, so the current task (new, or prepared
        // but not started after a reload or reopened kit) is prepared straight away.
        await busy(afterTester);
      } else {
        message("받은 테스터 번호를 입력한 뒤 시작을 눌러 주세요.");
        el("study-tester").focus();
      }
    } catch (error) { message(error.message, true); }
    resumePolling();
  }
  // Typed number to a catalog id: "t1", "1", "01" and "T01" all name T01.
  function matchTester(raw) {
    const text = String(raw || "").replace(/\s+/g, "").toUpperCase();
    if (!text) return null;
    const exact = view.testerIds.find(id => id.toUpperCase() === text);
    if (exact) return exact;
    const parts = value => value.toUpperCase().match(/^([A-Z]*)-?0*(\d+)$/);
    const typed = parts(text);
    if (!typed) return null;
    return view.testerIds.find(id => { const known = parts(id); return known && known[2] === typed[2] && (!typed[1] || typed[1] === known[1]); }) || null;
  }
  // After the number: the first time, the practice opens by itself; otherwise the current task.
  async function afterTester() {
    const current = phase();
    if (current === "complete") return el("study-complete").querySelector("h2");
    if (current === "ready" && firstTask() && !practiced()) return startPractice();
    if (current === "ready" || current === "ended") return prepare(view.session?.study_outcome === "technical");
    return current === "answering" ? el("study-result-heading") : current === "active" ? el("study-frame") : null;
  }
  async function assertNoOtherRecording(tester) {
    const active = (await api("/active-session")).session;
    if (active && active.study?.tester !== tester) {
      el("study-tester").value = view.tester || "";
      throw new Error("다른 과업이 기록 중입니다. 연구진에게 알려 주세요.");
    }
  }
  async function selectTester(tester) {
    if (!tester) return;
    await assertNoOtherRecording(tester);
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
  // 시작: the typed number names a tester; then the practice (first time) or the current task.
  async function confirmTester(raw) {
    if (!String(raw || "").trim()) throw new Error("테스터 번호를 입력해 주세요.");
    const tester = matchTester(raw);
    if (!tester) throw new Error("등록되지 않은 테스터 번호입니다. 받은 번호를 다시 확인해 주세요.");
    if (view.plan && tester === view.tester) return keepTester();
    // A task prepared for the wrong number but never started is closed (kept as cancelled), so
    // NVDA+Ctrl+H and the start gesture cannot pick up the other tester's task.
    if (view.tester && view.session?.status === "draft" && view.session.study?.tester === view.tester) {
      await assertNoOtherRecording(tester);
      await api(`/sessions/${view.session.id}/abort`, "POST", { reason: "tester_change", trigger: "tester_picker", by: "participant", to_tester: tester });
    }
    await selectTester(tester);
    return afterTester();
  }
  function resetForms() {
    el("study-answer").value = "";
    for (const radio of document.querySelectorAll('input[name="study-done"]')) radio.checked = false;
    el("study-answer-form").hidden = false;
    el("study-saved-box").hidden = true;
    view.timeLimitHandled = null;
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
    // 과업 시작 shows as soon as the instruction does; the site may still be loading (start waits).
    el("study-start").hidden = current !== "ready";
    // The practice can be opened again from any screen without an open task (0.5.1): an
    // instruction, a time-limit stop or the completion screen. Skipped while it is open.
    el("study-practice").hidden = practice || !["ready", "ended"].includes(current);
    el("study-practice-complete").hidden = practice || current !== "complete";
    el("study-practice-skip").hidden = !(practice && current === "ready");
    // Once a tester is confirmed the number field leaves the page; 테스터 번호 바꾸기 brings it
    // back (researchers still have ?change-tester=1 and ?tester=). Not while a task is open.
    const locked = Boolean(view.tester) && !view.changeTester;
    const testerChangeable = locked && !practice && ["ready", "ended", "complete"].includes(current);
    const undo = current === "answering" && (practice || view.session?.end_reason !== "time_limit");
    // A confirm step belongs to one phase; leaving that phase closes it.
    const open = { end: current === "active", restart: current === "active", redo: undo, tester: testerChangeable };
    if (view.confirm && !open[view.confirm]) view.confirm = null;
    const asking = Boolean(view.confirm);
    el("study-end").hidden = current !== "active" || asking;
    el("study-site-nav").hidden = current !== "active" || asking;
    el("study-restart").hidden = current !== "active" || asking;
    el("study-end-confirm").hidden = view.confirm !== "end";
    el("study-restart-confirm").hidden = view.confirm !== "restart";
    el("study-undo").hidden = !undo;
    el("study-return").hidden = view.confirm === "redo";
    el("study-redo").hidden = view.confirm === "redo";
    el("study-redo-confirm").hidden = view.confirm !== "redo";
    el("study-tester-form").hidden = locked;
    el("study-records").hidden = locked;
    el("study-tester-keep").hidden = !(view.tester && view.changeTester);
    el("study-tester-tools").hidden = !testerChangeable;
    el("study-tester-change").hidden = view.confirm === "tester";
    el("study-tester-confirm").hidden = view.confirm !== "tester";
    el("study-next").hidden = practice || !ended;
    el("study-practice-stop").hidden = !(practice && ended);
    el("study-practice-again").hidden = !(practice && ended);
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
    // The scenario sentence alone (0.5.0): no site name, tester, time or shortcut lines.
    setText("study-order", `과업 ${task.order} / ${plan.total}`);
    setText("study-instruction", String(task.instruction || "").trim());
    setText("study-answer-label", task.requires_answer === false ? "답 (없으면 비워 두세요)" : "답");
    const current = phase();
    const timeLimit = view.session?.end_reason === "time_limit";
    if (current === "ended") {
      // Only a time-limit end (or a reopened kit) stops here; a saved result goes to the next task.
      const outcome = view.session.study_outcome;
      setText("study-result-heading", timeLimit ? "제한 시간이 지나 과업이 끝났습니다." : "과업 결과");
      setText("study-saved", timeLimit ? "다음 과업을 누르면 다음 지시문을 읽어 드립니다."
        : `저장했습니다. ${labels[outcome] || "종료"}`);
      el("study-answer-form").hidden = true;
      el("study-saved-box").hidden = false;
    }
    if (current === "answering") {
      setText("study-result-heading", timeLimit ? "제한 시간이 지나 과업이 끝났습니다." : "과업 결과");
      el("study-answer-form").hidden = timeLimit;
      el("study-saved-box").hidden = true;
    }
    checkTimeLimit();
    renderControls();
  }
  function netElapsedSeconds() {
    const session = view.session;
    if (!session?.started_at) return 0;
    const now = Date.now();
    const pausedNow = session.paused_at ? Math.max(0, now - Date.parse(session.paused_at)) : 0;
    // paused_seconds includes time spent in the result form before a return to the task.
    const paused = (session.paused_seconds || 0) * 1000;
    return Math.max(0, Math.floor((now - Date.parse(session.started_at) - paused - pausedNow) / 1000));
  }
  // The limit is enforced but not shown or announced (0.5.0: no time on the participant's screen).
  function checkTimeLimit() {
    const task = currentTask();
    if (!task || view.practice || phase() !== "active") return;
    const elapsed = netElapsedSeconds();
    if (elapsed >= task.time_limit_seconds && !view.session.paused_at && view.timeLimitHandled !== view.session.id && !view.busy) {
      view.timeLimitHandled = view.session.id;
      logStudyEvent("time_limit_reached", { net_task_seconds: elapsed });
      busy(() => endTask("time_limit"));
    }
  }

  // ---- preparation (automatic; no facilitator) -----------------------------------
  // Resolves true on the frame's load event, false when it did not come within the timeout.
  function frameLoaded(timeout = 15000) {
    return new Promise(resolve => {
      const done = loaded => { clearTimeout(timer); el("study-frame").removeEventListener("load", onLoad); resolve(loaded); };
      const onLoad = () => done(true);
      const timer = setTimeout(() => done(false), timeout);
      el("study-frame").addEventListener("load", onLoad);
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
  // The instruction is shown and read at once; the site loads behind it (0.5.0), and 과업 시작
  // waits for it if pressed early, so the participant never sits through a loading message.
  async function prepare(retry = false) {
    const endpoint = retry ? `/study/${view.tester}/retry` : `/study/${view.tester}/prepare`;
    const data = await api(endpoint, "POST", retry ? { session_id: view.session.id } : {});
    view.session = data.session;
    view.ready = false;
    view.siteError = false;
    resetForms();
    await refreshPlan();
    render();
    if (view.session.status !== "draft") return null;
    const sessionId = view.session.id;
    const token = ++view.loadToken;
    const loading = view.siteLoading = loadSite(sessionId, retry, token)
      // 과업 시작 waiting on this load reports the result itself (the sentence was read twice).
      .catch(error => { if (token === view.loadToken && !view.startWaiting) message(error.message, true); })
      .finally(() => { if (view.siteLoading === loading) view.siteLoading = null; });
    return presentInstruction();
  }
  async function reloadSite() {
    const token = ++view.loadToken;
    const loading = view.siteLoading = loadSite(view.session.id, true, token);
    try { await loading; } catch { /* start() reports the cause */ } finally { if (view.siteLoading === loading) view.siteLoading = null; }
  }
  async function loadSite(sessionId, retry, token) {
    const current = () => token === view.loadToken && view.session?.id === sessionId && !view.practice;
    const began = view.prepBegan = Date.now();
    const diagnosis = { login: "" };
    view.siteCause = "";
    message("배정된 사이트를 준비하고 있습니다.");
    const loaded = frameLoaded(10000);
    el("study-frame").src = view.session.target_url;
    view.opened = true;
    // A page that had not loaded after 10 s stayed in "loading" for the whole 35 s, while
    // opening it again took about 10 s (Windows 10/28 and 10/29). It is opened again once.
    if (!(await loaded) && current()) {
      diagnosis.reopened = true;
      const again = frameLoaded(12000);
      el("study-frame").src = view.session.target_url;
      await again;
    }
    if (!current()) return;
    let result;
    try {
      result = await waitForSite(sessionId);
      // Sites open without any access code (10/28). A login page is a site error, never a prompt.
    } catch (error) {
      if (!current()) return;
      view.siteCause = await reportPreparation(sessionId, began, retry, { reason: error.reason || "unknown", ...diagnosis });
      if (error.message === SITE_UNREACHABLE) error.message = siteUnreachable(view.siteCause);
      throw error;
    }
    if (!result || !current()) return;
    view.siteError = !result.ready;
    view.siteCause = await reportPreparation(sessionId, began, retry, { ...diagnosis, ready: result.ready, reason: result.reason,
      net_error: result.net_error, stage: result.stage });
    if (!current()) return;
    applyPreparation(result);
    if (!result.ready) throw new Error(siteUnreachable(view.siteCause));
  }
  // One site_preparation event per preparation; the recorder files a failure under a cause
  // (로그인 필요, 주소 허용 안 됨, 준비 시간 초과, 확장 응답 없음, ...) and returns its label.
  async function reportPreparation(sessionId, began, retry, detail) {
    const payload = { result: detail.ready ? "ready" : "failed", reason: detail.ready ? "" : detail.reason || "unknown",
      login: detail.login || "", net_error: detail.net_error || "", stage: detail.stage || "", reopened: Boolean(detail.reopened),
      elapsed_ms: Date.now() - began, retry: Boolean(retry) };
    const response = await api(`/sessions/${sessionId}/study-events`, "POST", { type: "site_preparation", payload }).catch(() => null);
    return response?.event?.payload?.cause_label || "";
  }
  function applyPreparation(result) {
    const wasReady = view.ready;
    view.ready = Boolean(result.ready);
    if (result.frame_title) view.frameTitle = result.frame_title;
    if (view.ready) view.siteError = false;
    // A login page means the kit's code is missing or refused; the participant is never asked for it.
    if (!view.ready && (view.siteError || result.reason === "login_required")) message(siteUnreachable(view.siteCause), true);
    else message(result.ready ? "사이트가 준비되었습니다." : "사이트 준비를 확인하고 있습니다.");
    renderControls();
    return !wasReady && view.ready;
  }
  async function checkSite() {
    const sessionId = view.session.id;
    const hadError = view.siteError;
    const result = await bridge("check");
    if (view.session?.id !== sessionId || view.session.status !== "draft" || view.siteLoading) return;
    // Became ready in the background (e.g. the site came back). The instruction was read already;
    // only a participant who heard the error is told.
    if (applyPreparation(result)) {
      view.siteCause = "";
      reportPreparation(sessionId, view.prepBegan || Date.now(), false, { ready: true, stage: "background_check" });
      if (hadError) announce("사이트가 준비되었습니다.");
    }
  }
  // Focus the instruction so NVDA reads it (announce mode "focus"); "과업 시작" follows on Tab.
  function presentInstruction() {
    logStudyEvent("instruction_presented", { method: "focus" });
    return el("study-instruction");
  }

  // ---- task actions ---------------------------------------------------------------
  async function start() {
    if (!view.ready && view.siteLoading) {
      view.startWaiting = true;
      try { await view.siteLoading; } finally { view.startWaiting = false; }
    }
    // A preparation that failed is tried again here; before 0.5.2 only F5 recovered (17-1).
    if (!view.ready) { alertAgain("사이트를 다시 여는 중입니다."); await reloadSite(); }
    if (!view.ready) throw new Error(siteUnreachable(view.siteCause));
    await checkConnection();
    if (!state.nvdaConnected) throw new Error("NVDA가 연결되지 않아 과업을 시작하지 않았습니다. 연구진에게 알려 주세요.");
    const result = await bridge("start");
    if (!result.started) throw new Error("사이트가 아직 준비되지 않았습니다. 잠시 뒤 다시 눌러 주세요.");
    if (result.frame_title) view.frameTitle = result.frame_title;
    view.session = (await api(`/sessions/${view.session.id}`)).session;
    view.siteStartKey = null;
    await refreshPlan();
    render();
    message("기록 중입니다.");
    requestAnimationFrame(() => focusSite());
    return el("study-frame");
  }
  // After busy() focuses the frame, move focus to the site's first control as on a newly opened
  // page, so NVDA's cursor starts inside the site (Windows check F3, 10/20). The practice page is
  // same-origin; a copy is asked through the extension's content script in the frame.
  // It also learns the history entry the task started on (back stops there); a copy replies
  // with "a11y-study-site-entry".
  // edge "last" (Shift+Tab out of the top of the site) focuses its last control instead, and
  // "resume" (과업으로 돌아가기) the control the task was ended on (Windows 10/28, 15).
  function focusSite(edge = "first") {
    const frame = el("study-frame");
    if (phase() !== "active") return;
    const focus = document.activeElement === frame;
    const doc = frame.contentDocument;
    if (doc) {
      view.siteStartKey ??= frame.contentWindow.navigation?.currentEntry?.key || null;
      if (focus) {
        const list = controls(doc);
        const kept = edge === "resume" && view.siteFocus?.isConnected && view.siteFocus.ownerDocument === doc ? view.siteFocus : null;
        (kept || (edge === "last" ? list.at(-1) : list[0]))?.focus();
      }
    } else frame.contentWindow?.postMessage({ kind: "a11y-study-focus-site", focus, edge }, "*");
  }
  function controls(doc) {
    const selector = 'a[href], button, input:not([type="hidden"]), select, textarea, summary, [tabindex]:not([tabindex="-1"])';
    return [...doc.querySelectorAll(selector)].filter(node => !node.disabled && node.getClientRects().length > 0 && !node.closest("[inert], [aria-hidden='true']"));
  }
  // ---- 초점 가두기 (0.5.0) --------------------------------------------------------------
  // Participants lost the site when Tab ran past its last control into the Chrome toolbar, and
  // heard the recorder page read when focus fell back onto it. During a task the two empty
  // traps around the frame send Tab/Shift+Tab back into the site, and focus that lands anywhere
  // else on this page (not the mouse-only controls below the site) is returned to the site.
  // Only NVDA+Ctrl+E (or the time limit) leaves the site.
  function trap(edge) {
    if (phase() !== "active") return;
    el("study-frame").focus();
    focusSite(edge);
  }
  // Outside a task Tab ran from the first or last control into the Chrome info bar (Windows
  // 10/28). Two traps at the ends of this view wrap it around.
  function tabbables() {
    return [...el("study-view").querySelectorAll("button, input, select, textarea, a[href], [tabindex]")]
      .filter(node => node.tabIndex >= 0 && !node.disabled && !node.classList.contains("focus-trap")
        && node.getClientRects().length > 0 && !node.closest("[inert], [aria-hidden='true'], [hidden]"));
  }
  function onFocusIn(event) {
    if (el("study-view").hidden) return;
    const target = event.target;
    if (phase() !== "active") {
      if (target === el("study-page-start")) tabbables().at(-1)?.focus();
      else if (target === el("study-page-end")) tabbables()[0]?.focus();
      return;
    }
    if (target === el("study-trap-start")) trap("last");
    else if (target === el("study-trap-end")) trap("first");
    else if (target !== el("study-frame") && !el("study-stage").contains(target)) trap("first");
  }

  // ---- 이전 페이지 / 과업 처음 화면 (Windows pilot 10/28) ---------------------------------
  // The site runs in a frame, so the browser's Back walks the whole tab history (earlier
  // tasks, the login page, the recorder) and Alt+Home replaces the recorder. Alt+Left and
  // Alt+Home, wherever they land (the copy through the extension, the practice page, this
  // page) and the two buttons below the site, go back inside the frame no further than the
  // page the task started on, or reopen that page. Recorded as site_navigation events.
  function siteKey(event) {
    if (event.ctrlKey || event.metaKey || event.shiftKey) return "";
    if ((event.altKey && event.key === "ArrowLeft") || event.key === "BrowserBack") return "back";
    if ((event.altKey && event.key === "Home") || event.key === "BrowserHome") return "start";
    return "";
  }
  function historyBack(nav, startKey) {
    const start = nav?.entries().find(entry => entry.key === startKey);
    if (!start || nav.currentEntry.index <= start.index) return false;
    const { committed, finished } = nav.back();
    committed.catch(() => {});
    finished.catch(() => {});
    return true;
  }
  function onSiteKey(event, trigger) {
    const action = siteKey(event);
    if (!action || el("study-view").hidden) return;
    // Outside a task the keys do nothing: the browser's back or home page would leave the recorder.
    event.preventDefault();
    siteCommand(action, trigger);
  }
  function siteCommand(action, trigger) {
    if (view.busy || phase() !== "active") return;
    const frame = el("study-frame");
    // After the page changes, focus goes to its first control as at the task start (F3).
    view.refocusUntil = Date.now() + 5000;
    frame.focus();
    if (action === "start") {
      frame.src = view.practice ? "/practice.html" : view.session.target_url;
      siteCommandDone(action, trigger, true);
    } else if (frame.contentDocument) siteCommandDone(action, trigger, historyBack(frame.contentWindow.navigation, view.siteStartKey));
    else {
      const pending = view.pendingBack = { trigger };
      frame.contentWindow?.postMessage({ kind: "a11y-study-site-back", startKey: view.siteStartKey }, "*");
      setTimeout(() => { if (view.pendingBack === pending) { view.pendingBack = null; siteCommandDone(action, trigger, false, "no_reply"); } }, 1500);
    }
  }
  function siteCommandDone(action, trigger, moved, reason = "") {
    if (!moved) {
      view.refocusUntil = 0;
      announce("더 돌아갈 페이지가 없습니다.");
    }
    if (!view.practice) logStudyEvent("site_navigation", { action, trigger, moved, ...(reason ? { reason } : {}) });
  }
  // #study-site-status stays outside the shelved top, so NVDA hears it during a task. It is
  // emptied again so browse mode does not meet the old sentence below the site.
  function announce(text) {
    const node = el("study-site-status");
    node.textContent = "";
    setTimeout(() => { node.textContent = text; }, 50);
    setTimeout(() => { if (node.textContent === text) node.textContent = ""; }, 5000);
  }
  function onFrameMessage(event) {
    const frame = el("study-frame");
    if (!frame || event.source !== frame.contentWindow || typeof event.data?.kind !== "string") return;
    const data = event.data;
    if (data.kind === "a11y-study-site-key" && ["back", "start"].includes(data.action)) siteCommand(data.action, "site_key");
    else if (data.kind === "a11y-study-site-entry" && phase() === "active") view.siteStartKey ??= (data.key || null);
    else if (data.kind === "a11y-study-site-back" && view.pendingBack) {
      const { trigger } = view.pendingBack;
      view.pendingBack = null;
      siteCommandDone("back", trigger, data.moved === true);
    }
  }
  function onFrameLoad() {
    const frame = el("study-frame");
    // The practice page shares this origin and has no content script: listen to its keys here.
    try {
      frame.contentDocument?.addEventListener("keydown", event => onSiteKey(event, "site_key"), true);
      frame.contentDocument?.addEventListener("focusin", event => { view.siteFocus = event.target; }, true);
    } catch { /* cross-origin: the content script keeps its own last focus */ }
    if (Date.now() < view.refocusUntil) { view.refocusUntil = 0; requestAnimationFrame(() => focusSite()); }
  }

  async function endTask(reason) {
    const data = await api(`/sessions/${view.session.id}/end-task`, "POST", { reason });
    view.session = data.session;
    await refreshPlan();
    render();
    if (reason === "time_limit") return saveTimeLimit();
    message("과업이 끝났습니다. 결과를 입력해 주세요.");
    return el("study-result-heading");
  }
  // Time limit: nobody is asked; the result is saved as timeout with an empty answer. This is the
  // one end that stops on a screen of its own (다음 과업), so the participant knows why it ended.
  async function saveTimeLimit() {
    const target = await finish("timeout", "", "system", false);
    return view.plan.finished === view.plan.total ? target : el("study-next");
  }
  // A saved result goes straight on to the next instruction (0.5.0); there is no saved screen.
  async function finish(outcome, answer, enteredBy, advance = true) {
    if (!outcome) throw new Error("성공인지 실패인지 골라 주세요.");
    message("결과와 기록을 저장하고 있습니다.");
    const data = await api(`/sessions/${view.session.id}/stop`, "POST", {
      status: outcome === "completed" ? "completed" : "abandoned", study_outcome: outcome,
      answer, entered_by: enteredBy,
    });
    view.session = data.session;
    try { await bridge("close"); } catch { /* Saved result remains valid when the extension has disconnected. */ }
    view.opened = false;
    await refreshPlan();
    message("결과와 기록을 저장했습니다.");
    // The result form stays on screen (and focus on 저장) until the next instruction replaces it.
    if (advance && view.plan.finished < view.plan.total) return prepare();
    render();
    return view.plan.finished === view.plan.total ? el("study-complete").querySelector("h2") : el("study-saved");
  }
  async function submitParticipant() {
    const outcome = document.querySelector('input[name="study-done"]:checked')?.value;
    if (!outcome) throw new Error("성공인지 실패인지 골라 주세요.");
    return finish(outcome, el("study-answer").value, "participant");
  }
  // 다음 과업. A legacy "technical" result is re-prepared instead of skipped.
  function nextTask() { return prepare(view.session?.study_outcome === "technical"); }

  // 과업 끝내기 button: ask first (focus on 계속하기); the E gesture and the time limit end without asking.
  // Every confirm step is in the page (no window.confirm) and opens with focus on the safe choice.
  function openConfirm(name, focusId) { view.confirm = name; renderControls(); return el(focusId); }
  function closeConfirm(focusId) { view.confirm = null; renderControls(); return el(focusId); }

  // ---- 되돌리기 (0.4.4) -------------------------------------------------------------
  // 과업 다시 시작 / 처음부터 다시 하기: the attempt is kept as aborted (excluded from analysis)
  // and the same task is prepared again as a new attempt; focus goes to its instruction.
  async function restartTask(trigger) {
    await api(`/sessions/${view.session.id}/abort`, "POST", { reason: "restart", trigger, by: "participant" });
    view.confirm = null;
    view.frameTitle = "";
    const target = await prepare();
    if (target) message("과업을 처음부터 다시 합니다.");
    return target;
  }
  // 과업으로 돌아가기: the end was pressed by mistake. Recording resumes on the same page; the
  // time in the result form is not counted (server paused_seconds).
  async function returnToTask() {
    const data = await api(`/sessions/${view.session.id}/resume-task`, "POST", { by: "participant" });
    view.session = data.session;
    resetForms();
    await refreshPlan();
    render();
    // An alert left from the result form ("성공인지 실패인지 골라 주세요.") was read again with the
    // whole site on the way back (Windows 10/29, 1 of 1); it is cleared first.
    message("");
    announce("과업으로 돌아왔습니다.");
    requestAnimationFrame(resumeSite);
    return null;
  }
  // 과업으로 돌아가기 (0.5.3): focus goes straight to the control the task was ended on. Focusing
  // the frame first made NVDA read the page title and address before it (Windows 10/29, 15).
  function resumeSite() {
    const frame = el("study-frame");
    if (phase() !== "active") return;
    const doc = frame.contentDocument;
    if (doc) {
      const kept = view.siteFocus?.isConnected && view.siteFocus.ownerDocument === doc ? view.siteFocus : null;
      if (kept) kept.focus(); else { frame.focus(); focusSite(); }
      return;
    }
    frame.contentWindow?.postMessage({ kind: "a11y-study-focus-site", focus: true, edge: "resume" }, "*");
    // A copy that could not take focus by itself gets it through the frame.
    setTimeout(() => { if (phase() === "active" && document.activeElement !== frame) { frame.focus(); focusSite("resume"); } }, 250);
  }
  // 테스터 번호 바꾸기: free while nothing is saved for this number; otherwise ask first.
  function beginTesterChange() {
    const saved = view.plan?.finished || 0;
    if (!saved) return showTesterPicker();
    setText("study-tester-question", `${view.tester}로 저장된 결과 ${saved}개는 지워지지 않고 그대로 남습니다. 다른 테스터 번호로 바꿀까요?`);
    return openConfirm("tester", "study-tester-no");
  }
  function showTesterPicker() {
    view.confirm = null;
    view.changeTester = true;
    renderControls();
    el("study-tester").value = "";
    message("바꿀 테스터 번호를 입력한 뒤 시작을 눌러 주세요.");
    return el("study-tester");
  }
  function keepTester() {
    view.changeTester = false;
    el("study-tester").value = view.tester;
    renderControls();
    message(`테스터 번호 ${view.tester}를 그대로 씁니다.`);
    return el("study-tester-change");
  }

  // ---- practice (대본 §4.4; nothing is recorded) -------------------------------------
  // 0.5.0: the one place the gestures are taught. It opens by itself the first time a tester
  // starts, can be opened again (연습 다시 하기) before any task, and at its end says whether the
  // participant has tried NVDA+Ctrl+H and ended with NVDA+Ctrl+E.
  async function startPractice() {
    await api("/signals/practice", "POST", { on: true });
    view.loadToken += 1;
    view.siteLoading = null;
    view.practice = { phase: "ready", repeated: false, endedBy: "" };
    view.confirm = null;
    resetForms();
    el("study-frame").src = "/practice.html";
    view.frameTitle = "";
    renderPractice();
    message("연습입니다. 연습은 기록하지 않습니다.");
    return el("study-instruction");
  }
  // 본 과업으로 가기: leave practice and present the current task (the completion screen when
  // every task is done).
  async function stopPractice() {
    await api("/signals/practice", "POST", { on: false });
    markPracticed();
    view.practice = null;
    el("study-frame").src = "about:blank";
    message("연습을 끝냈습니다.");
    if (view.plan.finished === view.plan.total) { resetForms(); render(); return el("study-complete").querySelector("h2"); }
    // prepare() resets the forms after its request. Hiding the practice result first dropped
    // focus onto the page while it waited and NVDA read "연습 결과 영역, 테스터 번호 바꾸기"
    // before the next instruction (Windows 10/28, 17).
    return prepare();
  }
  function renderPractice() {
    const current = view.practice.phase;
    el("study-goal").hidden = false;
    el("study-complete").hidden = true;
    setText("study-order", "연습");
    setText("study-instruction", PRACTICE_GUIDE);
    setText("study-answer-label", "답");
    setText("study-result-heading", current === "ended" ? "연습 결과" : "과업 결과");
    el("study-answer-form").hidden = current !== "answering";
    el("study-saved-box").hidden = current !== "ended";
    view.ready = true;
    renderControls();
  }
  function practiceSignal(signal) {
    if (signal.kind === "repeat") view.practice.repeated = true;
    else if (signal.kind === "start" && view.practice.phase === "ready") practiceStart().focus();
    else if (signal.kind === "end" && view.practice.phase === "active") practiceEnd("gesture").focus();
  }
  function practiceStart() { view.practice.phase = "active"; view.siteStartKey = null; renderPractice(); message("연습 중입니다."); requestAnimationFrame(() => focusSite()); return el("study-frame"); }
  function practiceEnd(by) { view.practice.phase = "answering"; view.practice.endedBy = by; renderPractice(); message("연습 과업이 끝났습니다."); return el("study-result-heading"); }
  function practiceReturn() { message(""); view.practice.phase = "active"; renderPractice(); announce("과업으로 돌아왔습니다."); requestAnimationFrame(resumeSite); return null; }
  function practiceRestart() {
    view.practice = { phase: "ready", repeated: false, endedBy: "" };
    view.confirm = null;
    resetForms();
    el("study-frame").src = "/practice.html";
    renderPractice();
    message("연습을 처음부터 다시 합니다.");
    return el("study-instruction");
  }
  function practiceSubmit() {
    const outcome = document.querySelector('input[name="study-done"]:checked')?.value;
    if (!outcome) throw new Error("성공인지 실패인지 골라 주세요.");
    const { repeated, endedBy } = view.practice;
    const checks = [
      repeated ? "" : "NVDA, Ctrl, H를 아직 눌러 보지 않았습니다.",
      endedBy === "gesture" ? "" : "본 과업은 NVDA, Ctrl, E로 끝냅니다.",
    ].filter(Boolean).join(" ");
    view.practice.phase = "ended";
    setText("study-saved", `연습을 마쳤습니다. ${checks ? `${checks} ` : "단축키를 모두 써 보셨습니다. "}`
      + "본 과업으로 가려면 '본 과업으로 가기', 다시 연습하려면 '연습 다시 하기'를 누르세요. 연습은 지시문 화면과 모든 과업을 마친 화면에서 다시 할 수 있습니다.");
    renderPractice();
    return el("study-saved");
  }

  document.addEventListener("DOMContentLoaded", () => {
    const on = (id, event, handler) => el(id).addEventListener(event, handler);
    on("study-tester-form", "submit", e => { e.preventDefault(); busy(() => confirmTester(el("study-tester").value)); });
    // 0.5.2: a press while the previous one is still running (a site being prepared again) is
    // kept and run when it ends; before, it was dropped and nothing was heard (Windows 10/28, 17-1).
    on("study-start", "click", () => {
      if (view.busy && !view.practice) { view.startPending = true; return; }
      busy(view.practice ? async () => practiceStart() : start);
    });
    const escape = (id, focusId) => on(id, "keydown", e => { if (e.key === "Escape" && !view.busy) { e.preventDefault(); e.stopPropagation(); closeConfirm(focusId).focus(); } });
    on("study-end", "click", () => { if (!view.busy && phase() === "active") openConfirm("end", "study-end-no").focus(); });
    on("study-end-yes", "click", () => busy(view.practice ? async () => practiceEnd("button") : () => endTask("participant_button")));
    on("study-end-no", "click", () => closeConfirm("study-frame").focus());
    escape("study-end-confirm", "study-frame");
    on("study-restart", "click", () => { if (!view.busy && phase() === "active") openConfirm("restart", "study-restart-no").focus(); });
    on("study-restart-yes", "click", () => busy(view.practice ? async () => practiceRestart() : () => restartTask("button")));
    on("study-restart-no", "click", () => closeConfirm("study-frame").focus());
    escape("study-restart-confirm", "study-frame");
    on("study-return", "click", () => busy(view.practice ? async () => practiceReturn() : returnToTask));
    on("study-redo", "click", () => { if (!view.busy && phase() === "answering") openConfirm("redo", "study-redo-no").focus(); });
    on("study-redo-yes", "click", () => busy(view.practice ? async () => practiceRestart() : () => restartTask("result_form")));
    on("study-redo-no", "click", () => closeConfirm("study-redo").focus());
    escape("study-redo-confirm", "study-redo");
    on("study-tester-change", "click", () => { if (!view.busy) beginTesterChange().focus(); });
    on("study-tester-yes", "click", () => showTesterPicker().focus());
    on("study-tester-no", "click", () => closeConfirm("study-tester-change").focus());
    escape("study-tester-confirm", "study-tester-change");
    on("study-tester-keep", "click", () => { if (!view.busy) keepTester().focus(); });
    on("study-next", "click", () => busy(nextTask));
    on("study-answer-form", "submit", e => { e.preventDefault(); busy(view.practice ? async () => practiceSubmit() : submitParticipant); });
    on("study-practice", "click", () => busy(startPractice));
    on("study-practice-complete", "click", () => busy(startPractice));
    on("study-practice-again", "click", () => busy(async () => practiceRestart()));
    on("study-practice-stop", "click", () => busy(stopPractice));
    on("study-practice-skip", "click", () => busy(stopPractice));
    on("study-site-back", "click", () => siteCommand("back", "button"));
    on("study-site-start", "click", () => siteCommand("start", "button"));
    on("study-frame", "load", onFrameLoad);
    document.addEventListener("keydown", event => onSiteKey(event, "recorder_key"), true);
    document.addEventListener("focusin", onFocusIn);
    window.addEventListener("message", onFrameMessage);
  });
  return { home, pausePolling };
})();
