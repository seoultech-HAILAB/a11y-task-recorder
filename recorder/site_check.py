"""Why an assigned site did or did not become ready (Windows pilot, 2026-10-28).

The dashboard reports each preparation as a `site_preparation` study event with the raw
extension reason. The recorder files a failure under a cause the researcher can act on,
prints one line in the server window, and lists every preparation in the result package.
The participant only hears the short "사이트에 접속할 수 없습니다" message with the label.
"""

import csv
import io
import sqlite3
from typing import Any, Dict, Optional

from recorder.common import json_load

SITE_CAUSES = {
    "login": "로그인 필요",
    "address": "주소 허용 안 됨",
    "timeout": "준비 시간 초과",
    "extension": "확장 응답 없음",
    "network": "사이트 연결 실패",
    "page": "사본 확인 실패",
    "recorder": "기록 서버 오류",
}
_REASON_CAUSES = {
    "login_required": "login",
    # The extension refused the catalog URL, the frame shows another address, or the bridge
    # was asked from a page that is not the recorder at 127.0.0.1:8765.
    "invalid_assignment_url": "address",
    "wrong_url": "address",
    "untrusted_origin": "address",
    "wrong_dashboard_tab": "address",
    # No answer from the extension (not loaded, reloaded, or the content script is missing).
    "extension_unavailable": "extension",
    "bridge_timeout": "extension",
    "tab_closed": "extension",
    "not_prepared": "extension",
    "invalid_command": "extension",
    # The page loaded but is not the copy the task expects.
    "capture_missing": "page",
    "capture_invalid": "page",
    "invalid_preparation": "page",
    "preparation_scope_mismatch": "page",
    "preparation_required": "page",
    "preparation_failed": "page",
    "collector_error": "recorder",
    "active_session_conflict": "recorder",
    "already_started": "recorder",
    "session_finished": "recorder",
    "not_assigned_task": "recorder",
}
# Chrome refused to show the frame (dashboard frame-src, the copy's frame-ancestors or
# X-Frame-Options); any other net error means the site itself could not be reached.
_BLOCKED_NET_ERRORS = frozenset({
    "net::ERR_BLOCKED_BY_CSP", "net::ERR_BLOCKED_BY_RESPONSE", "net::ERR_BLOCKED_BY_CLIENT",
    "net::ERR_BLOCKED_BY_ADMINISTRATOR",
})
CSV_FIELDS = ("timestamp", "tester", "order", "task_id", "session_id", "result", "cause", "cause_label",
              "reason", "login", "net_error", "stage", "elapsed_ms", "retry")


def site_cause(payload: Dict[str, Any]) -> str:
    """Cause code for a failed preparation; "timeout" when the site was still loading at the deadline."""
    reason = str(payload.get("reason") or "")
    # A refused code ends on the access page again, or on its error page (wrong_url).
    if reason == "login_required" or (payload.get("login") == "submitted" and reason == "wrong_url"):
        return "login"
    if reason == "frame_error":
        return "address" if payload.get("net_error") in _BLOCKED_NET_ERRORS else "network"
    return _REASON_CAUSES.get(reason, "timeout")


def classify(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Add cause and cause_label to a failed site_preparation payload."""
    if payload.get("result") != "failed":
        return payload
    cause = site_cause(payload)
    return {**payload, "cause": cause, "cause_label": SITE_CAUSES[cause]}


def failure_line(payload: Dict[str, Any], study: Optional[Dict[str, Any]]) -> str:
    """One server-window line for a failed preparation; no URL, host or code."""
    study = study or {}
    reason = str(payload.get("reason") or "")
    extra = ", ".join(str(value) for value in (payload.get("login"), payload.get("net_error")) if value)
    return "사이트 준비 실패: 테스터 {} 과업 {} · {} ({}{})".format(
        study.get("tester", "?"), study.get("order", "?"), payload.get("cause_label", ""),
        reason, ", " + extra if extra else "")


def preparation_csv(connection: sqlite3.Connection) -> bytes:
    """Every site_preparation event of every study session (draft sessions included)."""
    rows = connection.execute(
        "SELECT e.timestamp, e.payload, s.id AS session_id, s.study FROM events e"
        " JOIN sessions s ON s.id = e.session_id"
        " WHERE e.source = 'dashboard' AND e.type = 'site_preparation' ORDER BY e.timestamp, e.id"
    ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(CSV_FIELDS)
    for row in rows:
        payload = json_load(row["payload"], {})
        study = json_load(row["study"], {})
        record = {**payload, "timestamp": row["timestamp"], "session_id": row["session_id"],
                  "tester": study.get("tester", ""), "order": study.get("order", ""), "task_id": study.get("task_id", "")}
        writer.writerow([record.get(field, "") for field in CSV_FIELDS])
    return ("﻿" + output.getvalue()).encode("utf-8")


def failure_count(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        "SELECT payload FROM events WHERE source = 'dashboard' AND type = 'site_preparation'"
    ).fetchall()
    return sum(1 for row in rows if json_load(row["payload"], {}).get("result") == "failed")
