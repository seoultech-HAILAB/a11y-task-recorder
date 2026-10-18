// Practice screen (대본 §4.4): one button reveals the ingredients; nothing is recorded.
document.getElementById("show").addEventListener("click", () => {
  document.getElementById("ingredients").textContent = "재료는 두부, 대파, 간장입니다.";
});
