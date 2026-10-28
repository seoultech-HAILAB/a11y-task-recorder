"""Single-window task flow (0.4.0): task end and resume, pauses, participant answers.

A study session now passes draft -> active -> answering -> completed/abandoned.
`active` is the only state in which collectors record; `answering` is the result
form. The mixin only adds methods to RecorderStore and uses its connection helpers.
"""

import json
import sqlite3
from http import HTTPStatus
from typing import Any, Dict, List, Optional

from recorder.common import ApiError, json_load, parse_timestamp, utc_now
from recorder.site_check import classify
from recorder.study import STUDY_OUTCOMES

END_REASONS = frozenset({
    "participant_gesture", "participant_button", "facilitator_gesture", "facilitator_button", "time_limit",
    "facilitator_stop",
})
# "system": the dashboard saved the result itself (time limit reached, no facilitator present).
ENTERED_BY = frozenset({"participant", "facilitator", "system"})
PAUSE_REASONS = frozenset({"break", "technical_support", "other"})
# Dashboard-only study events. They carry no participant speech or keys.
# site_preparation: how the assigned site became ready or why not; site_navigation: the
# recorder's own back / task start page commands inside the frame (Windows pilot, 10/28).
STUDY_EVENT_TYPES = frozenset({
    "signal", "instruction_presented", "instruction_repeat", "time_limit_reached", "help_level_1",
    "task_end", "task_resume", "pause", "resume", "answer_saved", "phase", "site_preparation", "site_navigation",
})
# Outcomes the participant form may choose; timeout/technical stay facilitator-only.
PARTICIPANT_OUTCOMES = frozenset({"completed", "blocked"})


def _seconds_between(start: str, end: str) -> float:
    return (parse_timestamp(end) - parse_timestamp(start)).total_seconds()


class StudyFlowMixin:
    """Methods mixed into RecorderStore; relies on connect(), get_session(), _study_progress()."""

    def _study_row(self, connection: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "세션을 찾을 수 없습니다.")
        if not json_load(row["study"], {}):
            raise ApiError(HTTPStatus.BAD_REQUEST, "배정 과업 세션에서만 쓸 수 있습니다.")
        return row

    @staticmethod
    def _insert_dashboard_event(connection: sqlite3.Connection, session_id: str, event_type: str,
                                payload: Dict[str, Any], timestamp: Optional[str] = None) -> int:
        cursor = connection.execute(
            "INSERT INTO events (session_id, timestamp, source, type, payload, url, page_title, element)"
            " VALUES (?, ?, 'dashboard', ?, ?, '', '', '{}')",
            (session_id, timestamp or utc_now(), event_type[:60], json.dumps(payload, ensure_ascii=False)),
        )
        return int(cursor.lastrowid)

    def log_study_event(self, session_id: str, event_type: str, payload: Any) -> Dict[str, Any]:
        """Record a dashboard-side study event regardless of the recording state."""
        if event_type not in STUDY_EVENT_TYPES:
            raise ApiError(HTTPStatus.BAD_REQUEST, "허용되지 않은 과업 이벤트 종류입니다.")
        clean = dict(payload) if isinstance(payload, dict) else {}
        if event_type == "site_preparation":
            clean = classify(clean)
        if len(json.dumps(clean, ensure_ascii=False)) > 4000:
            raise ApiError(HTTPStatus.BAD_REQUEST, "과업 이벤트가 너무 큽니다.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._study_row(connection, session_id)
            event_id = self._insert_dashboard_event(connection, session_id, event_type, clean)
            row = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self.event_from_row(row)

    def end_task(self, session_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Seal the participant's task interval; the result form follows in `answering`."""
        reason = str(data.get("reason") or "facilitator_button")
        if reason not in END_REASONS:
            raise ApiError(HTTPStatus.BAD_REQUEST, "종료 사유가 올바르지 않습니다.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._study_row(connection, session_id)
            if row["status"] != "active":
                raise ApiError(HTTPStatus.CONFLICT, "기록 중인 과업만 종료할 수 있습니다.")
            now = utc_now()
            if row["paused_at"]:
                self._close_pause(connection, row, now, auto=True)
            connection.execute(
                "UPDATE sessions SET status = 'answering', task_ended_at = ?, end_reason = ? WHERE id = ?",
                (now, reason, session_id),
            )
            self._insert_dashboard_event(connection, session_id, "task_end", {
                "reason": reason, "net_task_seconds": self._net_seconds(connection, row, now),
            }, now)
        return self.get_session(session_id)

    def resume_task(self, session_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Return from the result form to the task after an accidental end."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._study_row(connection, session_id)
            if row["status"] != "answering":
                raise ApiError(HTTPStatus.CONFLICT, "결과 입력 단계의 과업만 다시 시작할 수 있습니다.")
            other = connection.execute(
                "SELECT id FROM sessions WHERE status = 'active' AND id != ? LIMIT 1", (session_id,)
            ).fetchone()
            if other:
                raise ApiError(HTTPStatus.CONFLICT, "다른 세션이 기록 중입니다.")
            now = utc_now()
            self._insert_dashboard_event(connection, session_id, "task_resume", {
                "previous_task_ended_at": row["task_ended_at"], "previous_end_reason": row["end_reason"],
                "gap_seconds": round(_seconds_between(row["task_ended_at"], now), 2) if row["task_ended_at"] else None,
                "by": str(data.get("by") or "facilitator")[:40],
            }, now)
            connection.execute(
                "UPDATE sessions SET status = 'active', task_ended_at = NULL, end_reason = '' WHERE id = ?",
                (session_id,),
            )
        return self.get_session(session_id)

    def pause_task(self, session_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        reason = str(data.get("reason") or "other")
        if reason not in PAUSE_REASONS:
            raise ApiError(HTTPStatus.BAD_REQUEST, "일시정지 사유가 올바르지 않습니다.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._study_row(connection, session_id)
            if row["status"] != "active":
                raise ApiError(HTTPStatus.CONFLICT, "기록 중인 과업만 일시정지할 수 있습니다.")
            if row["paused_at"]:
                raise ApiError(HTTPStatus.CONFLICT, "이미 일시정지 중입니다.")
            now = utc_now()
            connection.execute("UPDATE sessions SET paused_at = ?, pause_reason = ? WHERE id = ?",
                               (now, reason, session_id))
            self._insert_dashboard_event(connection, session_id, "pause", {"reason": reason}, now)
        return self.get_session(session_id)

    def resume_pause(self, session_id: str) -> Dict[str, Any]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._study_row(connection, session_id)
            if not row["paused_at"]:
                raise ApiError(HTTPStatus.CONFLICT, "일시정지 중이 아닙니다.")
            self._close_pause(connection, row, utc_now(), auto=False)
        return self.get_session(session_id)

    def _close_pause(self, connection: sqlite3.Connection, row: sqlite3.Row, now: str, auto: bool) -> None:
        self._insert_dashboard_event(connection, row["id"], "resume", {
            "reason": row["pause_reason"], "paused_seconds": round(_seconds_between(row["paused_at"], now), 2),
            "auto": auto,
        }, now)
        connection.execute("UPDATE sessions SET paused_at = NULL, pause_reason = '' WHERE id = ?", (row["id"],))

    def _paused_seconds(self, connection: sqlite3.Connection, session_id: str) -> float:
        rows = connection.execute(
            "SELECT payload FROM events WHERE session_id = ? AND source = 'dashboard' AND type = 'resume'",
            (session_id,),
        ).fetchall()
        return sum(float(json_load(row["payload"], {}).get("paused_seconds") or 0) for row in rows)

    def _net_seconds(self, connection: sqlite3.Connection, row: sqlite3.Row, task_ended_at: str) -> Optional[float]:
        if not row["started_at"]:
            return None
        gross = _seconds_between(row["started_at"], task_ended_at)
        return round(max(0.0, gross - self._paused_seconds(connection, row["id"])), 2)

    def finalize_task_timing(self, connection: sqlite3.Connection, session_id: str, ended_at: str) -> None:
        """Called by stop_session: fix task_ended_at when stop came straight from `active`."""
        row = connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        task_ended_at = row["task_ended_at"] or ended_at
        if row["paused_at"]:
            self._close_pause(connection, row, ended_at, auto=True)
        connection.execute(
            "UPDATE sessions SET task_ended_at = ?, end_reason = CASE WHEN end_reason = '' THEN 'facilitator_stop'"
            " ELSE end_reason END, net_task_seconds = ? WHERE id = ?",
            (task_ended_at, self._net_seconds(connection, row, task_ended_at), session_id),
        )

    @staticmethod
    def answer_entry(data: Dict[str, Any], outcome: str) -> Dict[str, Any]:
        entered_by = str(data.get("entered_by") or "facilitator")
        if entered_by not in ENTERED_BY:
            raise ApiError(HTTPStatus.BAD_REQUEST, "입력자 값이 올바르지 않습니다.")
        answer = str(data.get("answer", ""))
        if len(answer) > 4000:
            raise ApiError(HTTPStatus.BAD_REQUEST, "답이 너무 깁니다.")
        return {"at": utc_now(), "outcome": outcome, "answer": answer, "entered_by": entered_by}

    def record_answer(self, connection: sqlite3.Connection, session_id: str, entry: Dict[str, Any]) -> None:
        row = connection.execute("SELECT answer_history FROM sessions WHERE id = ?", (session_id,)).fetchone()
        history: List[Dict[str, Any]] = json_load(row["answer_history"], [])
        history.append(entry)
        connection.execute(
            "UPDATE sessions SET study_outcome = ?, study_answer = ?, study_answer_entered_by = ?,"
            " answer_history = ? WHERE id = ?",
            (entry["outcome"], entry["answer"], entry["entered_by"],
             json.dumps(history[-50:], ensure_ascii=False), session_id),
        )
        self._insert_dashboard_event(connection, session_id, "answer_saved", {
            "outcome": entry["outcome"], "entered_by": entry["entered_by"], "revision": len(history),
        }, entry["at"])

    def update_answer(self, session_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Correct a saved participant answer until the next task has been prepared."""
        outcome = data.get("study_outcome")
        if outcome not in STUDY_OUTCOMES:
            raise ApiError(HTTPStatus.BAD_REQUEST, "과업 종료 결과를 선택해 주세요.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._study_row(connection, session_id)
            if row["status"] not in {"completed", "abandoned"}:
                raise ApiError(HTTPStatus.CONFLICT, "저장된 결과만 고칠 수 있습니다.")
            if {row["study_outcome"], outcome} - PARTICIPANT_OUTCOMES and row["study_outcome"] != outcome:
                raise ApiError(HTTPStatus.CONFLICT, "완료 여부만 바꿀 수 있습니다. 시간 종료·기술 문제는 바꿀 수 없습니다.")
            study = json_load(row["study"], {})
            progress = self._study_progress(connection, self._study_catalog(connection), study["tester"])
            later = [task for task in progress["tasks"]
                     if task["order"] > study["order"] and task["session_id"]]
            if later:
                raise ApiError(HTTPStatus.CONFLICT, "다음 과업이 준비되어 결과를 고칠 수 없습니다.")
            entry = self.answer_entry(data, outcome)
            self.record_answer(connection, session_id, entry)
            connection.execute("UPDATE sessions SET status = ? WHERE id = ?",
                               ("completed" if outcome == "completed" else "abandoned", session_id))
        return self.get_session(session_id)
