(() => {
  let preparationAttempted = false;
  let receipt = null;

  function inspect(message) {
    const assigned = new URL(message.targetUrl);
    const current = new URL(window.location.href);
    const loopback = assigned.protocol === "http:" && ["127.0.0.1", "localhost"].includes(assigned.hostname);
    // Our hosted copies live under /s/<id>/ over https (loopback http in rehearsals). Any other
    // http(s) start URL is a partner's test copy (catalog upload_url, 0.4.2): it has no capture
    // meta and no preparation steps, and the readiness check is the assigned URL having loaded.
    const hostedCopy = /^\/s\/[a-f0-9]{12}\/(?:index\.html|p\/[a-f0-9]{16}\.html)$/.test(assigned.pathname);
    if (!["http:", "https:"].includes(assigned.protocol) ||
        (hostedCopy && assigned.protocol !== "https:" && !loopback) ||
        (current.href !== assigned.href && current.href !== `${assigned.origin}/access`)) {
      return { ready: false, reason: "wrong_url" };
    }
    if (document.readyState !== "complete") return { ready: false, reason: "loading" };
    if (document.querySelector('form[action="/access"] input[type="password"]')) {
      return { ready: false, reason: "login_required" };
    }
    if (current.href !== assigned.href) return { ready: false, reason: "wrong_url" };
    if (!hostedCopy) {
      if (!Array.isArray(message.preparation)) return { ready: false, reason: "invalid_preparation" };
      if (message.preparation.length) return { ready: false, reason: "preparation_scope_mismatch" };
      return { ready: true, setup: {
        method: "operator_automation", actions: [], checked_at: new Date().toISOString(),
        excluded_from_participant_recording: true,
      } };
    }
    const meta = document.querySelector('meta[name="capture-source"]');
    if (!meta?.content) return { ready: false, reason: "capture_missing" };
    let source;
    try { source = new URL(meta.content); } catch { return { ready: false, reason: "capture_invalid" }; }
    if (!["http:", "https:"].includes(source.protocol)) return { ready: false, reason: "capture_invalid" };
    if (!Array.isArray(message.preparation)) return { ready: false, reason: "invalid_preparation" };
    if (!message.preparation.length) return { ready: true, setup: {
      method: "operator_automation", actions: [], checked_at: new Date().toISOString(),
      excluded_from_participant_recording: true,
    } };
    if (message.preparation.length !== 1 || source.origin !== "https://www.daisomall.co.kr" ||
        source.pathname !== "/ds/dst/SCR_DST_0015") return { ready: false, reason: "preparation_scope_mismatch" };
    const quantity = document.querySelector("#catalog-cart-quantity");
    const name = document.querySelector("#catalog-cart-name");
    const dialog = document.querySelector("#catalog-cart-dialog");
    const add = document.querySelector('.product-card[data-blux-item-id="1034450"] .button-item button');
    const open = document.querySelector("#catalog-cart-trigger");
    const increase = document.querySelector("#catalog-cart-increase");
    if (!quantity || !name || !dialog || !add || !open || !increase) return { ready: false, reason: "loading" };
    const prepared = () => quantity.value === "3" && name.textContent.trim() === "팬트리 수납 바구니 1호" && dialog.open;
    if (!prepared()) {
      if (!message.allowPreparation || preparationAttempted) return { ready: false, reason: "preparation_required" };
      preparationAttempted = true;
      // Use the hosted page's existing handlers without repairing its FE attributes.
      add.click();
      open.click();
      increase.click();
      increase.click();
      if (!prepared()) return { ready: false, reason: "preparation_failed" };
      receipt = { method: "operator_automation", actions: ["cart_add", "cart_open", "increase", "increase"],
        product_id: "1034450", quantity: 3, prepared_at: new Date().toISOString(),
        excluded_from_participant_recording: true };
    }
    return { ready: true, setup: receipt || { method: "operator_state_check", actions: [],
      product_id: "1034450", quantity: 3, checked_at: new Date().toISOString(),
      excluded_from_participant_recording: true } };
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.kind !== "a11y-study-page") return false;
    if (sender.id !== chrome.runtime.id || sender.tab) {
      sendResponse({ ready: false, reason: "untrusted_sender" });
      return false;
    }
    try { sendResponse(inspect(message)); }
    catch { sendResponse({ ready: false, reason: "preparation_failed" }); }
    return false;
  });
})();
