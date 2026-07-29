const status = document.getElementById("status");
const marker = document.getElementById("marker");

function getStatus() {
  chrome.runtime.sendMessage({ kind: "recorder-status" }, (result) => {
    if (chrome.runtime.lastError || !result?.connected) {
      status.innerHTML =
        "<p><strong>기록 대기 중</strong>서버를 실행하고 대시보드에서 세션을 시작하세요.</p>";
      marker.disabled = true;
      return;
    }
    status.innerHTML = `<p><strong>기록 중</strong>${escapeHtml(result.session.title)}</p>`;
    marker.disabled = false;
  });
}

function escapeHtml(value) {
  const node = document.createElement("span");
  node.textContent = String(value ?? "");
  return node.innerHTML;
}

marker.addEventListener("click", () => {
  marker.disabled = true;
  chrome.runtime.sendMessage(
    { kind: "recorder-marker", label: "확장 팝업에서 불편 지점 표시" },
    (result) => {
      if (result?.recorded) {
        status.innerHTML = "<p><strong>표시했습니다</strong>과업을 계속 진행하세요.</p>";
      } else {
        status.innerHTML = "<p><strong>기록 실패</strong>서버와 세션 상태를 확인하세요.</p>";
      }
      setTimeout(getStatus, 1200);
    }
  );
});

getStatus();
