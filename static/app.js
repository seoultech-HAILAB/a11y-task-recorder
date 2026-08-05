const API = "/api";

const state = {
  currentSession: null,
  events: [],
  issues: [],
  steps: [],
  selectedEventIds: new Set(),
  timelineView: "grouped",
  nvdaConnected: false,
  notesDirty: false,
  pollTimer: null,
  noticeTimer: null,
};

const statusLabels = {
  draft: "초안",
  active: "기록 중",
  completed: "완료",
  abandoned: "중단",
};

const sourceLabels = {
  nvda: "NVDA",
  browser: "브라우저",
  dashboard: "대시보드",
};

const typeLabels = {
  speech: "음성 출력",
  speech_episode: "NVDA 발화",
  speech_cancel: "음성 취소",
  speech_canceled: "음성 취소",
  input: "키 입력",
  keyboard: "키 입력",
  focus: "포커스",
  navigation: "페이지 이동",
  history: "화면 이동",
  click: "클릭",
  submit: "폼 제출",
  marker: "불편 표시",
  mode: "NVDA 모드",
  step_start: "step 시작",
  step_end: "step 완료",
  hint: "힌트",
  page_ready: "페이지 준비",
  dom_mutation: "DOM 변화",
};

function byId(id) {
  return document.getElementById(id);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function request(path, options = {}) {
  const config = { ...options };
  if (config.body && typeof config.body !== "string") {
    config.headers = { "Content-Type": "application/json", ...(config.headers || {}) };
    config.body = JSON.stringify(config.body);
  }
  const response = await fetch(`${API}${path}`, config);
  let data = {};
  try {
    data = await response.json();
  } catch {
    data = {};
  }
  if (!response.ok) {
    throw new Error(data.error || `요청에 실패했습니다 (${response.status})`);
  }
  return data;
}

function showNotice(message, isError = false) {
  const notice = byId("notice");
  notice.textContent = message;
  notice.classList.toggle("error", isError);
  notice.hidden = false;
  clearTimeout(state.noticeTimer);
  state.noticeTimer = setTimeout(() => {
    notice.hidden = true;
  }, 5000);
}

async function checkConnection() {
  const status = byId("connection-status");
  const nvda = byId("nvda-status");
  try {
    const health = await request("/health");
    status.textContent = "로컬 서버 연결됨";
    status.classList.add("connected");
    state.nvdaConnected = Boolean(health.nvda_connected);
    nvda.textContent = state.nvdaConnected ? "NVDA 연결됨" : "NVDA 미연결";
    nvda.classList.toggle("connected", state.nvdaConnected);
    nvda.classList.toggle("warning", !state.nvdaConnected);
  } catch {
    status.textContent = "서버 연결 끊김";
    status.classList.remove("connected");
    state.nvdaConnected = false;
    nvda.textContent = "NVDA 확인 불가";
    nvda.classList.remove("connected");
    nvda.classList.add("warning");
  }
}

function showView(name) {
  byId("home-view").hidden = name !== "home";
  byId("session-view").hidden = name !== "session";
  byId("main").focus();
}

function stopPolling() {
  clearInterval(state.pollTimer);
  state.pollTimer = null;
}

function goHome() {
  stopPolling();
  state.currentSession = null;
  state.events = [];
  state.issues = [];
  state.steps = [];
  state.selectedEventIds.clear();
  state.notesDirty = false;
  window.history.replaceState({}, "", "/");
  showView("home");
  loadSessions();
}

async function loadSessions() {
  try {
    const data = await request("/sessions");
    renderSessions(data.sessions);
  } catch (error) {
    byId("session-list").innerHTML =
      `<p class="empty-state">${escapeHtml(error.message)}</p>`;
  }
}

function formatDuration(seconds) {
  const safe = Math.max(0, Number(seconds) || 0);
  const hours = Math.floor(safe / 3600);
  const minutes = Math.floor((safe % 3600) / 60);
  const remainder = safe % 60;
  if (hours) return `${hours}시간 ${minutes}분 ${remainder}초`;
  if (minutes) return `${minutes}분 ${remainder}초`;
  return `${remainder}초`;
}

function formatDate(timestamp) {
  if (!timestamp) return "—";
  return new Intl.DateTimeFormat("ko-KR", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(timestamp));
}

function formatTime(timestamp) {
  if (!timestamp) return "—";
  return new Intl.DateTimeFormat("ko-KR", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    fractionalSecondDigits: 3,
    hour12: false,
  }).format(new Date(timestamp));
}

function renderSessions(sessions) {
  const target = byId("session-list");
  if (!sessions.length) {
    target.innerHTML =
      '<p class="empty-state">아직 세션이 없습니다. 첫 과업 세션을 만들어 보세요.</p>';
    return;
  }
  target.innerHTML = sessions
    .map((session) => {
      const summary = session.summary || {};
      return `
        <article class="session-card">
          <div>
            <p class="session-card-meta">
              <span class="session-status ${escapeHtml(session.status)}">
                ${escapeHtml(statusLabels[session.status] || session.status)}
              </span>
              ${escapeHtml(session.participant || "참여자 미지정")}
            </p>
            <h3>${escapeHtml(session.title)}${
              Number(session.round) > 1
                ? ` <span class="round-badge">${Number(session.round)}회차</span>`
                : ""
            }</h3>
            <p class="session-card-meta">
              ${escapeHtml(formatDate(session.started_at || session.created_at))}
              · ${escapeHtml(formatDuration(summary.duration_seconds))}
              · 문제 ${Number(summary.issue_count || 0)}건
            </p>
          </div>
          <button class="button quiet compact" type="button"
            data-action="open-session" data-session-id="${escapeHtml(session.id)}">
            열기
          </button>
        </article>`;
    })
    .join("");
}

async function createSession(form) {
  const data = new FormData(form);
  const environment = {
    nvda_version: data.get("nvda_version") || "",
    browser: data.get("browser") || "",
    speech_rate: data.get("speech_rate") || "",
    keyboard_layout: data.get("keyboard_layout") || "",
  };
  const payload = {
    title: data.get("title"),
    participant: data.get("participant"),
    target_url: data.get("target_url"),
    scenario: data.get("scenario"),
    expected_announcement: data.get("expected_announcement"),
    prior_site_experience: data.get("prior_site_experience"),
    environment,
  };
  const result = await request("/sessions", { method: "POST", body: payload });
  form.reset();
  showNotice("세션을 만들었습니다.");
  await openSession(result.session.id);
}

async function openSession(sessionId, announce = true) {
  stopPolling();
  state.notesDirty = false;
  window.history.replaceState({}, "", `/?session=${encodeURIComponent(sessionId)}`);
  showView("session");
  await refreshSession(sessionId);
  if (announce) showNotice(`‘${state.currentSession.title}’ 세션을 열었습니다.`);
  if (state.currentSession.status === "active") {
    state.pollTimer = setInterval(() => refreshSession(sessionId, false), 1600);
  }
}

async function refreshSession(sessionId = state.currentSession?.id, updateTimestamp = true) {
  if (!sessionId) return;
  try {
    const [sessionData, eventData, issueData] = await Promise.all([
      request(`/sessions/${sessionId}`),
      request(`/sessions/${sessionId}/events?limit=10000`),
      request(`/sessions/${sessionId}/issues`),
      checkConnection(),
    ]);
    state.currentSession = sessionData.session;
    state.events = eventData.events;
    state.issues = issueData.issues;
    state.steps = sessionData.session.steps || [];
    renderSession();
    if (updateTimestamp) {
      byId("last-updated").textContent = `갱신 ${formatTime(new Date().toISOString())}`;
    }
  } catch (error) {
    showNotice(error.message, true);
    stopPolling();
  }
}

function renderSession() {
  const session = state.currentSession;
  byId("session-title").textContent = session.title;
  byId("session-participant").textContent = session.participant
    ? `참여자 ${session.participant}`
    : "참여자 미지정";
  byId("session-target").textContent = session.target_url || "시작 URL 미지정";
  byId("session-status").textContent = statusLabels[session.status] || session.status;
  byId("session-status").className = `session-status ${session.status}`;

  const active = session.status === "active";
  const elapsedSeconds = session.started_at
    ? (Date.now() - new Date(session.started_at).getTime()) / 1000
    : 0;
  const hasNvdaEvents = state.events.some((event) => event.source === "nvda");
  const hasBrowserEvents = state.events.some((event) => event.source === "browser");
  const nvdaCaptureMissing =
    active && elapsedSeconds >= 8 && hasBrowserEvents && !hasNvdaEvents;
  byId("nvda-warning").hidden =
    !active || (state.nvdaConnected && !nvdaCaptureMissing);
  byId("start-button").hidden = session.status !== "draft";
  if (!active) byId("marker-intensity").hidden = true;
  byId("marker-button").hidden = !active || !byId("marker-intensity").hidden;
  byId("stop-button").hidden = !active;
  byId("abandon-button").hidden = !active;
  byId("hint-form").hidden = !active;
  byId("step-form").querySelectorAll("input, button").forEach((control) => {
    control.disabled = session.status === "completed" || session.status === "abandoned";
  });
  byId("event-filter").disabled = !state.events.length;
  if (!state.notesDirty) {
    byId("session-notes").value = session.notes || "";
  }

  byId("export-json").href = `${API}/sessions/${session.id}/export.json`;
  byId("export-csv").href = `${API}/sessions/${session.id}/export.csv`;
  byId("export-interactions").href =
    `${API}/sessions/${session.id}/export-interactions.csv`;

  renderMetrics(session.summary || {});
  renderRounds();
  renderEvents();
  renderIssues();
  renderSteps();
  renderAnchors();
  renderDetails();
}

function renderMetrics(summary) {
  const metrics = [
    ["총 검사 시간", formatDuration(summary.duration_seconds)],
    ["Tab", `${Number(summary.tab_forward || 0)}회`],
    ["Shift+Tab", `${Number(summary.tab_backward || 0)}회`],
    ["뒤로가기", `${Number(summary.back_count || 0)}회`],
    ["NVDA 발화 에피소드", `${Number(summary.speech_episode_count || summary.speech_count || 0)}건`],
    ["발화 조각", `${Number(summary.speech_fragment_count || 0)}개`],
    ["발화 중단", `${Number(summary.speech_interruption_count || 0)}회`],
    ["힌트", `${Number(summary.hint_count || 0)}회`],
    ["불편 표시", `${Number(summary.marker_count || 0)}건`],
    ["등록 문제", `${Number(summary.issue_count || 0)}건`],
  ];
  byId("metrics").innerHTML = metrics
    .map(
      ([label, value]) =>
        `<dl class="metric"><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></dl>`
    )
    .join("");

  const shortcuts = Object.entries(summary.shortcut_counts || {});
  byId("shortcut-counts").innerHTML = shortcuts.length
    ? `<ul class="shortcut-list">${shortcuts
        .map(
          ([shortcut, count]) =>
            `<li><kbd>${escapeHtml(shortcut)}</kbd> ${Number(count)}회</li>`
        )
        .join("")}</ul>`
    : '<p class="muted">기록된 단축키가 없습니다.</p>';
}

function eventMatchesFilter(event, filter) {
  if (filter === "all") return true;
  if (filter === "input") return ["input", "keyboard"].includes(event.type);
  if (filter === "speech") return ["speech", "speech_episode"].includes(event.type);
  if (filter === "navigation") return ["navigation", "history"].includes(event.type);
  return event.type === filter;
}

function stepLabel(stepId) {
  const step = state.steps.find((item) => item.id === stepId);
  return step ? `Step ${step.position}. ${step.title}` : "";
}

function eventDetail(event) {
  const payload = event.payload || {};
  const element = event.element || {};
  let primary = "";
  if (["speech", "speech_episode"].includes(event.type)) {
    primary =
      payload.normalized_text || payload.text || payload.speech || "(텍스트 없음)";
  } else if (["input", "keyboard"].includes(event.type)) {
    primary = payload.chord || payload.gesture || payload.display_name || payload.key || "키 입력";
  } else if (event.type === "marker") {
    primary = payload.label || "사용자가 불편 지점을 표시했습니다.";
    if (payload.intensity) primary += ` · 강도 ${payload.intensity}`;
  } else if (["navigation", "history"].includes(event.type)) {
    primary = payload.direction
      ? `${payload.direction} · ${payload.transition_type || payload.kind || ""}`
      : payload.kind || "페이지 이동";
  } else if (event.type === "focus") {
    primary =
      element.accessible_name || element.name || payload.name || element.selector || "포커스 이동";
  } else if (event.type === "hint") {
    primary = payload.text || "힌트 제공";
  } else if (event.type === "dom_mutation") {
    primary = `추가 ${Number(payload.added_nodes || 0)} · 삭제 ${Number(
      payload.removed_nodes || 0
    )} · 속성 ${Number(payload.attribute_changes || 0)}`;
  } else if (["step_start", "step_end"].includes(event.type)) {
    primary = payload.title || stepLabel(event.step_id) || typeLabels[event.type];
  } else {
    primary = payload.label || payload.action || payload.value || typeLabels[event.type] || event.type;
  }
  const context = [
    element.role ? `역할: ${element.role}` : "",
    element.selector ? `위치: ${element.selector}` : "",
    element.unique_id ? `ID: ${element.unique_id}` : "",
    stepLabel(event.step_id),
    event.speech_end_ts
      ? `발화 종료 ${formatTime(event.speech_end_ts)}${
          event.interrupted ? " · 청취 중단" : ""
        }`
      : "",
    event.page_title || "",
    event.url || "",
  ].filter(Boolean);
  return { primary, context: context.join(" · ") };
}

const GROUP_LABELS = {
  input: "입력",
  focus: "포커스",
  speech: "발화",
  navigation: "페이지 이동",
  page_ready: "페이지 준비",
  marker: "불편 표시",
  hint: "힌트",
  step_start: "step 시작",
  step_end: "step 완료",
  mode: "NVDA 모드",
};

const DIRECTION_LABELS = {
  new: "새 페이지",
  back: "뒤로",
  forward: "앞으로",
  reload: "새로고침",
  back_or_forward: "뒤로/앞으로",
  state_change: "화면 전환",
};

// 키 입력 하나에 도착 요소(focus)와 NVDA 안내(speech)를 묶어 상호작용 단위를 만든다.
function buildInteractionGroups(events) {
  const groups = [];
  let current = null;
  const push = (kind, event) => {
    current = {
      kind,
      event,
      firstId: event.id,
      lastId: event.id,
      key: "",
      elementName: "",
      elementRole: "",
      speeches: [],
      pageTitle: "",
    };
    groups.push(current);
    return current;
  };
  for (const event of events) {
    const type = event.type;
    const payload = event.payload || {};
    const element = event.element || {};
    if (type === "input" || type === "keyboard") {
      const group = push("input", event);
      group.key =
        payload.display_name || payload.chord || payload.gesture || payload.key || "입력";
    } else if (type === "focus") {
      const name = element.accessible_name || element.name || "";
      if (current && current.kind === "input" && !current.elementName) {
        current.elementName = name;
        current.elementRole = element.role || "";
        current.lastId = event.id;
      } else {
        const group = push("focus", event);
        group.elementName = name;
        group.elementRole = element.role || "";
      }
    } else if (type === "speech" || type === "speech_episode") {
      const speech = {
        text: payload.normalized_text || payload.raw_text || payload.text || "",
        interrupted: Boolean(event.interrupted),
        listenSeconds: null,
      };
      if (event.speech_end_ts) {
        const ms = new Date(event.speech_end_ts) - new Date(event.timestamp);
        if (Number.isFinite(ms) && ms >= 0) speech.listenSeconds = ms / 1000;
      }
      if (current && current.kind === "input") {
        current.speeches.push(speech);
        current.lastId = event.id;
      } else {
        push("speech", event).speeches.push(speech);
      }
    } else if (type === "speech_cancel" || type === "speech_canceled") {
      if (current) current.lastId = event.id;
    } else if (type === "dom_mutation") {
      // 저수준 기록은 원시 이벤트 보기에서만 표시한다.
    } else if (type === "navigation" || type === "history") {
      push("navigation", event);
    } else if (type === "page_ready") {
      if (current && current.kind === "navigation") {
        current.lastId = event.id;
        current.pageTitle = event.page_title || "";
      } else {
        push("page_ready", event);
      }
    } else {
      push(type, event);
      if (type === "marker" || type === "hint") current = null;
    }
  }
  return groups;
}

function groupMatchesFilter(group, filter) {
  if (filter === "all") return true;
  if (filter === "input") return group.kind === "input";
  if (filter === "speech") return group.kind === "speech";
  if (filter === "navigation") {
    return group.kind === "navigation" || group.kind === "page_ready";
  }
  return group.kind === filter;
}

function renderGroupDetail(group) {
  const event = group.event;
  const payload = event.payload || {};
  const speeches = group.speeches
    .map((speech) => {
      const listened =
        speech.listenSeconds === null
          ? ""
          : speech.interrupted
            ? ` · ${speech.listenSeconds.toFixed(1)}초 청취`
            : " · 끝까지 들음";
      return `<small class="speech-line">“${escapeHtml(speech.text || "(내용 없음)")}”${listened}</small>`;
    })
    .join("");
  const role = group.elementRole
    ? ` <small class="role-tag">${escapeHtml(group.elementRole)}</small>`
    : "";
  if (group.kind === "input") {
    const destination = group.elementName ? ` → ${group.elementName}` : "";
    return `<strong>${escapeHtml(group.key + destination)}</strong>${role}${speeches}`;
  }
  if (group.kind === "focus") {
    return `<strong>${escapeHtml(group.elementName || "(요소 정보 없음)")}</strong>${role}`;
  }
  if (group.kind === "speech") {
    return speeches || "<strong>(내용 없음)</strong>";
  }
  if (group.kind === "navigation") {
    const direction = DIRECTION_LABELS[payload.direction] || "";
    const title = group.pageTitle ? ` <small>${escapeHtml(group.pageTitle)}</small>` : "";
    return `<strong>${escapeHtml(event.url || "(URL 없음)")}</strong>
      ${direction ? `<small>${escapeHtml(direction)}</small>` : ""}${title}`;
  }
  if (group.kind === "page_ready") {
    return `<strong>${escapeHtml(event.page_title || event.url || "페이지 준비")}</strong>`;
  }
  const detail = eventDetail(event);
  return `<strong>${escapeHtml(detail.primary)}</strong>
    ${detail.context ? `<small>${escapeHtml(detail.context)}</small>` : ""}`;
}

function renderEvents() {
  const filter = byId("event-filter").value;
  const grouped = state.timelineView === "grouped";
  const domOption = byId("event-filter").querySelector('option[value="dom_mutation"]');
  if (domOption) domOption.hidden = grouped;
  const target = byId("event-list");
  const emptyRow =
    '<tr><td colspan="6" class="empty-state">조건에 맞는 이벤트가 없습니다.</td></tr>';

  if (grouped) {
    const groups = buildInteractionGroups(state.events).filter((group) =>
      groupMatchesFilter(group, filter)
    );
    if (!groups.length) {
      target.innerHTML = emptyRow;
      updateSelection();
      return;
    }
    target.innerHTML = groups
      .map((group) => {
        const event = group.event;
        const label = GROUP_LABELS[group.kind] || typeLabels[event.type] || event.type;
        const checked = state.selectedEventIds.has(group.firstId) ? "checked" : "";
        const rowClass =
          group.kind === "marker" ? "marker-row" : group.kind === "hint" ? "hint-row" : "";
        return `
        <tr data-event-id="${group.firstId}" class="${rowClass}">
          <td>
            <input type="checkbox" ${checked}
              aria-label="이벤트 ${group.firstId}, ${escapeHtml(label)} 구간 선택"
              data-event-select="${group.firstId}" data-event-select-end="${group.lastId}">
          </td>
          <td class="event-time">${escapeHtml(formatTime(event.timestamp))}</td>
          <td class="event-source">${escapeHtml(sourceLabels[event.source] || event.source)}</td>
          <td><span class="event-type ${escapeHtml(group.kind)}">${escapeHtml(label)}</span></td>
          <td class="event-detail">${renderGroupDetail(group)}</td>
          <td>
            <button class="button quiet compact" type="button"
              data-action="label-event" data-event-id="${group.firstId}">
              이 지점 라벨링
            </button>
          </td>
        </tr>`;
      })
      .join("");
    updateSelection();
    return;
  }

  const events = state.events.filter((event) => eventMatchesFilter(event, filter));
  if (!events.length) {
    target.innerHTML = emptyRow;
    updateSelection();
    return;
  }
  target.innerHTML = events
    .map((event) => {
      const detail = eventDetail(event);
      const checked = state.selectedEventIds.has(event.id) ? "checked" : "";
      const rowClass =
        event.type === "marker" ? "marker-row" : event.type === "hint" ? "hint-row" : "";
      return `
        <tr data-event-id="${event.id}" class="${rowClass}">
          <td>
            <input type="checkbox" ${checked}
              aria-label="이벤트 ${event.id}, ${escapeHtml(typeLabels[event.type] || event.type)} 구간 선택"
              data-event-select="${event.id}">
          </td>
          <td class="event-time">${escapeHtml(formatTime(event.timestamp))}</td>
          <td class="event-source">${escapeHtml(sourceLabels[event.source] || event.source)}</td>
          <td><span class="event-type ${escapeHtml(event.type)}">
            ${escapeHtml(typeLabels[event.type] || event.type)}
          </span></td>
          <td class="event-detail">
            <strong>${escapeHtml(detail.primary)}</strong>
            ${detail.context ? `<small>${escapeHtml(detail.context)}</small>` : ""}
          </td>
          <td>
            <button class="button quiet compact" type="button"
              data-action="label-event" data-event-id="${event.id}">
              이 지점 라벨링
            </button>
          </td>
        </tr>`;
    })
    .join("");
  updateSelection();
}

function setIssueRange(ids) {
  state.selectedEventIds = new Set(ids.map(Number));
  document.querySelectorAll("[data-event-select]").forEach((checkbox) => {
    checkbox.checked = state.selectedEventIds.has(Number(checkbox.dataset.eventSelect));
  });
  updateSelection();
}

function updateSelection() {
  const ids = [...state.selectedEventIds].sort((a, b) => a - b);
  const start = ids[0] || "";
  const end = ids[ids.length - 1] || "";
  document.querySelectorAll("tr[data-event-id]").forEach((row) => {
    const rowId = Number(row.dataset.eventId);
    row.classList.toggle(
      "in-range",
      ids.length > 0 && rowId >= Number(start) && rowId <= Number(end)
    );
  });
  byId("start-event-id").value = start;
  byId("end-event-id").value = end;
  const startEvent = state.events.find((event) => event.id === start);
  const endEvent = state.events.find((event) => event.id === end);
  byId("start-step-id").value = startEvent?.step_id || "";
  byId("end-step-id").value = endEvent?.step_id || "";
  const stepRange = [
    stepLabel(startEvent?.step_id),
    stepLabel(endEvent?.step_id),
  ].filter(Boolean);
  byId("issue-range").textContent = ids.length
    ? `이벤트 #${start}${start !== end ? `–#${end}` : ""} 연결됨${
        stepRange.length ? ` · ${[...new Set(stepRange)].join("–")}` : ""
      }`
    : "연결된 이벤트 없음";
  byId("selection-help").textContent = ids.length
    ? `${ids.length}개 이벤트 선택됨 · 등록 시 #${start}${start !== end ? `–#${end}` : ""} 구간으로 연결`
    : "문제 구간을 만들려면 이벤트를 하나 이상 선택하세요.";
}

function renderIssues() {
  const target = byId("issue-list");
  if (!state.issues.length) {
    target.innerHTML = '<p class="empty-state">등록한 문제가 없습니다.</p>';
    return;
  }
  const severityLabels = {
    minor: "경미",
    moderate: "보통",
    major: "중대",
    critical: "치명적",
  };
  target.innerHTML = state.issues
    .map((issue) => {
      const range =
        issue.start_event_id || issue.end_event_id
          ? `이벤트 #${issue.start_event_id || issue.end_event_id}${
              issue.end_event_id && issue.end_event_id !== issue.start_event_id
                ? `–#${issue.end_event_id}`
                : ""
            }`
          : "로그 구간 미지정";
      const steps = [
        stepLabel(issue.start_step_id),
        stepLabel(issue.end_step_id),
      ].filter(Boolean);
      return `
        <article class="issue-card" data-severity="${escapeHtml(issue.severity)}">
          <p class="muted">${escapeHtml(severityLabels[issue.severity] || issue.severity)}
            · ${escapeHtml(range)}${
              steps.length ? ` · ${escapeHtml([...new Set(steps)].join("–"))}` : ""
            }</p>
          <h3>${escapeHtml(issue.summary)}</h3>
          ${issue.description ? `<p>${escapeHtml(issue.description)}</p>` : ""}
          ${
            issue.expected_announcement
              ? `<p><strong>기대:</strong> ${escapeHtml(issue.expected_announcement)}</p>`
              : ""
          }
          <div>${(issue.tags || [])
            .map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`)
            .join("")}</div>
          <button class="delete-issue" type="button" data-action="delete-issue"
            data-issue-id="${escapeHtml(issue.id)}">문제 삭제</button>
        </article>`;
    })
    .join("");
}

function renderDetails() {
  const session = state.currentSession;
  const environment = session.environment || {};
  const rows = [
    ["과업 설명", session.scenario || "미입력"],
    ["주요 기대 안내", session.expected_announcement || "미입력"],
    ["NVDA", environment.nvda_version || "미입력"],
    ["브라우저", environment.browser || "미입력"],
    ["음성 속도", environment.speech_rate || "미입력"],
    ["키보드 배열", environment.keyboard_layout || "미입력"],
    [
      "사이트 사용 경험",
      {
        none: "처음 사용",
        rare: "가끔 사용",
        frequent: "자주 사용",
      }[session.prior_site_experience] || "미입력",
    ],
    ["시작", formatDate(session.started_at)],
    ["종료", formatDate(session.ended_at)],
  ];
  byId("session-details").innerHTML = rows
    .map(([term, description]) => `<dt>${escapeHtml(term)}</dt><dd>${escapeHtml(description)}</dd>`)
    .join("");
}

function renderSteps() {
  const target = byId("step-list");
  if (!state.steps.length) {
    target.innerHTML =
      '<p class="empty-state">등록한 step이 없습니다. step 없이도 기록할 수 있지만 불편 구간 분석은 덜 정밀해집니다.</p>';
    return;
  }
  const activeSession = state.currentSession.status === "active";
  const outcomeLabels = {
    complete: "성공",
    assisted: "힌트 후 성공",
    blocked: "막힘",
  };
  target.innerHTML = state.steps
    .map((step) => {
      const status = {
        pending: "대기",
        active: "진행 중",
        completed: "완료",
      }[step.status] || step.status;
      const outcomeText = outcomeLabels[step.outcome]
        ? ` · 결과: ${outcomeLabels[step.outcome]}`
        : "";
      const outcomeControls =
        step.status === "completed"
          ? `<div class="step-outcome-row">
              <label class="sr-only" for="outcome-${escapeHtml(step.id)}">step 수행 결과</label>
              <select id="outcome-${escapeHtml(step.id)}" class="step-outcome"
                data-step-outcome="${escapeHtml(step.id)}">
                ${step.outcome ? "" : '<option value="" selected>결과 선택</option>'}
                <option value="complete" ${step.outcome === "complete" ? "selected" : ""}>성공</option>
                <option value="assisted" ${step.outcome === "assisted" ? "selected" : ""}>힌트 후 성공</option>
                <option value="blocked" ${step.outcome === "blocked" ? "selected" : ""}>막힘</option>
              </select>
              <input class="step-outcome-note" data-step-note="${escapeHtml(step.id)}"
                value="${escapeHtml(step.outcome_note || "")}"
                placeholder="사유 (예: 버튼을 찾지 못함)" aria-label="step 결과 사유">
            </div>`
          : "";
      return `
        <article class="step-item ${escapeHtml(step.status)}">
          <div>
            <p class="muted">Step ${Number(step.position)} · ${escapeHtml(status)}${escapeHtml(outcomeText)}</p>
            <h3>${escapeHtml(step.title)}</h3>
            ${
              step.expected_announcement
                ? `<p>기대 발화: ${escapeHtml(step.expected_announcement)}</p>`
                : ""
            }
            ${outcomeControls}
          </div>
          <div class="step-actions">
            ${
              activeSession && step.status !== "active"
                ? `<button class="button quiet compact" type="button"
                    data-action="step-start" data-step-id="${escapeHtml(step.id)}">
                    이 step 시작
                  </button>`
                : ""
            }
            ${
              activeSession && step.status === "active"
                ? `<button class="button primary compact" type="button"
                    data-action="step-finish" data-step-id="${escapeHtml(step.id)}">
                    step 완료
                  </button>`
                : ""
            }
          </div>
        </article>`;
    })
    .join("");
}

function renderRounds() {
  const nav = byId("round-tabs");
  const session = state.currentSession;
  const rounds = session.rounds || [];
  const finished = ["completed", "abandoned"].includes(session.status);
  nav.hidden = rounds.length <= 1 && !finished;
  if (nav.hidden) {
    nav.innerHTML = "";
    return;
  }
  const tabs = rounds
    .map((item) => {
      const current = item.id === session.id;
      const suffix =
        item.status === "active" ? " · 기록 중" : item.status === "draft" ? " · 준비" : "";
      return `<button type="button" class="round-tab${current ? " active" : ""}"
        data-action="open-round" data-session-id="${escapeHtml(item.id)}"
        aria-current="${current ? "true" : "false"}">${item.round}회차${suffix}</button>`;
    })
    .join("");
  const last = rounds[rounds.length - 1];
  const lastFinished =
    last && ["completed", "abandoned"].includes(last.status);
  const addButton = finished && lastFinished
    ? `<button type="button" class="round-tab add" data-action="rerun">+ 다음 회차</button>`
    : "";
  nav.innerHTML = tabs + addButton;
}

function renderAnchors() {
  const container = byId("anchor-chips");
  const guide = byId("review-guide");
  const completed = ["completed", "abandoned"].includes(state.currentSession?.status);
  guide.hidden = !completed;
  const anchors = state.events.filter(
    (event) => event.type === "marker" || event.type === "hint"
  );
  container.hidden = !anchors.length;
  if (!anchors.length) {
    container.innerHTML = "";
    return;
  }
  let markerCount = 0;
  let hintCount = 0;
  container.innerHTML =
    '<span class="anchor-label">빠른 이동</span>' +
    anchors
      .map((event) => {
        const isMarker = event.type === "marker";
        const order = isMarker ? ++markerCount : ++hintCount;
        const label = `${isMarker ? "불편" : "힌트"} ${order} · ${formatTime(event.timestamp)}`;
        return `<button type="button" class="anchor-chip ${isMarker ? "marker" : "hint"}"
          data-action="jump-event" data-event-id="${event.id}">${escapeHtml(label)}</button>`;
      })
      .join("");
}

function jumpToEvent(eventId) {
  const filter = byId("event-filter");
  let row = document.querySelector(`tr[data-event-id="${eventId}"]`);
  if (!row && filter.value !== "all") {
    filter.value = "all";
    renderEvents();
    row = document.querySelector(`tr[data-event-id="${eventId}"]`);
  }
  if (!row) return;
  row.scrollIntoView({ block: "center", behavior: "smooth" });
  row.classList.add("jump-flash");
  setTimeout(() => row.classList.remove("jump-flash"), 1600);
  row.querySelector("[data-event-select]")?.focus({ preventScroll: true });
}

async function createStep(form) {
  const data = new FormData(form);
  await request(`/sessions/${state.currentSession.id}/steps`, {
    method: "POST",
    body: {
      title: data.get("title"),
      expected_announcement: data.get("expected_announcement"),
    },
  });
  form.reset();
  await refreshSession();
  showNotice("시나리오 step을 추가했습니다.");
}

async function transitionStep(stepId, action) {
  await request(
    `/sessions/${state.currentSession.id}/steps/${stepId}/${action}`,
    { method: "POST", body: {} }
  );
  await refreshSession();
  showNotice(action === "start" ? "step 기록을 시작했습니다." : "step을 완료했습니다.");
}

async function addHint(form) {
  const data = new FormData(form);
  const activeStep = state.steps.find((step) => step.status === "active");
  await request(`/sessions/${state.currentSession.id}/hints`, {
    method: "POST",
    body: {
      text: data.get("text"),
      step_id: activeStep?.id || null,
      timestamp: new Date().toISOString(),
    },
  });
  form.reset();
  await refreshSession();
  showNotice("힌트 제공 시각과 현재 step을 기록했습니다.");
}

async function startSession() {
  await checkConnection();
  if (!state.nvdaConnected) {
    showNotice(
      "NVDA가 연결되지 않아 기록을 시작하지 않았습니다. 평가시작 파일을 다시 실행한 뒤 NVDA 연결됨을 확인하세요.",
      true
    );
    return;
  }
  const result = await request(`/sessions/${state.currentSession.id}/start`, {
    method: "POST",
    body: {},
  });
  state.currentSession = result.session;
  renderSession();
  stopPolling();
  state.pollTimer = setInterval(
    () => refreshSession(state.currentSession.id, false),
    1600
  );
  showNotice("기록을 시작했습니다. NVDA 애드온과 브라우저 확장이 이 세션을 자동으로 감지합니다.");
  if (state.currentSession.target_url) {
    window.open(state.currentSession.target_url, "_blank", "noopener");
  }
}

async function stopSession(status) {
  const notes = byId("session-notes").value;
  const result = await request(`/sessions/${state.currentSession.id}/stop`, {
    method: "POST",
    body: { status, notes },
  });
  state.currentSession = result.session;
  state.notesDirty = false;
  stopPolling();
  await refreshSession(state.currentSession.id);
  const firstAnchor = document.querySelector("#anchor-chips .anchor-chip");
  if (firstAnchor) {
    firstAnchor.focus();
  } else {
    const heading = byId("timeline-heading");
    heading.setAttribute("tabindex", "-1");
    heading.focus();
  }
  showNotice(
    status === "completed"
      ? "세션을 완료했습니다. 타임라인에서 불편 구간을 검토하고 라벨링하세요."
      : "세션을 중단 상태로 저장했습니다."
  );
}

async function addMarker(intensity) {
  const payload = { label: "대시보드에서 불편 지점 표시" };
  if (intensity) payload.intensity = Number(intensity);
  await request("/events", {
    method: "POST",
    body: {
      session_id: state.currentSession.id,
      source: "dashboard",
      type: "marker",
      timestamp: new Date().toISOString(),
      payload,
    },
  });
  await refreshSession(state.currentSession.id);
  const marker = [...state.events].reverse().find((event) => event.type === "marker");
  if (marker) {
    setIssueRange([marker.id]);
  }
  showNotice(
    `불편 지점을 표시했습니다${intensity ? ` (강도 ${intensity})` : ""}. 과업을 계속한 뒤 종료 후 설명을 작성할 수 있습니다.`
  );
}

async function saveStepOutcome(stepId) {
  const outcome =
    document.querySelector(`[data-step-outcome="${stepId}"]`)?.value ?? "";
  const note = document.querySelector(`[data-step-note="${stepId}"]`)?.value ?? "";
  await request(`/sessions/${state.currentSession.id}/steps/${stepId}`, {
    method: "PATCH",
    body: { outcome, outcome_note: note },
  });
  await refreshSession(state.currentSession.id);
  showNotice("step 결과를 저장했습니다.");
}

async function createIssue(form) {
  const data = new FormData(form);
  const payload = {
    summary: data.get("summary"),
    description: data.get("description"),
    expected_announcement: data.get("expected_announcement"),
    severity: data.get("severity"),
    tags: data.get("tags"),
    start_event_id: data.get("start_event_id") || null,
    end_event_id: data.get("end_event_id") || null,
    start_step_id: data.get("start_step_id") || null,
    end_step_id: data.get("end_step_id") || null,
  };
  await request(`/sessions/${state.currentSession.id}/issues`, {
    method: "POST",
    body: payload,
  });
  form.reset();
  byId("issue-severity").value = "moderate";
  setIssueRange([]);
  await refreshSession(state.currentSession.id);
  showNotice("문제를 타임라인에 연결해 등록했습니다.");
}

async function saveNotes() {
  const result = await request(`/sessions/${state.currentSession.id}`, {
    method: "PATCH",
    body: { notes: byId("session-notes").value },
  });
  state.currentSession = result.session;
  state.notesDirty = false;
  showNotice("세션 의견을 저장했습니다.");
}

document.addEventListener("click", async (event) => {
  const control = event.target.closest("[data-action]");
  if (!control) return;
  const action = control.dataset.action;
  try {
    if (action === "home") {
      event.preventDefault();
      goHome();
    } else if (action === "refresh-sessions") {
      await loadSessions();
      showNotice("세션 목록을 새로고침했습니다.");
    } else if (action === "export-package") {
      control.disabled = true;
      try {
        const data = await request("/export-package", { method: "POST", body: {} });
        const pkg = data.package || {};
        const skipped = pkg.active_session_count
          ? ` (진행 중인 세션 ${pkg.active_session_count}개는 제외됨)`
          : "";
        showNotice(
          `결과 패키지를 만들었습니다: ${pkg.file_name} — 완료 세션 ${pkg.session_count}개${skipped}. 결과 폴더가 열립니다.`
        );
      } finally {
        control.disabled = false;
      }
    } else if (action === "open-session") {
      await openSession(control.dataset.sessionId);
    } else if (action === "refresh-session") {
      await refreshSession();
      showNotice("세션 데이터를 새로고침했습니다.");
    } else if (action === "start") {
      await startSession();
    } else if (action === "stop") {
      await stopSession("completed");
    } else if (action === "abandon") {
      await stopSession("abandoned");
    } else if (action === "marker") {
      const group = byId("marker-intensity");
      group.hidden = false;
      control.hidden = true;
      group.querySelector("[data-intensity]")?.focus();
    } else if (action === "marker-intensity") {
      byId("marker-intensity").hidden = true;
      byId("marker-button").hidden = false;
      await addMarker(control.dataset.intensity);
    } else if (action === "marker-cancel") {
      byId("marker-intensity").hidden = true;
      byId("marker-button").hidden = false;
      byId("marker-button").focus();
    } else if (action === "step-start") {
      await transitionStep(control.dataset.stepId, "start");
    } else if (action === "step-finish") {
      await transitionStep(control.dataset.stepId, "finish");
    } else if (action === "view-grouped" || action === "view-raw") {
      state.timelineView = action === "view-raw" ? "raw" : "grouped";
      const filterSelect = byId("event-filter");
      if (state.timelineView === "grouped" && filterSelect.value === "dom_mutation") {
        filterSelect.value = "all";
      }
      byId("view-grouped").classList.toggle("active", state.timelineView === "grouped");
      byId("view-grouped").setAttribute(
        "aria-pressed",
        String(state.timelineView === "grouped")
      );
      byId("view-raw").classList.toggle("active", state.timelineView === "raw");
      byId("view-raw").setAttribute("aria-pressed", String(state.timelineView === "raw"));
      renderEvents();
    } else if (action === "jump-event") {
      jumpToEvent(Number(control.dataset.eventId));
    } else if (action === "open-round") {
      await openSession(control.dataset.sessionId);
    } else if (action === "rerun") {
      control.disabled = true;
      try {
        const result = await request(
          `/sessions/${state.currentSession.id}/rerun`,
          { method: "POST", body: {} }
        );
        await openSession(result.session.id);
        showNotice(
          `${result.session.round}회차 세션이 준비되었습니다. 기록 시작을 누르면 같은 과업을 다시 진행합니다.`
        );
      } finally {
        control.disabled = false;
      }
    } else if (action === "clear-range") {
      setIssueRange([]);
    } else if (action === "label-event") {
      setIssueRange([Number(control.dataset.eventId)]);
      byId("issue-summary").focus();
    } else if (action === "delete-issue") {
      if (window.confirm("이 문제 라벨을 삭제하시겠습니까? 원본 이벤트는 유지됩니다.")) {
        await request(`/issues/${control.dataset.issueId}`, { method: "DELETE" });
        await refreshSession();
        showNotice("문제 라벨을 삭제했습니다.");
      }
    }
  } catch (error) {
    showNotice(error.message, true);
  }
});

document.addEventListener("change", (event) => {
  if (event.target.matches("[data-event-select]")) {
    const id = Number(event.target.dataset.eventSelect);
    const endId = Number(event.target.dataset.eventSelectEnd || id);
    if (event.target.checked) {
      state.selectedEventIds.add(id);
      state.selectedEventIds.add(endId);
    } else {
      state.selectedEventIds.delete(id);
      state.selectedEventIds.delete(endId);
    }
    updateSelection();
  } else if (event.target.id === "event-filter") {
    renderEvents();
  } else if (event.target.matches("[data-step-outcome]")) {
    saveStepOutcome(event.target.dataset.stepOutcome).catch((error) =>
      showNotice(error.message, true)
    );
  } else if (event.target.matches("[data-step-note]")) {
    saveStepOutcome(event.target.dataset.stepNote).catch((error) =>
      showNotice(error.message, true)
    );
  }
});

byId("session-notes").addEventListener("input", () => {
  state.notesDirty = true;
});

byId("session-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await createSession(event.currentTarget);
  } catch (error) {
    showNotice(error.message, true);
  }
});

byId("issue-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await createIssue(event.currentTarget);
  } catch (error) {
    showNotice(error.message, true);
  }
});

byId("step-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await createStep(event.currentTarget);
  } catch (error) {
    showNotice(error.message, true);
  }
});

byId("hint-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await addHint(event.currentTarget);
  } catch (error) {
    showNotice(error.message, true);
  }
});

byId("notes-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await saveNotes();
  } catch (error) {
    showNotice(error.message, true);
  }
});

async function initialize() {
  await checkConnection();
  const sessionId = new URLSearchParams(window.location.search).get("session");
  if (sessionId) {
    try {
      await openSession(sessionId, false);
      return;
    } catch (error) {
      showNotice(error.message, true);
    }
  }
  showView("home");
  await loadSessions();
}

initialize();
