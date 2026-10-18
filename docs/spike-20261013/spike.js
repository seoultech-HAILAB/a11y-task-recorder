// Phase switcher for the spike: mirrors what study.js does in 0.4.0.
const top = document.getElementById("top");
const frame = document.getElementById("frame");
const result = document.getElementById("result");
const state = document.getElementById("state");
function shelve(region, on) {
  region.setAttribute("aria-hidden", on ? "true" : "false");
  for (const node of region.querySelectorAll("button, input, textarea, a, [tabindex]")) {
    if (on) { node.dataset.tab = node.getAttribute("tabindex") ?? ""; node.setAttribute("tabindex", "-1"); }
    else if (node.dataset.tab !== undefined) { node.dataset.tab ? node.setAttribute("tabindex", node.dataset.tab) : node.removeAttribute("tabindex"); delete node.dataset.tab; }
  }
}
function phase(name) {
  document.body.dataset.phase = name;
  const active = name === "active";
  shelve(top, active);
  const parked = name === "answering" || name === "ended";
  frame.toggleAttribute("inert", parked);
  frame.setAttribute("aria-hidden", parked ? "true" : "false");
  frame.tabIndex = parked ? -1 : 0;
  result.hidden = !(name === "answering" || name === "ended");
  state.textContent = "단계: " + ({ ready: "준비", active: "수행(머리 영역 aria-hidden)", answering: "결과 입력(프레임 inert)", ended: "저장됨" })[name];
  document.title = active ? "오늘의 요리: 두부조림 (프레임 제목 미러링)" : "스파이크: 한 창 기록기 (상위 문서)";
}
document.getElementById("read").addEventListener("click", () => document.getElementById("instruction").focus());
document.getElementById("start").addEventListener("click", () => { phase("active"); frame.focus(); });
document.getElementById("end").addEventListener("click", () => { phase("answering"); document.getElementById("result-heading").focus(); });
document.getElementById("reset").addEventListener("click", () => { phase("ready"); document.getElementById("saved").hidden = true; });
document.getElementById("form").addEventListener("submit", event => {
  event.preventDefault();
  phase("ended");
  const saved = document.getElementById("saved");
  saved.hidden = false;
  saved.textContent = "저장했습니다. 답: " + (document.getElementById("answer").value || "(없음)");
  saved.focus();
});
// The facilitator can also end with Ctrl+Alt+E from inside the top document only (not from the frame).
document.addEventListener("keydown", event => {
  if (event.ctrlKey && event.altKey && event.key.toLowerCase() === "e") document.getElementById("end").click();
});
phase("ready");
