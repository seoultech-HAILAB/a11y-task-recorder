(() => {
  const pageUrl = new URL(window.location.href);
  if (
    ["127.0.0.1", "localhost"].includes(pageUrl.hostname) &&
    pageUrl.port === "8765"
  ) {
    return;
  }

  const mutationBuffer = {
    added: 0,
    removed: 0,
    attributes: 0,
    characterData: 0,
    attributeNames: new Set(),
    xpaths: new Set(),
  };
  let flushTimer = null;
  let documentVersion = 0;

  function sanitizedUrl() {
    return `${window.location.origin}${window.location.pathname}`;
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

  function send(type, payload = {}) {
    try {
      chrome.runtime.sendMessage({
        kind: "recorder-event",
        event: {
          type,
          timestamp: new Date().toISOString(),
          url: sanitizedUrl(),
          page_title: document.title,
          payload,
          element: {},
        },
      });
    } catch {
      // 확장 업데이트 직후 메시지 포트가 닫힌 경우 다음 변화부터 다시 기록합니다.
    }
  }

  function flushMutations() {
    flushTimer = null;
    documentVersion += 1;
    send("dom_mutation", {
      document_version: documentVersion,
      added_nodes: mutationBuffer.added,
      removed_nodes: mutationBuffer.removed,
      attribute_changes: mutationBuffer.attributes,
      character_data_changes: mutationBuffer.characterData,
      attribute_names: [...mutationBuffer.attributeNames].slice(0, 20),
      changed_xpaths: [...mutationBuffer.xpaths].slice(0, 20),
    });
    mutationBuffer.added = 0;
    mutationBuffer.removed = 0;
    mutationBuffer.attributes = 0;
    mutationBuffer.characterData = 0;
    mutationBuffer.attributeNames.clear();
    mutationBuffer.xpaths.clear();
  }

  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      mutationBuffer.added += mutation.addedNodes?.length || 0;
      mutationBuffer.removed += mutation.removedNodes?.length || 0;
      if (mutation.type === "attributes") {
        mutationBuffer.attributes += 1;
        if (mutation.attributeName) {
          mutationBuffer.attributeNames.add(mutation.attributeName);
        }
      } else if (mutation.type === "characterData") {
        mutationBuffer.characterData += 1;
      }
      const target =
        mutation.target instanceof Element
          ? mutation.target
          : mutation.target.parentElement;
      const targetXpath = xpath(target);
      if (targetXpath) mutationBuffer.xpaths.add(targetXpath);
    }
    clearTimeout(flushTimer);
    flushTimer = setTimeout(flushMutations, 450);
  });

  function start() {
    send("page_ready", {
      kind: "content_script_ready",
      ready_state: document.readyState,
      language: document.documentElement.lang || "",
    });
    observer.observe(document.documentElement, {
      subtree: true,
      childList: true,
      attributes: true,
      characterData: true,
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
