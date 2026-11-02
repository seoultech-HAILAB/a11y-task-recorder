const COLLECTOR = "http://127.0.0.1:8765";
const ACTIVE_CACHE_MS = 1200;
const RECORDER_CLIENT = "a11y-recorder-cft-v1";

let activeCache = { checkedAt: 0, session: null };
let previousSession = null;
let environmentReportedSession = null;
const frameHistories = new Map();
// Since 0.4.0 the study copy runs inside the dashboard tab's iframe. The owned
// "tab" is the dashboard tab and the owned frame is that iframe.
const STUDY_TAB_KEY = "a11yStudyOwnedTab";
let studyCommands = Promise.resolve();
let freeLaunch = null;
let freeTabClaims = Promise.resolve();
// Last sub-frame load error per tab ("net::ERR_BLOCKED_BY_CSP", "net::ERR_NAME_NOT_RESOLVED", ...):
// a frame Chrome refused shows chrome-error://, which the readiness check would otherwise
// only see as "still loading" until the dashboard gives up (Windows pilot, 10/28).
const frameErrors = new Map();

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

// Both hosted and partner copies accept HTTP(S); assignments come from the local catalog.
function studyOriginOk(target) {
  return ["http:", "https:"].includes(target.protocol);
}
// Credentials, query and fragment remain forbidden. The frame must show the exact
// assigned URL; hosted /s/ copies additionally require capture metadata (study-page.js).
function assignedUrlOk(value) {
  try {
    const target = new URL(value);
    return studyOriginOk(target) && !target.username && !target.password && !target.search && !target.hash;
  } catch { return false; }
}

// 0.6.0 free task: a site the tester chose, opened in its own window (real sites refuse to be
// framed). Query and fragment are allowed; credentials and non-web schemes are not.
function freeUrlOk(value) {
  try {
    const target = new URL(value);
    return ["http:", "https:"].includes(target.protocol) && !target.username && !target.password &&
      target.origin !== COLLECTOR;
  } catch { return false; }
}
const isFree = session => session?.study?.kind === "free";

async function freeCommand(message, session, state, dashboardTabId) {
  if (!freeUrlOk(session.target_url)) return studyFailure("invalid_assignment_url");
  if (state?.sessionId === session.id && state.tabId !== dashboardTabId) return studyFailure("wrong_dashboard_tab");
  if (message.action === "status") return { ok: true, sessionId: session.id, status: session.status, frame_title: "" };
  if (message.action === "prepare" || message.action === "check") {
    if (session.status !== "draft") return studyFailure("session_finished");
    if (state?.sessionId !== session.id) {
      if (state?.siteTabIds?.length) await closeSiteTabs(state);
      state = { sessionId: session.id, tabId: dashboardTabId, mode: "free", siteTabIds: [], frameId: 0 };
      await saveState(state);
    }
    return { ok: true, ready: true, sessionId: session.id };
  }
  if (state?.sessionId !== session.id || state.mode !== "free") return studyFailure("not_prepared");
  if (message.action === "start") {
    if (session.status !== "draft") return studyFailure("already_started");
    // Recording starts first; first-load events are buffered until the returned window identity
    // confirms which new tabs belong to this task. The window opens on the site itself: opened
    // blank and then navigated, focus stayed in the address bar and the page was three F6 presses
    // away (Windows 11/02, 0.6.0). A normal window, not a popup: NVDA read an extension popup's
    // name as "_crx_<extension id>" (0.6.1).
    await studyRequest(`/api/sessions/${session.id}/start`, { method: "POST", body: JSON.stringify({ study_ready: true }) });
    await getActiveSession(true);
    state.awaitingSite = true;
    state.siteTabIds = [];
    await saveState(state);
    // Creation callbacks can arrive before windows.create resolves. Keep a bounded,
    // volatile buffer; never send an unverified new window's events to the collector.
    const launch = { sessionId: session.id, tabs: new Map(), events: [], bytes: 0, dropped: new Map() };
    freeLaunch = launch;
    let tabId, createdWindowId;
    try {
      const win = await chrome.windows.create({ url: session.target_url, focused: true, type: "normal", state: "maximized" });
      createdWindowId = win?.id;
      await freeTabClaims;
      if (!Number.isInteger(win?.id)) throw new Error("free task window missing");
      const owned = new Set((win.tabs || []).map(tab => tab.id).filter(Number.isInteger));
      for (const tab of launch.tabs.values()) if (tab.windowId === win.id) owned.add(tab.id);
      // A site can open another tab while the initial window is still loading.
      for (let pass = 0; pass < launch.tabs.size; pass++) {
        for (const tab of launch.tabs.values()) if (owned.has(tab.openerTabId)) owned.add(tab.id);
      }
      if (!owned.size) throw new Error("free task tab missing");
      tabId = win.tabs?.[0]?.id || [...owned][0];
      state = await loadState();
      if (state?.sessionId !== session.id) throw new Error("free task changed during launch");
      state.siteTabIds = [...owned];
      state.siteWindowId = win.id;
      delete state.awaitingSite;
      await saveState(state);
      freeLaunch = null;
      for (const item of launch.events) {
        if (owned.has(item.tabId)) await sendEvent(item.event, item.tabId, item.frameId, session.id);
      }
      const dropped = [...launch.dropped].reduce((n, [id, count]) => n + (owned.has(id) ? count : 0), 0);
      if (dropped) await sendEvent({ type: "marker", payload: {
        kind: "free_launch_buffer_overflow", evidence_kind: "recorder_metadata", dropped_events: dropped,
      } }, tabId, 0, session.id);
    } catch {
      freeLaunch = null;
      if (Number.isInteger(createdWindowId)) {
        try { await chrome.windows.remove(createdWindowId); } catch { /* Window already gone. */ }
      }
      // Opening the window is a recorder failure, never a participant's blocked task.
      try {
        await studyRequest(`/api/sessions/${session.id}/stop`, { method: "POST", body: JSON.stringify({
          status: "abandoned", study_outcome: "technical", entered_by: "system", answer: "",
          notes: "free_window_open_failed",
        }) });
        await getActiveSession(true);
        await saveState(null);
      } catch { /* The dashboard recovers the still-active task and offers another end attempt. */ }
      return studyFailure("free_window_open_failed");
    }
    try {
      await studyRequest("/api/events", { method: "POST", body: JSON.stringify({
        session_id: session.id, timestamp: new Date().toISOString(), source: "browser",
        type: "page_ready", url: session.target_url, page_title: "",
        payload: { kind: "study_recording_start", evidence_kind: "recorder_metadata",
          ui_mode: "free_tab", tab_id: tabId, frame_id: 0 },
      }) });
    } catch { return { ok: true, ready: true, started: true, reason: "start_metadata_failed" }; }
    return { ok: true, ready: true, started: true };
  }
  // "leave": the task has ended; the site window closes and the dashboard comes back for the result.
  // "close": the result is saved.
  if (message.action === "leave" || message.action === "close") {
    if (message.action === "leave" && session.status !== "answering") return studyFailure("session_not_finished");
    if (message.action === "close" && !["completed", "abandoned"].includes(session.status)) return studyFailure("session_not_finished");
    await closeSiteTabs(state);
    if (message.action === "close") await saveState(null);
    else { state.siteTabIds = []; await saveState(state); }
    try {
      const dashboard = await chrome.tabs.get(state.tabId);
      await chrome.windows.update(dashboard.windowId, { focused: true });
      await chrome.tabs.update(state.tabId, { active: true });
    } catch { /* the dashboard window is gone */ }
    return { ok: true, closed: true, sessionId: session.id };
  }
  return studyFailure("invalid_command");
}

async function closeSiteTabs(state) {
  if (Number.isInteger(state.siteWindowId)) {
    try { await chrome.windows.remove(state.siteWindowId); } catch { /* Already closed or unavailable in older runtimes. */ }
  }
  for (const tabId of state.siteTabIds || []) {
    try { await chrome.tabs.remove(tabId); } catch { /* already closed */ }
  }
}

function sameOrigin(a, b) {
  try { return new URL(a).origin === new URL(b).origin; } catch { return false; }
}

async function loadState() {
  return (await chrome.storage.session.get(STUDY_TAB_KEY))[STUDY_TAB_KEY] || null;
}
async function saveState(state) {
  if (state) await chrome.storage.session.set({ [STUDY_TAB_KEY]: state });
  else await chrome.storage.session.remove(STUDY_TAB_KEY);
}

// Find the dashboard tab's sub-frame that shows the assigned copy (or its login page).
async function findStudyFrame(tabId, targetUrl) {
  let frames;
  try { frames = await chrome.webNavigation.getAllFrames({ tabId }); } catch { return null; }
  const origin = new URL(targetUrl).origin;
  const children = (frames || []).filter(frame => frame.frameId !== 0 && frame.parentFrameId === 0);
  const candidates = children.filter(frame =>
    frame.url === targetUrl || frame.url === `${origin}/access` || frame.url.startsWith(`${origin}/`));
  const frame = candidates.find(frame => frame.url === targetUrl) || candidates[0] || null;
  if (frame) return frame;
  // A single web frame on another origin is a wrong-URL situation, not "still loading".
  const foreign = children.find(frame => /^https?:/.test(frame.url) && !frame.url.startsWith(`${COLLECTOR}/`) && frame.url !== "about:blank");
  return foreign ? { frameId: foreign.frameId, url: foreign.url, foreign: true } : null;
}

// The copy's frame failed to load (an error page has no content script to answer).
function frameFailure(state, session) {
  const failed = frameErrors.get(state.tabId);
  if (!failed || !sameOrigin(failed.url, session.target_url)) return null;
  return { ok: true, ready: false, reason: "frame_error", net_error: failed.error };
}

async function studyReadiness(session, state) {
  try { await chrome.tabs.get(state.tabId); }
  catch { return studyFailure("tab_closed"); }
  const frame = await findStudyFrame(state.tabId, session.target_url);
  if (!frame) return frameFailure(state, session) || { ok: true, ready: false, reason: "loading", stage: "frame" };
  if (frame.foreign) return studyFailure("wrong_url");
  state.frameId = frame.frameId;
  await saveState(state);
  const onAccessPage = frame.url === `${new URL(session.target_url).origin}/access`;
  if (frame.url !== session.target_url && !onAccessPage) return studyFailure("wrong_url");
  let result;
  try {
    result = await chrome.tabs.sendMessage(state.tabId, { kind: "a11y-study-page",
      targetUrl: session.target_url, preparation: session.study.preparation || [],
      allowPreparation: session.status === "draft" && !state.prepared,
    }, { frameId: frame.frameId });
  } catch { return frameFailure(state, session) || { ok: true, ready: false, reason: "loading", stage: "content" }; }
  if (!result) return studyFailure("extension_unavailable");
  if (onAccessPage && result.reason !== "login_required") return studyFailure("wrong_url");
  if (result.ready) {
    state.prepared = true;
    state.setup = state.setup || result.setup;
    await saveState(state);
  }
  return { ok: true, ...result, sessionId: session.id, tabId: state.tabId, frameId: frame.frameId,
    setup: state.setup || result.setup, frame_title: state.frameTitle || "" };
}

async function studyCommand(message, sender) {
  try {
    if (sender.frameId !== 0 || new URL(sender.url || "").origin !== COLLECTOR) {
      return studyFailure("untrusted_origin");
    }
  } catch { return studyFailure("untrusted_origin"); }
  if (!/^[A-Za-z0-9_-]{1,100}$/.test(message.sessionId || "") ||
      !["prepare", "check", "start", "close", "status", "leave"].includes(message.action)) return studyFailure("invalid_command");
  const dashboardTabId = sender.tab?.id;
  if (!Number.isInteger(dashboardTabId)) return studyFailure("untrusted_origin");
  try {
    const { session } = await studyRequest(`/api/sessions/${message.sessionId}`);
    if (!session?.study) return studyFailure("not_assigned_task");
    let state = await loadState();
    if (isFree(session)) {
      const { session: active } = await studyRequest("/api/active-session");
      if (active && active.id !== session.id) return studyFailure("active_session_conflict");
      return await freeCommand(message, session, state, dashboardTabId);
    }
    if (message.action === "leave") return studyFailure("invalid_command");
    if (!assignedUrlOk(session.target_url)) return studyFailure("invalid_assignment_url");
    if (message.action === "status") {
      return { ok: true, sessionId: session.id, status: session.status,
        frame_title: state?.sessionId === session.id ? state.frameTitle || "" : "" };
    }
    const { session: active } = await studyRequest("/api/active-session");
    if (active && active.id !== session.id) return studyFailure("active_session_conflict");
    if (active && message.action !== "close") return studyFailure("already_started");
    if (message.action === "close") {
      if (!["completed", "abandoned"].includes(session.status)) return studyFailure("session_not_finished");
      if (state?.sessionId !== session.id) return { ok: true, closed: false };
      await saveState(null);
      return { ok: true, closed: true, sessionId: session.id };
    }
    if (session.status !== "draft") return studyFailure(session.status === "active" ? "already_started" : "session_finished");
    if (message.action === "prepare") {
      if (state && state.sessionId !== session.id) {
        const { session: prior } = await studyRequest(`/api/sessions/${state.sessionId}`);
        if (prior?.status === "active") return studyFailure("active_session_conflict");
        state = null;
      }
      if (!state) {
        state = { sessionId: session.id, tabId: dashboardTabId, frameId: null, prepared: false, frameTitle: "" };
        await saveState(state);
      }
    }
    if (state?.sessionId !== session.id) return studyFailure("not_prepared");
    if (state.tabId !== dashboardTabId) return studyFailure("wrong_dashboard_tab");
    const ready = await studyReadiness(session, state);
    if (message.action !== "start" || !ready.ready) return ready;
    const frame = await findStudyFrame(state.tabId, session.target_url);
    if (!frame || frame.foreign || frame.url !== session.target_url) return studyFailure("wrong_url");
    await studyRequest(`/api/sessions/${session.id}/start`, {
      method: "POST", body: JSON.stringify({ study_ready: true, study_setup: state.setup }),
    });
    await getActiveSession(true);
    try {
      await studyRequest("/api/events", { method: "POST", body: JSON.stringify({
        session_id: session.id, timestamp: new Date().toISOString(), source: "browser",
        type: "page_ready", url: session.target_url, page_title: state.frameTitle || "",
        payload: { kind: "study_recording_start", evidence_kind: "recorder_metadata",
          ui_mode: "single_window", tab_id: state.tabId, frame_id: frame.frameId },
      }) });
    } catch { return { ...ready, started: true, reason: "start_metadata_failed" }; }
    return { ...ready, started: true };
  } catch (error) { return studyFailure("collector_error", error.message); }
}

// Since 0.3.3 the full page URL (query and hash included) is recorded; only
// non-web schemes such as chrome:// are left blank.
function recordedUrl(value) {
  if (!value) return "";
  try {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol)) return "";
    return url.href;
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

// An assigned task records only its own frame inside the dashboard tab. Anything
// else (other tabs, the dashboard document itself) is dropped (R6, 2026-10-11).
async function allowedFrame(session, tabId, frameId) {
  if (!session?.study) return true;
  // A free task records every frame of its own window's tabs (0.6.0), never the dashboard.
  if (isFree(session)) {
    const state = await loadState();
    return Boolean(state && state.sessionId === session.id && state.mode === "free" &&
      Number.isInteger(tabId) && (state.siteTabIds || []).includes(tabId));
  }
  if (!Number.isInteger(tabId) || !Number.isInteger(frameId) || frameId === 0) return false;
  const state = await loadState();
  return Boolean(state && state.sessionId === session.id && state.tabId === tabId && state.frameId === frameId);
}

async function sendEvent(event, tabId = undefined, frameId = undefined, expectedSessionId = undefined) {
  const capturedAt = event.timestamp || new Date().toISOString();
  let session = await getActiveSession();
  if ((!session || Date.parse(capturedAt) < Date.parse(session.started_at)) && previousSession) {
    session = previousSession;
  }
  if (!session) return { recorded: false, reason: "no_active_session" };
  if (expectedSessionId && session.id !== expectedSessionId) return { recorded: false, reason: "session_changed" };
  const launch = freeLaunch;
  if (launch?.sessionId === session.id && Number.isInteger(tabId) && launch.tabs.has(tabId)) {
    const size = JSON.stringify(event).length;
    if (launch.events.length < 128 && launch.bytes + size <= 1024 * 1024) {
      launch.events.push({ event: { ...event, timestamp: capturedAt }, tabId, frameId });
      launch.bytes += size;
    } else launch.dropped.set(tabId, (launch.dropped.get(tabId) || 0) + 1);
    return { recorded: false, queued: true };
  }
  if (!(await allowedFrame(session, tabId, frameId))) return { recorded: false, reason: "outside_task_frame" };
  if (session.id !== environmentReportedSession) {
    await reportBrowserEnvironment(session.id);
    environmentReportedSession = session.id;
  }
  const payload = {
    session_id: session.id,
    timestamp: capturedAt,
    source: "browser",
    ...event,
    payload: { ...(event.payload || {}), tab_id: tabId, frame_id: frameId },
  };
  if (payload.url) payload.url = recordedUrl(payload.url);
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

function inferHistoryDirection(key, url, qualifiers) {
  const cleanUrl = recordedUrl(url);
  const history = frameHistories.get(key) || { entries: [], index: -1 };
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

  frameHistories.set(key, history);
  return direction;
}

async function rememberFrameTitle(sender, title, url) {
  const state = await loadState();
  if (!state || sender.tab?.id !== state.tabId) return { accepted: false };
  if (state.frameId !== sender.frameId) {
    // The frame id is resolved by the readiness check; accept the title early when
    // it comes from a frame showing the assigned copy's origin.
    let session = activeCache.session;
    if (!session) { try { session = (await studyRequest(`/api/sessions/${state.sessionId}`)).session; } catch { return { accepted: false }; } }
    try { if (new URL(url).origin !== new URL(session.target_url).origin) return { accepted: false }; } catch { return { accepted: false }; }
  }
  state.frameTitle = String(title || "").slice(0, 300);
  await saveState(state);
  return { accepted: true };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.kind === "a11y-study-command") {
    studyCommands = studyCommands
      .catch(() => {})
      .then(() => studyCommand(message, sender))
      .catch(err => ({ ok: false, error: String(err?.message || err) }));
    studyCommands.then(sendResponse);
    return true;
  }
  if (message?.kind === "recorder-event") {
    const event = {
      ...message.event,
      url: message.event?.url || sender.url || "",
    };
    sendEvent(event, sender.tab?.id, sender.frameId).then(sendResponse);
    return true;
  }
  if (message?.kind === "recorder-frame-title") {
    rememberFrameTitle(sender, message.title, message.url).then(sendResponse);
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

// Only the dashboard's own sub-frame counts; an embed inside the copy has its own parent.
const studyFrameEvent = (details) => details.frameId !== 0 && (details.parentFrameId ?? 0) === 0 && /^https?:/.test(details.url);
chrome.webNavigation.onErrorOccurred.addListener(async (details) => {
  if (studyFrameEvent(details)) frameErrors.set(details.tabId, { url: details.url, error: details.error });
  if (/^https?:/.test(details.url) && isFree(await getActiveSession())) {
    // An error page may never run the content script. Keep this technical evidence
    // without guessing a participant outcome or treating missing events as an FE.
    return sendEvent({ type: "marker", url: details.url,
      timestamp: new Date(details.timeStamp || Date.now()).toISOString(),
      payload: { kind: "free_navigation_error", evidence_kind: "browser_metadata", error: details.error },
    }, details.tabId, details.frameId);
  }
});

chrome.webNavigation.onCommitted.addListener((details) => {
  if (studyFrameEvent(details)) frameErrors.delete(details.tabId);
  if (!/^https?:/.test(details.url)) return;
  const qualifiers = details.transitionQualifiers || [];
  const direction = inferHistoryDirection(`${details.tabId}:${details.frameId}`, details.url, qualifiers);
  sendEvent({
    type: "navigation",
    url: details.url,
    payload: {
      direction,
      transition_type: details.transitionType,
      transition_qualifiers: qualifiers,
    },
  }, details.tabId, details.frameId);
});

chrome.webNavigation.onHistoryStateUpdated.addListener((details) => {
  if (!/^https?:/.test(details.url)) return;
  sendEvent({
    type: "history",
    url: details.url,
    payload: {
      direction: "state_change",
      kind: "history_state_updated",
      transition_type: details.transitionType,
    },
  }, details.tabId, details.frameId);
});

// A link that opens a new tab from the free-task site belongs to the task (0.6.0).
chrome.tabs.onCreated?.addListener((tab) => {
  freeTabClaims = freeTabClaims.catch(() => {}).then(async () => {
    if (freeLaunch) {
      if (Number.isInteger(tab.id) && freeLaunch.tabs.size < 32) freeLaunch.tabs.set(tab.id, tab);
      return;
    }
    const state = await loadState();
    if (state?.mode !== "free" || state.awaitingSite || !Number.isInteger(tab.id)) return;
    if (tab.windowId !== state.siteWindowId && !(state.siteTabIds || []).includes(tab.openerTabId)) return;
    state.siteTabIds = [...new Set([...(state.siteTabIds || []), tab.id])];
    await saveState(state);
  });
  return freeTabClaims;
});

chrome.tabs.onRemoved.addListener((tabId) => {
  frameErrors.delete(tabId);
  for (const key of frameHistories.keys()) if (key.startsWith(`${tabId}:`)) frameHistories.delete(key);
});

// The Alt+Shift+M browser command was removed in 0.3.4 (Windows input-language toggle).
async function createMarker(label) {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const tab = tabs[0];
  const state = await loadState();
  return sendEvent({
    type: "marker",
    url: tab?.url || "",
    page_title: tab?.title || "",
    payload: { label },
  }, tab?.id, state?.frameId ?? undefined);
}

// 0.5.2: the first window of a new kit once stayed blank, titled with its address, until F5
// (Windows 10/28, 준비5). When the browser starts, a recorder tab that has not loaded its page
// 8 s later is reloaded once. Only at start-up: the worker also wakes during a task.
const DASHBOARD_TITLE = "A11y Task Recorder";
async function reloadStalledDashboard() {
  try {
    const tabs = await chrome.tabs.query({ url: ["http://127.0.0.1:8765/*"] });
    for (const tab of tabs) {
      if (tab.status !== "complete" || tab.title !== DASHBOARD_TITLE) await chrome.tabs.reload(tab.id);
    }
  } catch { /* no recorder tab */ }
}
const watchStartup = () => setTimeout(reloadStalledDashboard, 8000);

chrome.runtime.onInstalled.addListener(() => {
  getActiveSession(true);
  watchStartup();
});
chrome.runtime.onStartup?.addListener(watchStartup);

getActiveSession(true);
