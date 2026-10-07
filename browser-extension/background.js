const COLLECTOR = "http://127.0.0.1:8765";
const ACTIVE_CACHE_MS = 1200;
const RECORDER_CLIENT = "a11y-recorder-cft-v1";

let activeCache = { checkedAt: 0, session: null };
let previousSession = null;
let environmentReportedSession = null;
const tabHistories = new Map();
const STUDY_TAB_KEY = "a11yStudyOwnedTab";
let studyCommands = Promise.resolve();

function studyFailure(reason, error) {
  return { ok: false, ready: false, reason, ...(error ? { error } : {}) };
}

async function studyRequest(path, options = {}) {
  const response = await fetch(`${COLLECTOR}${path}`, {
    cache: "no-store", ...options,
    headers: { "Content-Type": "application/json", "X-A11y-Recorder-Client": RECORDER_CLIENT },
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `collector ${response.status}`);
  return body;
}

async function studyReadiness(session, state) {
  let tab;
  try { tab = await chrome.tabs.get(state.tabId); }
  catch { return studyFailure("tab_closed"); }
  if (tab.status === "loading" || tab.pendingUrl) return { ok: true, ready: false, reason: "loading" };
  const onAccessPage = tab.url === `${new URL(session.target_url).origin}/access`;
  if (tab.url !== session.target_url && !onAccessPage) return studyFailure("wrong_url");
  let result;
  try {
    result = await chrome.tabs.sendMessage(tab.id, { kind: "a11y-study-page",
      targetUrl: session.target_url, preparation: session.study.preparation || [],
      allowPreparation: session.status === "draft" && !state.prepared,
    });
  } catch { return { ok: true, ready: false, reason: "loading" }; }
  if (!result) return studyFailure("extension_unavailable");
  if (result.reason === "login_required") await chrome.tabs.update(tab.id, { active: true });
  if (onAccessPage && result.reason !== "login_required") return studyFailure("wrong_url");
  if (result.ready) {
    state.prepared = true;
    state.setup = state.setup || result.setup;
    await chrome.storage.session.set({ [STUDY_TAB_KEY]: state });
  }
  return { ok: true, ...result, sessionId: session.id, tabId: tab.id, setup: state.setup || result.setup };
}

async function studyCommand(message, sender) {
  try {
    if (sender.frameId !== 0 || new URL(sender.url || "").origin !== COLLECTOR) {
      return studyFailure("untrusted_origin");
    }
  } catch { return studyFailure("untrusted_origin"); }
  if (!/^[A-Za-z0-9_-]{1,100}$/.test(message.sessionId || "") ||
      !["prepare", "check", "start", "close"].includes(message.action)) return studyFailure("invalid_command");
  try {
    const { session } = await studyRequest(`/api/sessions/${message.sessionId}`);
    if (!session?.study) return studyFailure("not_assigned_task");
    const target = new URL(session.target_url);
    if (target.protocol !== "https:" || target.username || target.password || target.search || target.hash ||
        !/^\/s\/[a-f0-9]{12}\/(?:index\.html|p\/[a-f0-9]{16}\.html)$/.test(target.pathname)) {
      return studyFailure("invalid_assignment_url");
    }
    let state = (await chrome.storage.session.get(STUDY_TAB_KEY))[STUDY_TAB_KEY];
    const { session: active } = await studyRequest("/api/active-session");
    if (active && active.id !== session.id) return studyFailure("active_session_conflict");
    if (active && message.action !== "close") return studyFailure("already_started");
    if (message.action === "close") {
      if (!["completed", "abandoned"].includes(session.status)) return studyFailure("session_not_finished");
      if (state?.sessionId !== session.id) return { ok: true, closed: false };
      try { await chrome.tabs.remove(state.tabId); } catch { /* Already closed. */ }
      await chrome.storage.session.remove(STUDY_TAB_KEY);
      return { ok: true, closed: true, sessionId: session.id };
    }
    if (session.status !== "draft") return studyFailure(session.status === "active" ? "already_started" : "session_finished");
    if (message.action === "prepare") {
      if (state?.sessionId !== session.id && state) {
        const { session: prior } = await studyRequest(`/api/sessions/${state.sessionId}`);
        if (prior?.status === "active") return studyFailure("active_session_conflict");
        try { await chrome.tabs.remove(state.tabId); } catch { /* Already closed. */ }
        state = null;
        await chrome.storage.session.remove(STUDY_TAB_KEY);
      }
      if (state) {
        try { await chrome.tabs.get(state.tabId); }
        catch { state = null; }
      }
      if (state) {
        const readiness = await studyReadiness(session, state);
        if (!["preparation_required", "preparation_failed", "wrong_url", "capture_missing"].includes(readiness.reason)) {
          return readiness;
        }
        const { session: latest } = await studyRequest(`/api/sessions/${session.id}`);
        const { session: recording } = await studyRequest("/api/active-session");
        if (recording || latest.status !== "draft") return studyFailure("active_session_conflict");
        try { await chrome.tabs.remove(state.tabId); } catch { /* Already closed. */ }
        state = null;
        await chrome.storage.session.remove(STUDY_TAB_KEY);
      }
      if (!state) {
        let tab;
        try { tab = await chrome.tabs.create({ url: session.target_url, active: false }); }
        catch { return studyFailure("popup_blocked"); }
        if (!Number.isInteger(tab?.id)) return studyFailure("popup_blocked");
        state = { sessionId: session.id, tabId: tab.id, prepared: false };
        await chrome.storage.session.set({ [STUDY_TAB_KEY]: state });
      }
    }
    if (state?.sessionId !== session.id) return studyFailure("not_prepared");
    const ready = await studyReadiness(session, state);
    if (message.action !== "start" || !ready.ready) return ready;
    const tab = await chrome.tabs.get(state.tabId);
    if (tab.url !== session.target_url || tab.pendingUrl) return studyFailure("wrong_url");
    await studyRequest(`/api/sessions/${session.id}/start`, {
      method: "POST", body: JSON.stringify({ study_ready: true, study_setup: state.setup }),
    });
    await getActiveSession(true);
    await new Promise(resolve => setTimeout(resolve, 1500));
    try { await chrome.tabs.update(state.tabId, { active: true }); }
    catch { return { ...ready, started: true, reason: "task_focus_failed" }; }
    try {
      const focusedTab = await chrome.tabs.get(state.tabId);
      await studyRequest("/api/events", { method: "POST", body: JSON.stringify({
        session_id: session.id, timestamp: new Date().toISOString(), source: "browser",
        type: "page_ready", url: session.target_url, page_title: focusedTab.title || "",
        payload: { kind: "study_recording_start", evidence_kind: "recorder_metadata" },
      }) });
    } catch { return { ...ready, started: true, reason: "start_metadata_failed" }; }
    return { ...ready, started: true };
  } catch (error) { return studyFailure("collector_error", error.message); }
}

function sanitizedUrl(value) {
  if (!value) return "";
  try {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol)) return "";
    return `${url.origin}${url.pathname}`;
  } catch {
    return "";
  }
}

async function getActiveSession(force = false) {
  const now = Date.now();
  if (!force && now - activeCache.checkedAt < ACTIVE_CACHE_MS) {
    return activeCache.session;
  }
  try {
    const response = await fetch(`${COLLECTOR}/api/active-session`, {
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`collector ${response.status}`);
    const data = await response.json();
    if (activeCache.session && activeCache.session.id !== data.session?.id) previousSession = activeCache.session;
    activeCache = { checkedAt: now, session: data.session || null };
  } catch {
    activeCache = { checkedAt: now, session: null };
  }
  updateBadge(activeCache.session);
  return activeCache.session;
}

async function reportBrowserEnvironment(sessionId) {
  const match = navigator.userAgent.match(
    /(Edg|Chrome|Firefox)\/([0-9.]+)/
  );
  const browser = match ? `${match[1]} ${match[2]}` : navigator.userAgent;
  try {
    await fetch(`${COLLECTOR}/api/sessions/${sessionId}`, {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        "X-A11y-Recorder-Client": RECORDER_CLIENT,
      },
      body: JSON.stringify({
        environment_merge: {
          browser,
          browser_extension_version: chrome.runtime.getManifest().version,
        },
      }),
    });
  } catch {
    // 환경 메타데이터는 보조 정보이므로 이벤트 수집은 계속합니다.
  }
}

async function updateBadge(session) {
  try {
    await chrome.action.setBadgeText({ text: session ? "REC" : "" });
    await chrome.action.setBadgeBackgroundColor({ color: "#0f6046" });
    await chrome.action.setTitle({
      title: session
        ? `기록 중: ${session.title}`
        : "A11y Task Recorder — 진행 중인 세션 없음",
    });
  } catch {
    // 확장이 종료되는 순간에는 action API 호출이 실패할 수 있습니다.
  }
}

async function sendEvent(event) {
  const capturedAt = event.timestamp || new Date().toISOString();
  let session = await getActiveSession();
  if ((!session || Date.parse(capturedAt) < Date.parse(session.started_at)) && previousSession) {
    session = previousSession;
  }
  if (!session) return { recorded: false, reason: "no_active_session" };
  if (session.id !== environmentReportedSession) {
    await reportBrowserEnvironment(session.id);
    environmentReportedSession = session.id;
  }
  const payload = {
    session_id: session.id,
    timestamp: capturedAt,
    source: "browser",
    ...event,
  };
  if (payload.url) payload.url = sanitizedUrl(payload.url);
  try {
    const response = await fetch(`${COLLECTOR}/api/events`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-A11y-Recorder-Client": RECORDER_CLIENT,
      },
      body: JSON.stringify(payload),
    });
    if (response.status === 409) {
      activeCache = { checkedAt: 0, session: null };
    }
    return { recorded: response.ok };
  } catch {
    return { recorded: false, reason: "collector_unavailable" };
  }
}

function inferHistoryDirection(tabId, url, qualifiers) {
  const cleanUrl = sanitizedUrl(url);
  const history = tabHistories.get(tabId) || { entries: [], index: -1 };
  const isHistory = qualifiers.includes("forward_back");
  let direction = "new";

  if (isHistory) {
    const previousUrl = history.entries[history.index - 1];
    const nextUrl = history.entries[history.index + 1];
    if (previousUrl === cleanUrl) {
      history.index -= 1;
      direction = "back";
    } else if (nextUrl === cleanUrl) {
      history.index += 1;
      direction = "forward";
    } else {
      const earlierIndex = history.entries.lastIndexOf(cleanUrl, history.index - 1);
      if (earlierIndex >= 0) {
        history.index = earlierIndex;
        direction = "back";
      } else {
        direction = "back_or_forward";
        history.entries = history.entries.slice(0, history.index + 1);
        history.entries.push(cleanUrl);
        history.index = history.entries.length - 1;
      }
    }
  } else if (history.entries[history.index] !== cleanUrl) {
    history.entries = history.entries.slice(0, history.index + 1);
    history.entries.push(cleanUrl);
    history.index = history.entries.length - 1;
  }

  tabHistories.set(tabId, history);
  return direction;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.kind === "a11y-study-command") {
    studyCommands = studyCommands.then(() => studyCommand(message, sender));
    studyCommands.then(sendResponse);
    return true;
  }
  if (message?.kind === "recorder-event") {
    const event = {
      ...message.event,
      url: message.event?.url || sender.tab?.url || "",
    };
    sendEvent(event).then(sendResponse);
    return true;
  }
  if (message?.kind === "recorder-status") {
    getActiveSession(true).then((session) =>
      sendResponse({ connected: Boolean(session), session })
    );
    return true;
  }
  if (message?.kind === "recorder-marker") {
    createMarker(message.label || "브라우저 확장에서 불편 지점 표시").then(sendResponse);
    return true;
  }
  return false;
});

chrome.webNavigation.onCommitted.addListener((details) => {
  if (details.frameId !== 0 || !/^https?:/.test(details.url)) return;
  const qualifiers = details.transitionQualifiers || [];
  const direction = inferHistoryDirection(details.tabId, details.url, qualifiers);
  sendEvent({
    type: "navigation",
    url: details.url,
    payload: {
      direction,
      transition_type: details.transitionType,
      transition_qualifiers: qualifiers,
    },
  });
});

chrome.webNavigation.onHistoryStateUpdated.addListener((details) => {
  if (details.frameId !== 0 || !/^https?:/.test(details.url)) return;
  sendEvent({
    type: "history",
    url: details.url,
    payload: {
      direction: "state_change",
      kind: "history_state_updated",
      transition_type: details.transitionType,
    },
  });
});

chrome.tabs.onRemoved.addListener((tabId) => {
  tabHistories.delete(tabId);
});

chrome.commands.onCommand.addListener((command) => {
  if (command === "mark-issue") {
    createMarker("Alt+Shift+M으로 불편 지점 표시");
  }
});

async function createMarker(label) {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const tab = tabs[0];
  return sendEvent({
    type: "marker",
    url: tab?.url || "",
    page_title: tab?.title || "",
    payload: { label },
  });
}

chrome.runtime.onInstalled.addListener(() => {
  getActiveSession(true);
});

getActiveSession(true);
