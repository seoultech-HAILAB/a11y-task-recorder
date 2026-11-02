"""Free tasks (0.6.0): a site and a goal the tester chooses, recorded in a browser tab.

Assigned tasks stay the study's data; a free task is extra, optional, and marked
`study.kind = "free"` so analysis can keep it apart (memory construction, not FE
comparison or the SNC metrics). It is not part of the catalog: it has no study_attempts
row, so it never changes assignment progress, and it is offered only after every assigned
task is finished. The site opens in its own browser tab instead of the dashboard frame
(most real sites refuse to be framed), so the extension records that tab's top document.
"""

import json
import uuid
from http import HTTPStatus
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from recorder.common import ApiError, utc_now

FREE_KIND = "free"
FREE_UI_MODE = "free_tab"
MAX_URL_LENGTH = 2000
MAX_GOAL_LENGTH = 500
FREE_TIME_LIMIT_SECONDS = 600


def free_url(value: Any) -> str:
    """http(s) only, no credentials; query and fragment are allowed (real sites use them)."""
    url = str(value or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    try:
        parts = urlsplit(url)
        parts.port  # "javascript:alert(1)" would become host "javascript", port "alert(1)"
    except ValueError:
        raise ApiError(HTTPStatus.BAD_REQUEST, "사이트 주소를 확인해 주세요. 예: www.example.com") from None
    if (len(url) > MAX_URL_LENGTH or parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username or parts.password or any(ch.isspace() for ch in url)):
        raise ApiError(HTTPStatus.BAD_REQUEST, "사이트 주소를 확인해 주세요. 예: www.example.com")
    if parts.hostname in {"127.0.0.1", "localhost"} and parts.port == 8765:
        raise ApiError(HTTPStatus.BAD_REQUEST, "기록 화면 주소는 쓸 수 없습니다.")
    return url


class FreeTaskMixin:
    def current_free_task(self) -> Optional[Dict[str, Any]]:
        """Recover an unfinished free task, including its unsaved result form."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE status IN ('active', 'answering') AND study LIKE ?"
                " ORDER BY created_at DESC LIMIT 1", ('%"kind": "free"%',)
            ).fetchone()
        return self.session_from_row(row) if row else None

    def create_free_task(self, tester: str, data: Dict[str, Any]) -> Dict[str, Any]:
        if set(data) - {"url", "goal"}:
            raise ApiError(HTTPStatus.BAD_REQUEST, "사이트 주소와 하려는 일만 보낼 수 있습니다.")
        url = free_url(data.get("url"))
        goal = " ".join(str(data.get("goal") or "").split())
        if not goal or len(goal) > MAX_GOAL_LENGTH:
            raise ApiError(HTTPStatus.BAD_REQUEST, "하려는 일을 한 줄로 적어 주세요.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            catalog = self._study_catalog(connection)
            progress = self._study_progress(connection, catalog, tester)
            if progress["finished"] < progress["total"]:
                raise ApiError(HTTPStatus.CONFLICT, "배정 과업을 모두 마친 뒤에 자유 과업을 할 수 있습니다.")
            if connection.execute("SELECT 1 FROM sessions WHERE status IN ('active', 'answering') LIMIT 1").fetchone():
                raise ApiError(HTTPStatus.CONFLICT, "진행 중이거나 결과를 저장하지 않은 과업이 있습니다.")
            busy = connection.execute(
                "SELECT id FROM sessions WHERE status IN ('draft', 'active', 'answering') AND study LIKE ? LIMIT 1",
                ('%"kind": "free"%',)).fetchone()
            if busy:
                # One free task at a time; an unstarted one is replaced by the new choice.
                row = connection.execute("SELECT status FROM sessions WHERE id = ?", (busy["id"],)).fetchone()
                if row["status"] != "draft":
                    raise ApiError(HTTPStatus.CONFLICT, "진행 중인 자유 과업이 있습니다.")
                now = utc_now()
                connection.execute("UPDATE sessions SET status = 'aborted', ended_at = ?, aborted_at = ?,"
                                   " abort_reason = 'free_task_replaced' WHERE id = ?", (now, now, busy["id"]))
            count = connection.execute("SELECT COUNT(*) AS n FROM sessions WHERE participant = ? AND study LIKE ?"
                                       " AND status != 'aborted'", (tester, '%"kind": "free"%')).fetchone()["n"]
            session_id = str(uuid.uuid4())
            metadata = {"kind": FREE_KIND, "tester": tester, "free_index": count + 1, "catalog_id": catalog["id"],
                        "site_name": urlsplit(url).hostname, "time_limit_seconds": FREE_TIME_LIMIT_SECONDS,
                        "preparation": [], "requires_answer": False, "ui_mode": FREE_UI_MODE}
            connection.execute(
                "INSERT INTO sessions(id, title, participant, target_url, scenario, created_at, group_id, round, study) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (session_id, "자유 과업 {} ({})".format(count + 1, metadata["site_name"]), tester, url, goal,
                 utc_now(), session_id, json.dumps(metadata, ensure_ascii=False)),
            )
        return self.get_session(session_id)
