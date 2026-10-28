// Practice screen (대본 §4.4): one button reveals the ingredients; nothing is recorded.
// The revealed line also says what to do next: participants in the 10/28 pilot looked for
// more of the page after the answer instead of ending (Windows pilot, issues 1 and 2).
document.getElementById("show").addEventListener("click", () => {
  document.getElementById("ingredients").textContent =
    "재료는 두부, 대파, 간장입니다. 첫 번째 재료를 찾았으면, 그 자리에서 NVDA, Ctrl, E를 함께 눌러 연습을 끝내세요.";
});
