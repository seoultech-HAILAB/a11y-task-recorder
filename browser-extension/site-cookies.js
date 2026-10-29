// Runs in the page's own world, in the recorder's site frame only (0.5.8).
// A site in the frame is cross-site to the recorder page, so Chrome drops the cookies its
// scripts set (headless check 10/29: document.cookie stayed empty right after it was set).
// The CJ copy's cookie consent dialog therefore came back on every task (Windows 10/29, 4 of 4),
// while a visitor of the real site answers it once. Tasks start with that consent given.
// Only these names are added to what the page reads; nothing else of the page changes.
(() => {
  const RECORDER = "http://127.0.0.1:8765";
  const CONSENTS = { isCookieAgree: "1" };
  if (window === window.top || window.parent !== window.top || location.ancestorOrigins?.[0] !== RECORDER) return;
  const native = Object.getOwnPropertyDescriptor(Document.prototype, "cookie");
  if (!native?.get || !native.set) return;
  Object.defineProperty(Document.prototype, "cookie", {
    configurable: true,
    enumerable: native.enumerable,
    get() {
      const own = native.get.call(this);
      const names = new Set(own.split(";").map(part => part.trim().split("=")[0]).filter(Boolean));
      const added = Object.entries(CONSENTS).filter(([name]) => !names.has(name)).map(([name, value]) => `${name}=${value}`);
      return [own, ...added].filter(Boolean).join("; ");
    },
    set(value) { native.set.call(this, value); },
  });
})();
