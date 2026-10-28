"""Start / end / repeat signals from the NVDA add-on gestures (0.4.0).

The add-on posts a signal; the server decides whether it applies to the current
assigned task and what NVDA should say. The dashboard polls the signal list and
performs the same action as its own buttons, so there is one executor.
"""

import os
import threading
from typing import Any, Dict, List, Optional

from recorder.common import utc_now

SIGNAL_KINDS = frozenset({"start", "end", "repeat"})
SIGNAL_SOURCES = frozenset({"nvda_addon", "facilitator", "dashboard"})
# "focus": the dashboard moves focus to the prompt and NVDA reads it. "nvda": the
# add-on speaks the prompt with ui.message. "both" does both (expect double speech).
ANNOUNCE_MODE = os.environ.get("A11Y_RECORDER_ANNOUNCE", "focus")
if ANNOUNCE_MODE not in {"focus", "nvda", "both"}:
    ANNOUNCE_MODE = "focus"
# Short sentences: the laptop plays NVDA through speakers in a shared room.
END_PROMPT = "과업이 끝났습니다. 결과를 입력해 주세요."
# 0.5.0 (participant feedback 10/28): NVDA+Ctrl+H reads the scenario sentence alone. How to end
# is taught once, in the practice (study.js PRACTICE_GUIDE), and never repeated after it.
# Same sentence as study.js PRACTICE_TASK.
PRACTICE_INSTRUCTION = "연습 화면에서 '재료 보기' 버튼을 눌러 첫 번째 재료를 확인하세요."
MAX_SIGNALS = 50


class SignalBoard:
    """In-memory queue of accepted signals plus the practice-mode flag."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._signals: List[Dict[str, Any]] = []
        self._seq = 0
        self.practice = False

    def decide(self, kind: str, source: str, session: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Return the add-on response and append the signal when it applies."""
        if kind not in SIGNAL_KINDS or source not in SIGNAL_SOURCES:
            return {"accepted": False, "reason": "invalid_signal", "speak": ""}
        if self.practice:
            speak = PRACTICE_INSTRUCTION if kind == "repeat" else (END_PROMPT if kind == "end" and ANNOUNCE_MODE != "focus" else "")
            return self._append(kind, source, None, speak, practice=True)
        status = session.get("status") if session else None
        if kind == "start" and status != "draft":
            return {"accepted": False, "reason": "no_prepared_task" if status is None else "not_draft", "speak": ""}
        if kind == "end" and status != "active":
            return {"accepted": False, "reason": "not_recording", "speak": ""}
        if kind == "repeat" and status not in {"draft", "active", "answering"}:
            return {"accepted": False, "reason": "no_task", "speak": ""}
        speak = ""
        if kind == "repeat":
            speak = str(session.get("scenario") or "").strip()
        elif kind == "end" and ANNOUNCE_MODE != "focus":
            speak = END_PROMPT
        return self._append(kind, source, session["id"], speak, practice=False)

    def _append(self, kind: str, source: str, session_id: Optional[str], speak: str, practice: bool) -> Dict[str, Any]:
        with self._lock:
            self._seq += 1
            signal = {"seq": self._seq, "kind": kind, "source": source, "session_id": session_id,
                      "at": utc_now(), "practice": practice}
            self._signals.append(signal)
            del self._signals[:-MAX_SIGNALS]
        return {"accepted": True, "speak": speak, "seq": signal["seq"], "announce_mode": ANNOUNCE_MODE}

    def since(self, after: int) -> Dict[str, Any]:
        with self._lock:
            return {"signals": [signal for signal in self._signals if signal["seq"] > after],
                    "seq": self._seq, "practice": self.practice, "announce_mode": ANNOUNCE_MODE}
