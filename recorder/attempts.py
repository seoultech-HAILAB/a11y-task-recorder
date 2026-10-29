"""Cancelled and repeated attempts of an assigned task: restart and tester change (0.4.4), and
저장한 과업 다시 하기 (0.5.7).

A participant who started a task by mistake restarts it, and a wrong tester number is
changed. Nothing is deleted: the attempt is closed as `aborted` and the next preparation
of the same task opens a new attempt (`study_attempts.retry_of`). Analysis excludes
aborted attempts; the result package lists every attempt in attempts.csv.

A saved task can be done again (0.5.7) when something went wrong after it was saved. The saved
attempt is kept as it is and marked `superseded` in attempts.csv; the new attempt carries the
reason. Analysis decides which attempt to use (a repeat has seen the task once already).
"""

import csv
import io
import sqlite3
from http import HTTPStatus
from typing import Any, Dict

from recorder.common import ApiError, json_load, utc_now

# reason -> (study_outcome, statuses it may close)
ABORT_REASONS = {
    "restart": ("restarted", frozenset({"active", "answering"})),
    "tester_change": ("cancelled", frozenset({"draft"})),
}
REDO_REASONS = {
    "technical": "기술 문제",
    "interrupted": "중간에 방해받음",
    "wrong_save": "결과를 잘못 저장함",
    "other": "기타",
}
SAVED_STATUSES = frozenset({"completed", "abandoned"})
ATTEMPT_FIELDS = (
    "tester", "order", "assignment_id", "attempt", "session_id", "retry_of", "status", "study_outcome",
    "analysis", "redo_reason", "abort_reason", "end_reason", "created_at", "started_at", "task_ended_at",
    "aborted_at", "net_task_seconds",
)


def _analysis(status: str, superseded: bool) -> str:
    if status == "aborted":
        return "exclude"
    return "superseded" if superseded else "include"


def _short(value: Any, limit: int = 40) -> str:
    return str(value or "")[:limit]


class AttemptsMixin:
    """Methods mixed into RecorderStore; relies on the StudyFlowMixin helpers."""

    def abort_task(self, session_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Close the current attempt as aborted; the next prepare opens a new attempt."""
        reason = str(data.get("reason") or "")
        if reason not in ABORT_REASONS:
            raise ApiError(HTTPStatus.BAD_REQUEST, "취소 사유가 올바르지 않습니다.")
        outcome, allowed = ABORT_REASONS[reason]
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._study_row(connection, session_id)
            if row["status"] not in allowed:
                raise ApiError(HTTPStatus.CONFLICT, "이 단계에서는 과업을 취소할 수 없습니다.")
            if row["end_reason"] == "time_limit":
                # The limit was reached; a restart would only buy the participant more time.
                raise ApiError(HTTPStatus.CONFLICT, "제한 시간으로 끝난 과업은 다시 시작할 수 없습니다.")
            study = json_load(row["study"], {})
            progress = self._study_progress(connection, self._study_catalog(connection), study["tester"])
            task = next((task for task in progress["tasks"] if task["order"] == study["order"]), None)
            if task is None or task["session_id"] != session_id:
                raise ApiError(HTTPStatus.CONFLICT, "현재 과업의 마지막 시도만 취소할 수 있습니다.")
            now = utc_now()
            if row["paused_at"]:
                self._close_pause(connection, row, now, auto=True)
            task_ended_at = row["task_ended_at"] or (now if row["started_at"] else None)
            net = self._net_seconds(connection, row, task_ended_at) if task_ended_at else None
            end_reason = row["end_reason"] or ("restart" if row["started_at"] else "")
            connection.execute(
                "UPDATE sessions SET status = 'aborted', ended_at = ?, task_ended_at = ?, end_reason = ?,"
                " net_task_seconds = ?, study_outcome = ?, aborted_at = ?, abort_reason = ? WHERE id = ?",
                (now, task_ended_at, end_reason, net, outcome, now, reason, session_id),
            )
            self._insert_dashboard_event(connection, session_id, "task_abort", {
                "reason": reason, "from_status": row["status"], "trigger": _short(data.get("trigger")),
                "by": _short(data.get("by") or "participant"), "previous_end_reason": row["end_reason"],
                "net_task_seconds": net, "to_tester": _short(data.get("to_tester")),
            }, now)
        return self.get_session(session_id)

    def redo_saved_task(self, tester: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """저장한 과업 다시 하기: a new attempt of a saved task; the saved one stays as it is."""
        reason = str(data.get("reason") or "")
        if reason not in REDO_REASONS:
            raise ApiError(HTTPStatus.BAD_REQUEST, "다시 하는 이유를 골라 주세요.")
        order = data.get("order")
        if type(order) is not int:
            raise ApiError(HTTPStatus.BAD_REQUEST, "다시 할 과업을 골라 주세요.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            catalog = self._study_catalog(connection)
            progress = self._study_progress(connection, catalog, tester)
            if connection.execute("SELECT id FROM sessions WHERE status IN ('active', 'answering') LIMIT 1").fetchone():
                raise ApiError(HTTPStatus.CONFLICT, "진행 중인 과업을 마친 뒤 다시 할 수 있습니다.")
            task = next((task for task in progress["tasks"] if task["order"] == order), None)
            source = connection.execute("SELECT * FROM sessions WHERE id = ?", (task["session_id"],)).fetchone() \
                if task and task["session_id"] else None
            if source is None or source["status"] not in SAVED_STATUSES:
                raise ApiError(HTTPStatus.CONFLICT, "저장한 과업만 다시 할 수 있습니다.")
            session_id = self._insert_attempt(connection, catalog, progress, tester, task, source, source["id"], reason)
            self._insert_dashboard_event(connection, source["id"], "task_redo", {
                "reason": reason, "reason_label": REDO_REASONS[reason], "new_session_id": session_id,
                "by": _short(data.get("by") or "participant"), "outcome": source["study_outcome"],
            })
        return self.get_session(session_id)

    @staticmethod
    def attempt_info(connection: sqlite3.Connection, session_id: str, status: str) -> Dict[str, Any]:
        """Where this session sits in its task's attempts, and whether analysis keeps it."""
        row = connection.execute("SELECT attempt, retry_of, redo_reason FROM study_attempts WHERE session_id = ?",
                                 (session_id,)).fetchone()
        if row is None:
            return {}
        following = connection.execute("SELECT session_id FROM study_attempts WHERE retry_of = ?",
                                       (session_id,)).fetchone()
        return {"attempt": row["attempt"], "retry_of": row["retry_of"], "redo_reason": row["redo_reason"],
                "next_attempt": following["session_id"] if following else None,
                "analysis": _analysis(status, bool(following) and status in SAVED_STATUSES)}


def attempts_csv(connection: sqlite3.Connection) -> bytes:
    """Every attempt of every assigned task, aborted ones marked analysis=exclude."""
    rows = connection.execute(
        "SELECT a.tester, a.assignment_id, a.attempt, a.retry_of, a.redo_reason, s.id AS session_id, s.study, s.status,"
        " EXISTS(SELECT 1 FROM study_attempts n WHERE n.retry_of = a.session_id) AS superseded,"
        " s.study_outcome, s.abort_reason, s.end_reason, s.created_at, s.started_at, s.task_ended_at,"
        " s.aborted_at, s.net_task_seconds FROM study_attempts a JOIN sessions s ON s.id = a.session_id"
        " ORDER BY a.tester, s.created_at, a.attempt"
    ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(ATTEMPT_FIELDS)
    for row in rows:
        record = dict(row)
        record["order"] = json_load(row["study"], {}).get("order", "")
        record["analysis"] = _analysis(row["status"], bool(row["superseded"]) and row["status"] in SAVED_STATUSES)
        writer.writerow(["" if record.get(field) is None else record.get(field) for field in ATTEMPT_FIELDS])
    return ("﻿" + output.getvalue()).encode("utf-8")


def aborted_count(connection: sqlite3.Connection) -> Dict[str, int]:
    rows = connection.execute(
        "SELECT abort_reason, COUNT(*) AS n FROM sessions WHERE status = 'aborted' GROUP BY abort_reason"
    ).fetchall()
    return {row["abort_reason"]: row["n"] for row in rows}

