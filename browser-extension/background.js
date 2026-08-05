const COLLECTOR = "http://127.0.0.1:8765";
const ACTIVE_CACHE_MS = 1200;
const RECORDER_CLIENT = "a11y-recorder-cft-v1";

let activeCache = { checkedAt: 0, session: null };
let environmentReportedSession = null;
const tabHistories = new Map();

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
  const session = await getActiveSession();
  if (!session) return { recorded: false, reason: "no_active_session" };
  if (session.id !== environmentReportedSession) {
    await reportBrowserEnvironment(session.id);
    environmentReportedSession = session.id;
  }
  const payload = {
    session_id: session.id,
    timestamp: new Date().toISOString(),
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
