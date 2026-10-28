(() => {
  const pageUrl = new URL(window.location.href);
  // window.top is undefined in unit harnesses; treat that as the top document.
  const isTop = !window.top || window.top === window;
  if (pageUrl.origin === "http://127.0.0.1:8765") {
    // The dashboard bridge lives only in the top-level recorder page.
    if (!isTop) return;
    window.addEventListener("message", async (event) => {
      const message = event.data;
      if (event.source !== window || event.origin !== pageUrl.origin ||
          message?.kind !== "a11y-study-command" ||
          typeof message.requestId !== "string" ||
          typeof message.sessionId !== "string" ||
          !["prepare", "check", "start", "close", "status"].includes(message.action)) return;
      let result;
      try {
        result = await chrome.runtime.sendMessage({ kind: message.kind,
          action: message.action, sessionId: message.sessionId });
      } catch {
        result = { ok: false, ready: false, reason: "extension_unavailable" };
      }
      window.postMessage({ kind: "a11y-study-response", requestId: message.requestId,
        result }, pageUrl.origin);
    });
    return;
  }
  if (
    ["127.0.0.1", "localhost"].includes(pageUrl.hostname) &&
    pageUrl.port === "8765"
  ) {
    return;
  }

  // 0.4.2: when a task starts the dashboard asks its study frame to focus the site's first
  // control, so NVDA's cursor starts in the site (Windows check F3, 10/20). Same rule as study.js.
  // The reply names the history entry the task started on; the recorder's back command
  // (Windows pilot, 10/28) never goes past it, so earlier tasks and the login page stay behind.
  if (!isTop) {
    const toRecorder = (data) => window.parent.postMessage(data, "http://127.0.0.1:8765");
    let lastFocus = null;
    window.addEventListener("focusin", (event) => { lastFocus = event.target; }, true);
    window.addEventListener("message", (event) => {
      if (event.source !== window.parent || event.origin !== "http://127.0.0.1:8765") return;
      if (event.data?.kind === "a11y-study-focus-site") {
        const selector = 'a[href], button, input:not([type="hidden"]), select, textarea, summary, [tabindex]:not([tabindex="-1"])';
        // edge "last": Shift+Tab ran out of the top of the site and comes back in at the bottom (0.5.0).
        const controls = [...document.querySelectorAll(selector)].filter((node) => !node.disabled &&
          node.getClientRects().length > 0 && !node.closest("[inert], [aria-hidden='true']"));
        // edge "resume": 과업으로 돌아가기 goes back to the control the task was ended on (0.5.2).
        const kept = event.data.edge === "resume" && lastFocus?.isConnected ? lastFocus : null;
        if (event.data.focus !== false) (kept || (event.data.edge === "last" ? controls.at(-1) : controls[0]))?.focus();
        toRecorder({ kind: "a11y-study-site-entry", key: window.navigation?.currentEntry?.key || "" });
      } else if (event.data?.kind === "a11y-study-site-back") {
        toRecorder({ kind: "a11y-study-site-back", moved: historyBack(window.navigation, event.data.startKey) });
      }
    });
    // In the recorder's study frame, Alt+Left and Alt+Home go to the recorder: the browser's own
    // back walks the whole tab history (earlier tasks, the recorder page) and its home page
    // replaces the recorder. Pages outside the recorder keep the browser keys.
    window.addEventListener("keydown", (event) => {
      const action = siteKey(event);
      if (!action || window.parent !== window.top ||
          window.location.ancestorOrigins?.[0] !== "http://127.0.0.1:8765") return;
      event.preventDefault();
      toRecorder({ kind: "a11y-study-site-key", action });
    }, true);
  }
  // Same rules as study.js (the practice page and the recorder page itself).
  function siteKey(event) {
    if (event.ctrlKey || event.metaKey || event.shiftKey) return "";
    if ((event.altKey && event.key === "ArrowLeft") || event.key === "BrowserBack") return "back";
    if ((event.altKey && event.key === "Home") || event.key === "BrowserHome") return "start";
    return "";
  }
  function historyBack(nav, startKey) {
    const start = nav?.entries().find((entry) => entry.key === startKey);
    if (!start || nav.currentEntry.index <= start.index) return false;
    const { committed, finished } = nav.back();
    committed.catch(() => {});
    finished.catch(() => {});
    return true;
  }

  const mutationBuffer = {
    added: 0,
    removed: 0,
    attributes: 0,
    characterData: 0,
    attributeNames: new Set(),
    xpaths: new Set(),
    firstChangeTs: null,
    batches: [],
  };
  let flushTimer = null;
  let documentVersion = 0;

  function currentUrl() {
    return window.location.href;
  }

  function xpath(element) {
    if (!(element instanceof Element)) return "";
    if (element.id) {
      const escaped = element.id.replaceAll('"', '\\"');
      return `//*[@id="${escaped}"]`;
    }
    const parts = [];
    let current = element;
    while (current && current.nodeType === Node.ELEMENT_NODE && parts.length < 10) {
      const tag = current.localName;
      if (!tag) break;
      let index = 1;
      let sibling = current.previousElementSibling;
      while (sibling) {
        if (sibling.localName === tag) index += 1;
        sibling = sibling.previousElementSibling;
      }
      parts.unshift(`${tag}[${index}]`);
      current = current.parentElement;
    }
    return `/${parts.join("/")}`.slice(0, 1000);
  }

  function post(message) {
    try {
      chrome.runtime.sendMessage(message).catch(() => {});
    } catch {
      // 확장 업데이트 직후 메시지 포트가 닫힌 경우 다음 변화부터 다시 기록합니다.
    }
  }

  function send(type, payload = {}, timestamp = new Date().toISOString()) {
    post({
      kind: "recorder-event",
      event: {
        type,
        timestamp,
        url: currentUrl(),
        page_title: document.title,
        payload,
        element: {},
      },
    });
  }

  function flushMutations() {
    flushTimer = null;
    documentVersion += 1;
    send("dom_mutation", {
      document_version: documentVersion,
      first_change_ts: mutationBuffer.firstChangeTs,
      added_nodes: mutationBuffer.added,
      removed_nodes: mutationBuffer.removed,
      attribute_changes: mutationBuffer.attributes,
      character_data_changes: mutationBuffer.characterData,
      attribute_names: [...mutationBuffer.attributeNames].slice(0, 20),
      changed_target_count: mutationBuffer.xpaths.size,
      batches: mutationBuffer.batches,
    }, mutationBuffer.firstChangeTs);
    mutationBuffer.added = 0;
    mutationBuffer.removed = 0;
    mutationBuffer.attributes = 0;
    mutationBuffer.characterData = 0;
    mutationBuffer.attributeNames.clear();
    mutationBuffer.xpaths.clear();
    mutationBuffer.firstChangeTs = null;
    mutationBuffer.batches = [];
  }

  const observer = new MutationObserver((mutations) => {
    const timestamp = new Date().toISOString();
    const batch = { timestamp, added_nodes: 0, removed_nodes: 0,
      attribute_changes: 0, character_data_changes: 0, attribute_names: [] };
    if (!mutationBuffer.firstChangeTs) {
      mutationBuffer.firstChangeTs = new Date().toISOString();
    }
    for (const mutation of mutations) {
      mutationBuffer.added += mutation.addedNodes?.length || 0;
      mutationBuffer.removed += mutation.removedNodes?.length || 0;
      batch.added_nodes += mutation.addedNodes?.length || 0;
      batch.removed_nodes += mutation.removedNodes?.length || 0;
      if (mutation.type === "attributes") {
        mutationBuffer.attributes += 1;
        batch.attribute_changes += 1;
        if (mutation.attributeName) {
          mutationBuffer.attributeNames.add(mutation.attributeName);
          if (!batch.attribute_names.includes(mutation.attributeName)) batch.attribute_names.push(mutation.attributeName);
        }
      } else if (mutation.type === "characterData") {
        mutationBuffer.characterData += 1;
        batch.character_data_changes += 1;
      }
      const target =
        mutation.target instanceof Element
          ? mutation.target
          : mutation.target.parentElement;
      const targetXpath = xpath(target);
      if (targetXpath) mutationBuffer.xpaths.add(targetXpath);
    }
    mutationBuffer.batches.push(batch);
    if (flushTimer === null) flushTimer = setTimeout(flushMutations, 450);
  });

  // Since 0.4.0 the study copy runs inside the dashboard's iframe. The dashboard
  // mirrors the copy's title into the window title (NVDA+T, help level 1), so the
  // frame reports its title whenever it changes. A report the background cannot accept yet
  // (the frame loads before the readiness check creates the study state) is sent again at
  // the next tick, so a site whose title never changes still reaches the window title.
  let reportedTitle = null;
  function reportTitle() {
    if (isTop || document.title === reportedTitle) return;
    reportedTitle = document.title;
    try {
      chrome.runtime.sendMessage({ kind: "recorder-frame-title", title: document.title, url: currentUrl() })
        .then((reply) => { if (!reply?.accepted) reportedTitle = null; })
        .catch(() => { reportedTitle = null; });
    } catch {
      reportedTitle = null;
    }
  }

  function start() {
    send("page_ready", {
      kind: "content_script_ready",
      ready_state: document.readyState,
      language: document.documentElement.lang || "",
      in_frame: !isTop,
    });
    observer.observe(document.documentElement, {
      subtree: true,
      childList: true,
      attributes: true,
      characterData: true,
    });
    reportTitle();
    if (!isTop) setInterval(reportTitle, 1500);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
