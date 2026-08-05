import argparse
import csv
import io
import json
import mimetypes
import os
import re
import sqlite3
import sys
import tempfile
import threading
import uuid
import webbrowser
import zipfile
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse


# 애드온 폴링 주기(1.25초)의 여유 배수. 이 시간 안에 호출이 없으면 미연결로 본다.
NVDA_LIVENESS_SECONDS = 10
BROWSER_CLIENT_HEADER = "X-A11y-Recorder-Client"
BROWSER_CLIENT_ID = "a11y-recorder-cft-v1"

ROOT_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT_DIR / "static"
DEFAULT_DB = ROOT_DIR / "data" / "recorder.sqlite3"
SESSION_FIELDS = {
    "title",
    "participant",
    "target_url",
    "scenario",
    "expected_announcement",
    "prior_site_experience",
    "environment",
    "notes",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_timestamp(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def json_load(value: Optional[str], fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class RecorderStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        # 명시적으로 close하지 않은 연결은 내부 statement cache와의 참조 순환
        # 때문에 GC 전까지 살아남아 Windows에서 DB 파일 잠금을 유지한다.
        connection = sqlite3.connect(str(self.db_path), timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._schema_lock, self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    participant TEXT NOT NULL DEFAULT '',
                    target_url TEXT NOT NULL DEFAULT '',
                    scenario TEXT NOT NULL DEFAULT '',
                    expected_announcement TEXT NOT NULL DEFAULT '',
                    prior_site_experience TEXT NOT NULL DEFAULT '',
                    environment TEXT NOT NULL DEFAULT '{}',
                    notes TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    ended_at TEXT
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    timestamp TEXT NOT NULL,
                    source TEXT NOT NULL,
                    type TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    url TEXT NOT NULL DEFAULT '',
                    page_title TEXT NOT NULL DEFAULT '',
                    element TEXT NOT NULL DEFAULT '{}',
                    step_id TEXT,
                    speech_end_ts TEXT,
                    interrupted INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_events_session_time
                    ON events(session_id, timestamp, id);

                CREATE TABLE IF NOT EXISTS steps (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    instructions TEXT NOT NULL DEFAULT '',
                    expected_announcement TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    started_at TEXT,
                    ended_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, position)
                );

                CREATE INDEX IF NOT EXISTS idx_steps_session_position
                    ON steps(session_id, position);

                CREATE TABLE IF NOT EXISTS issues (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    summary TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    expected_announcement TEXT NOT NULL DEFAULT '',
                    severity TEXT NOT NULL DEFAULT 'moderate',
                    tags TEXT NOT NULL DEFAULT '[]',
                    start_event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
                    end_event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
                    start_step_id TEXT,
                    end_step_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_issues_session
                    ON issues(session_id, created_at);
                """
            )
            self._ensure_column(
                connection,
                "sessions",
                "prior_site_experience",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(connection, "events", "step_id", "TEXT")
            self._ensure_column(connection, "events", "speech_end_ts", "TEXT")
            self._ensure_column(
                connection,
                "events",
                "interrupted",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(connection, "issues", "start_step_id", "TEXT")
            self._ensure_column(connection, "issues", "end_step_id", "TEXT")
            # 같은 과업의 반복 수행(회차)을 묶는 그룹 식별자와 회차 번호
            self._ensure_column(connection, "sessions", "group_id", "TEXT")
            self._ensure_column(
                connection, "sessions", "round", "INTEGER NOT NULL DEFAULT 1"
            )
            connection.execute("UPDATE sessions SET group_id = id WHERE group_id IS NULL")
            # step별 수행 결과(G_User): complete / assisted / blocked + 사유
            self._ensure_column(connection, "steps", "outcome", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(
                connection, "steps", "outcome_note", "TEXT NOT NULL DEFAULT ''"
            )

    @staticmethod
    def _apply_default_outcomes(connection: sqlite3.Connection, session_id: str) -> None:
        # 완료됐지만 결과 미지정인 step: 힌트가 연결됐으면 assisted, 아니면 complete.
        connection.execute(
            """
            UPDATE steps SET outcome = CASE
                WHEN EXISTS (
                    SELECT 1 FROM events
                    WHERE events.step_id = steps.id AND events.type = 'hint'
                ) THEN 'assisted'
                ELSE 'complete'
            END
            WHERE session_id = ? AND status = 'completed' AND outcome = ''
            """,
            (session_id,),
        )

    def update_step_outcome(
        self, session_id: str, step_id: str, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        outcome = str(data.get("outcome", "")).strip()
        if outcome not in {"", "complete", "assisted", "blocked"}:
            raise ApiError(HTTPStatus.BAD_REQUEST, "step 결과 코드가 올바르지 않습니다.")
        note = str(data.get("outcome_note", "")).strip()
        with self.connect() as connection:
            step = connection.execute(
                "SELECT id FROM steps WHERE id = ? AND session_id = ?",
                (step_id, session_id),
            ).fetchone()
            if not step:
                raise ApiError(HTTPStatus.NOT_FOUND, "step을 찾을 수 없습니다.")
            connection.execute(
                "UPDATE steps SET outcome = ?, outcome_note = ? WHERE id = ?",
                (outcome, note, step_id),
            )
            row = connection.execute(
                "SELECT * FROM steps WHERE id = ?", (step_id,)
            ).fetchone()
        return self.step_from_row(row)

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, declaration: str
    ) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info({})".format(table)).fetchall()
        }
        if column not in columns:
            connection.execute(
                "ALTER TABLE {} ADD COLUMN {} {}".format(table, column, declaration)
            )

    @staticmethod
    def session_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["environment"] = json_load(result.get("environment"), {})
        return result

    @staticmethod
    def event_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["payload"] = json_load(result.get("payload"), {})
        result["element"] = json_load(result.get("element"), {})
        result["interrupted"] = bool(result.get("interrupted"))
        return result

    @staticmethod
    def issue_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["tags"] = json_load(result.get("tags"), [])
        return result

    @staticmethod
    def step_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_session(self, data: Dict[str, Any]) -> Dict[str, Any]:
        title = str(data.get("title", "")).strip()
        if not title:
            raise ApiError(HTTPStatus.BAD_REQUEST, "시나리오 제목을 입력해 주세요.")
        session_id = str(uuid.uuid4())
        created_at = utc_now()
        environment = data.get("environment") if isinstance(data.get("environment"), dict) else {}
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    id, title, participant, target_url, scenario,
                    expected_announcement, prior_site_experience,
                    environment, notes, created_at, group_id, round
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    session_id,
                    title,
                    str(data.get("participant", "")).strip(),
                    str(data.get("target_url", "")).strip(),
                    str(data.get("scenario", "")).strip(),
                    str(data.get("expected_announcement", "")).strip(),
                    str(data.get("prior_site_experience", "")).strip(),
                    json.dumps(environment, ensure_ascii=False),
                    str(data.get("notes", "")).strip(),
                    created_at,
                    session_id,
                ),
            )
        return self.get_session(session_id)

    def rerun_session(self, session_id: str) -> Dict[str, Any]:
        """완료·중단된 세션의 과업 설정을 복사해 다음 회차 세션을 만든다."""
        source = self.get_session(session_id)
        if source["status"] not in {"completed", "abandoned"}:
            raise ApiError(
                HTTPStatus.BAD_REQUEST, "완료되거나 중단된 세션만 다음 회차를 만들 수 있습니다."
            )
        group_id = source.get("group_id") or source["id"]
        new_id = str(uuid.uuid4())
        created_at = utc_now()
        with self.connect() as connection:
            max_round = connection.execute(
                "SELECT MAX(round) AS max_round FROM sessions WHERE group_id = ?",
                (group_id,),
            ).fetchone()["max_round"] or 1
            connection.execute(
                """
                INSERT INTO sessions (
                    id, title, participant, target_url, scenario,
                    expected_announcement, prior_site_experience,
                    environment, notes, created_at, group_id, round
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id,
                    source["title"],
                    source.get("participant", ""),
                    source.get("target_url", ""),
                    source.get("scenario", ""),
                    source.get("expected_announcement", ""),
                    source.get("prior_site_experience", ""),
                    json.dumps(source.get("environment", {}), ensure_ascii=False),
                    "",
                    created_at,
                    group_id,
                    max_round + 1,
                ),
            )
            for step in source.get("steps", []):
                connection.execute(
                    """
                    INSERT INTO steps (
                        id, session_id, position, title, instructions,
                        expected_announcement, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        new_id,
                        step.get("position", 0),
                        step.get("title", ""),
                        step.get("instructions", ""),
                        step.get("expected_announcement", ""),
                        created_at,
                    ),
                )
        return self.get_session(new_id)

    def list_sessions(self) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sessions ORDER BY created_at DESC"
            ).fetchall()
        sessions = [self.session_from_row(row) for row in rows]
        for session in sessions:
            session["summary"] = self.session_summary(session["id"])
        return sessions

    def get_session(self, session_id: str, include_related: bool = False) -> Dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "세션을 찾을 수 없습니다.")
        result = self.session_from_row(row)
        result["summary"] = self.session_summary(session_id)
        if include_related:
            result["events"] = self.list_events(session_id)
            result["issues"] = self.list_issues(session_id)
            result["steps"] = self.list_steps(session_id)
        else:
            result["steps"] = self.list_steps(session_id)
        group_id = result.get("group_id") or session_id
        with self.connect() as connection:
            round_rows = connection.execute(
                "SELECT id, round, status FROM sessions WHERE group_id = ?"
                " ORDER BY round ASC, created_at ASC",
                (group_id,),
            ).fetchall()
        result["rounds"] = [dict(item) for item in round_rows]
        return result

    def active_session(self) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE status = 'active' ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        return self.session_from_row(row) if row else None

    def update_session(self, session_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        assignments = []
        values: List[Any] = []
        for field in SESSION_FIELDS:
            if field not in data:
                continue
            assignments.append("{} = ?".format(field))
            value = data[field]
            if field == "environment":
                value = json.dumps(value if isinstance(value, dict) else {}, ensure_ascii=False)
            else:
                value = str(value)
            values.append(value)
        environment_merge = (
            data.get("environment_merge")
            if isinstance(data.get("environment_merge"), dict)
            else None
        )
        if assignments or environment_merge:
            values.append(session_id)
            with self.connect() as connection:
                if assignments:
                    cursor = connection.execute(
                        "UPDATE sessions SET {} WHERE id = ?".format(", ".join(assignments)),
                        values,
                    )
                else:
                    cursor = connection.execute(
                        "SELECT id FROM sessions WHERE id = ?", (session_id,)
                    )
                missing = (
                    cursor.fetchone() is None
                    if not assignments
                    else cursor.rowcount == 0
                )
                if missing:
                    raise ApiError(HTTPStatus.NOT_FOUND, "세션을 찾을 수 없습니다.")
                if environment_merge:
                    row = connection.execute(
                        "SELECT environment FROM sessions WHERE id = ?", (session_id,)
                    ).fetchone()
                    if row is None:
                        raise ApiError(HTTPStatus.NOT_FOUND, "세션을 찾을 수 없습니다.")
                    environment = json_load(row["environment"], {})
                    environment.update(environment_merge)
                    connection.execute(
                        "UPDATE sessions SET environment = ? WHERE id = ?",
                        (json.dumps(environment, ensure_ascii=False), session_id),
                    )
        return self.get_session(session_id)

    def start_session(self, session_id: str) -> Dict[str, Any]:
        with self.connect() as connection:
            active = connection.execute(
                "SELECT id, title FROM sessions WHERE status = 'active' AND id != ? LIMIT 1",
                (session_id,),
            ).fetchone()
            if active:
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "이미 진행 중인 세션이 있습니다: {}".format(active["title"]),
                )
            cursor = connection.execute(
                """
                UPDATE sessions
                SET status = 'active', started_at = ?, ended_at = NULL
                WHERE id = ? AND status = 'draft'
                """,
                (utc_now(), session_id),
            )
            if cursor.rowcount == 0:
                existing = connection.execute(
                    "SELECT id FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not existing:
                    raise ApiError(HTTPStatus.NOT_FOUND, "세션을 찾을 수 없습니다.")
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "초안 상태의 세션만 시작할 수 있습니다. 새 평가에는 새 세션을 만들어 주세요.",
                )
        return self.get_session(session_id)

    def stop_session(self, session_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        final_status = data.get("status", "completed")
        if final_status not in {"completed", "abandoned"}:
            raise ApiError(HTTPStatus.BAD_REQUEST, "종료 상태가 올바르지 않습니다.")
        notes = data.get("notes")
        with self.connect() as connection:
            ended_at = utc_now()
            active_step = connection.execute(
                """
                SELECT * FROM steps
                WHERE session_id = ? AND status = 'active'
                ORDER BY position DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if notes is None:
                cursor = connection.execute(
                    "UPDATE sessions SET status = ?, ended_at = ? WHERE id = ? AND status = 'active'",
                    (final_status, ended_at, session_id),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE sessions SET status = ?, ended_at = ?, notes = ?
                    WHERE id = ? AND status = 'active'
                    """,
                    (final_status, ended_at, str(notes), session_id),
                )
            if cursor.rowcount == 0:
                raise ApiError(HTTPStatus.CONFLICT, "진행 중인 세션만 종료할 수 있습니다.")
            connection.execute(
                """
                UPDATE steps SET status = 'completed', ended_at = ?
                WHERE session_id = ? AND status = 'active'
                """,
                (ended_at, session_id),
            )
            self._apply_default_outcomes(connection, session_id)
            if active_step:
                connection.execute(
                    """
                    INSERT INTO events (
                        session_id, timestamp, source, type, payload, step_id
                    ) VALUES (?, ?, 'dashboard', 'step_end', ?, ?)
                    """,
                    (
                        session_id,
                        ended_at,
                        json.dumps(
                            {
                                "step_id": active_step["id"],
                                "position": active_step["position"],
                                "title": active_step["title"],
                                "reason": "session_stopped",
                            },
                            ensure_ascii=False,
                        ),
                        active_step["id"],
                    ),
                )
        return self.get_session(session_id)

    def create_step(self, session_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        title = str(data.get("title", "")).strip()
        if not title:
            raise ApiError(HTTPStatus.BAD_REQUEST, "step 제목을 입력해 주세요.")
        with self.connect() as connection:
            session = connection.execute(
                "SELECT id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not session:
                raise ApiError(HTTPStatus.NOT_FOUND, "세션을 찾을 수 없습니다.")
            position = connection.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 AS value FROM steps WHERE session_id = ?",
                (session_id,),
            ).fetchone()["value"]
            step_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO steps (
                    id, session_id, position, title, instructions,
                    expected_announcement, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    step_id,
                    session_id,
                    position,
                    title,
                    str(data.get("instructions", "")).strip(),
                    str(data.get("expected_announcement", "")).strip(),
                    utc_now(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM steps WHERE id = ?", (step_id,)
            ).fetchone()
        return self.step_from_row(row)

    def list_steps(self, session_id: str) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM steps WHERE session_id = ? ORDER BY position ASC",
                (session_id,),
            ).fetchall()
        return [self.step_from_row(row) for row in rows]

    def transition_step(
        self, session_id: str, step_id: str, action: str
    ) -> Dict[str, Any]:
        if action not in {"start", "finish"}:
            raise ApiError(HTTPStatus.BAD_REQUEST, "step 동작이 올바르지 않습니다.")
        now = utc_now()
        previous_active = None
        with self.connect() as connection:
            session = connection.execute(
                "SELECT status FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not session:
                raise ApiError(HTTPStatus.NOT_FOUND, "세션을 찾을 수 없습니다.")
            if session["status"] != "active":
                raise ApiError(HTTPStatus.CONFLICT, "기록 중인 세션에서만 step을 변경할 수 있습니다.")
            step = connection.execute(
                "SELECT * FROM steps WHERE id = ? AND session_id = ?",
                (step_id, session_id),
            ).fetchone()
            if not step:
                raise ApiError(HTTPStatus.NOT_FOUND, "step을 찾을 수 없습니다.")
            if action == "start":
                previous_active = connection.execute(
                    """
                    SELECT * FROM steps
                    WHERE session_id = ? AND status = 'active' AND id != ?
                    ORDER BY position DESC LIMIT 1
                    """,
                    (session_id, step_id),
                ).fetchone()
                connection.execute(
                    """
                    UPDATE steps SET status = 'completed', ended_at = ?
                    WHERE session_id = ? AND status = 'active' AND id != ?
                    """,
                    (now, session_id, step_id),
                )
                connection.execute(
                    """
                    UPDATE steps
                    SET status = 'active', started_at = COALESCE(started_at, ?), ended_at = NULL
                    WHERE id = ?
                    """,
                    (now, step_id),
                )
                event_type = "step_start"
            else:
                connection.execute(
                    "UPDATE steps SET status = 'completed', ended_at = ? WHERE id = ?",
                    (now, step_id),
                )
                event_type = "step_end"
            self._apply_default_outcomes(connection, session_id)
        if previous_active:
            self.add_event(
                {
                    "session_id": session_id,
                    "source": "dashboard",
                    "type": "step_end",
                    "timestamp": now,
                    "step_id": previous_active["id"],
                    "payload": {
                        "step_id": previous_active["id"],
                        "position": previous_active["position"],
                        "title": previous_active["title"],
                        "reason": "next_step_started",
                    },
                }
            )
        self.add_event(
            {
                "session_id": session_id,
                "source": "dashboard",
                "type": event_type,
                "timestamp": now,
                "step_id": step_id,
                "payload": {
                    "step_id": step_id,
                    "position": step["position"],
                    "title": step["title"],
                },
            }
        )
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM steps WHERE id = ?", (step_id,)
            ).fetchone()
        return self.step_from_row(row)

    def add_hint(self, session_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        step_id = str(data.get("step_id", "")).strip() or None
        if not step_id:
            with self.connect() as connection:
                active = connection.execute(
                    """
                    SELECT id FROM steps
                    WHERE session_id = ? AND status = 'active'
                    ORDER BY position DESC LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
            step_id = active["id"] if active else None
        return self.add_event(
            {
                "session_id": session_id,
                "source": "dashboard",
                "type": "hint",
                "timestamp": data.get("timestamp") or utc_now(),
                "step_id": step_id,
                "payload": {
                    "text": str(data.get("text", "")).strip(),
                    "kind": str(data.get("kind", "verbal")).strip() or "verbal",
                },
            }
        )

    def add_event(self, data: Dict[str, Any]) -> Dict[str, Any]:
        session_id = str(data.get("session_id") or data.get("sessionId") or "").strip()
        if not session_id:
            active = self.active_session()
            if not active:
                raise ApiError(HTTPStatus.CONFLICT, "진행 중인 세션이 없습니다.")
            session_id = active["id"]
        source = str(data.get("source", "")).strip()
        event_type = str(data.get("type", "")).strip()
        if not source or not event_type:
            raise ApiError(HTTPStatus.BAD_REQUEST, "이벤트 source와 type이 필요합니다.")
        try:
            timestamp = parse_timestamp(data.get("timestamp")).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")
        except (TypeError, ValueError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "이벤트 timestamp 형식이 올바르지 않습니다.")
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        element = data.get("element") if isinstance(data.get("element"), dict) else {}
        speech_end_ts = None
        if data.get("speech_end_ts"):
            try:
                speech_end_ts = parse_timestamp(data.get("speech_end_ts")).isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z")
            except (TypeError, ValueError):
                raise ApiError(
                    HTTPStatus.BAD_REQUEST, "speech_end_ts 형식이 올바르지 않습니다."
                )
            if parse_timestamp(speech_end_ts) < parse_timestamp(timestamp):
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "speech_end_ts는 이벤트 시작 시각보다 빠를 수 없습니다.",
                )
        step_id = str(data.get("step_id", "")).strip() or None
        with self.connect() as connection:
            session = connection.execute(
                "SELECT status FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not session:
                raise ApiError(HTTPStatus.NOT_FOUND, "세션을 찾을 수 없습니다.")
            if session["status"] != "active" and not data.get("allow_inactive"):
                raise ApiError(HTTPStatus.CONFLICT, "진행 중인 세션에만 이벤트를 기록할 수 있습니다.")
            if step_id:
                step = connection.execute(
                    "SELECT id FROM steps WHERE id = ? AND session_id = ?",
                    (step_id, session_id),
                ).fetchone()
                if not step:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "해당 세션의 step이 아닙니다.")
            elif event_type not in {"step_start", "step_end"}:
                active_step = connection.execute(
                    """
                    SELECT id FROM steps
                    WHERE session_id = ? AND status = 'active'
                    ORDER BY position DESC LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                step_id = active_step["id"] if active_step else None
            cursor = connection.execute(
                """
                INSERT INTO events (
                    session_id, timestamp, source, type, payload, url, page_title,
                    element, step_id, speech_end_ts, interrupted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    timestamp,
                    source[:40],
                    event_type[:60],
                    json.dumps(payload, ensure_ascii=False),
                    str(data.get("url", ""))[:4000],
                    str(data.get("page_title") or data.get("pageTitle") or "")[:1000],
                    json.dumps(element, ensure_ascii=False),
                    step_id,
                    speech_end_ts,
                    1 if data.get("interrupted") else 0,
                ),
            )
            event_id = cursor.lastrowid
            row = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self.event_from_row(row)

    def list_events(
        self, session_id: str, after_id: int = 0, limit: int = 2000
    ) -> List[Dict[str, Any]]:
        limit = min(max(limit, 1), 10000)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM events
                WHERE session_id = ? AND id > ?
                ORDER BY timestamp ASC, id ASC
                LIMIT ?
                """,
                (session_id, after_id, limit),
            ).fetchall()
        return [self.event_from_row(row) for row in rows]

    def create_issue(self, session_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        summary = str(data.get("summary", "")).strip()
        if not summary:
            raise ApiError(HTTPStatus.BAD_REQUEST, "문제 요약을 입력해 주세요.")
        severity = str(data.get("severity", "moderate"))
        if severity not in {"minor", "moderate", "major", "critical"}:
            raise ApiError(HTTPStatus.BAD_REQUEST, "심각도 값이 올바르지 않습니다.")
        tags = data.get("tags", [])
        if isinstance(tags, str):
            tags = [item.strip() for item in tags.split(",") if item.strip()]
        if not isinstance(tags, list):
            tags = []
        start_event_id = self._optional_event_id(session_id, data.get("start_event_id"))
        end_event_id = self._optional_event_id(session_id, data.get("end_event_id"))
        if start_event_id and end_event_id and start_event_id > end_event_id:
            start_event_id, end_event_id = end_event_id, start_event_id
        start_step_id = self._optional_step_id(session_id, data.get("start_step_id"))
        end_step_id = self._optional_step_id(session_id, data.get("end_step_id"))
        if not start_step_id and start_event_id:
            start_step_id = self._step_id_for_event(session_id, start_event_id)
        if not end_step_id and end_event_id:
            end_step_id = self._step_id_for_event(session_id, end_event_id)
        if start_step_id and end_step_id:
            positions = self._step_positions(session_id, [start_step_id, end_step_id])
            if positions[start_step_id] > positions[end_step_id]:
                start_step_id, end_step_id = end_step_id, start_step_id
        issue_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as connection:
            session = connection.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if not session:
                raise ApiError(HTTPStatus.NOT_FOUND, "세션을 찾을 수 없습니다.")
            connection.execute(
                """
                INSERT INTO issues (
                    id, session_id, summary, description, expected_announcement,
                    severity, tags, start_event_id, end_event_id,
                    start_step_id, end_step_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    issue_id,
                    session_id,
                    summary,
                    str(data.get("description", "")).strip(),
                    str(data.get("expected_announcement", "")).strip(),
                    severity,
                    json.dumps(tags, ensure_ascii=False),
                    start_event_id,
                    end_event_id,
                    start_step_id,
                    end_step_id,
                    now,
                    now,
                ),
            )
        return self.get_issue(issue_id)

    def _optional_event_id(self, session_id: str, value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            event_id = int(value)
        except (TypeError, ValueError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "이벤트 ID가 올바르지 않습니다.")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM events WHERE id = ? AND session_id = ?",
                (event_id, session_id),
            ).fetchone()
        if not row:
            raise ApiError(HTTPStatus.BAD_REQUEST, "해당 세션의 이벤트가 아닙니다.")
        return event_id

    def _optional_step_id(self, session_id: str, value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        step_id = str(value)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM steps WHERE id = ? AND session_id = ?",
                (step_id, session_id),
            ).fetchone()
        if not row:
            raise ApiError(HTTPStatus.BAD_REQUEST, "해당 세션의 step이 아닙니다.")
        return step_id

    def _step_id_for_event(self, session_id: str, event_id: int) -> Optional[str]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT step_id FROM events WHERE id = ? AND session_id = ?",
                (event_id, session_id),
            ).fetchone()
        return row["step_id"] if row else None

    def _step_positions(
        self, session_id: str, step_ids: List[str]
    ) -> Dict[str, int]:
        placeholders = ",".join("?" for _ in step_ids)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, position FROM steps WHERE session_id = ? AND id IN ({})".format(
                    placeholders
                ),
                [session_id] + step_ids,
            ).fetchall()
        return {row["id"]: row["position"] for row in rows}

    def get_issue(self, issue_id: str) -> Dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if not row:
            raise ApiError(HTTPStatus.NOT_FOUND, "문제를 찾을 수 없습니다.")
        return self.issue_from_row(row)

    def list_issues(self, session_id: str) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM issues WHERE session_id = ? ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
        return [self.issue_from_row(row) for row in rows]

    def delete_issue(self, issue_id: str) -> None:
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM issues WHERE id = ?", (issue_id,))
        if cursor.rowcount == 0:
            raise ApiError(HTTPStatus.NOT_FOUND, "문제를 찾을 수 없습니다.")

    def session_summary(self, session_id: str) -> Dict[str, Any]:
        with self.connect() as connection:
            session = connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not session:
                raise ApiError(HTTPStatus.NOT_FOUND, "세션을 찾을 수 없습니다.")
            rows = connection.execute(
                "SELECT * FROM events WHERE session_id = ? ORDER BY timestamp ASC, id ASC",
                (session_id,),
            ).fetchall()
            issue_count = connection.execute(
                "SELECT COUNT(*) AS count FROM issues WHERE session_id = ?", (session_id,)
            ).fetchone()["count"]

        duration_seconds = 0
        if session["started_at"]:
            start = parse_timestamp(session["started_at"])
            end = parse_timestamp(session["ended_at"]) if session["ended_at"] else datetime.now(timezone.utc)
            duration_seconds = max(0, int((end - start).total_seconds()))

        events = [self.event_from_row(row) for row in rows]
        key_events = self._deduplicated_key_events(events)
        tab_forward = 0
        tab_backward = 0
        shortcuts: Dict[str, int] = {}
        for event in key_events:
            chord = self._event_chord(event)
            normalized = self._normalized_event_key(event)
            if normalized == "tab":
                tab_forward += 1
            elif normalized == "shift+tab":
                tab_backward += 1
            elif chord:
                shortcuts[chord] = shortcuts.get(chord, 0) + 1

        navigation_back = sum(
            1
            for event in events
            if event["type"] in {"navigation", "history"}
            and str(event["payload"].get("direction", "")).lower() == "back"
        )
        if not navigation_back:
            navigation_back = sum(
                1
                for event in key_events
                if self._normalized_event_key(event) == "alt+left"
            )

        return {
            "duration_seconds": duration_seconds,
            "event_count": len(events),
            "tab_forward": tab_forward,
            "tab_backward": tab_backward,
            "back_count": navigation_back,
            "speech_count": sum(
                1 for event in events if event["type"] in {"speech", "speech_episode"}
            ),
            "speech_episode_count": sum(
                1 for event in events if event["type"] == "speech_episode"
            ),
            "speech_fragment_count": sum(
                int(event["payload"].get("fragment_count", 1))
                for event in events
                if event["type"] in {"speech", "speech_episode"}
            ),
            "unique_spoken_element_count": len(
                {
                    str(
                        event["element"].get("unique_id")
                        or event["element"].get("ia2_unique_id")
                        or "{}|{}".format(
                            event["element"].get("role", ""),
                            event["element"].get("name", ""),
                        )
                    )
                    for event in events
                    if event["type"] in {"speech", "speech_episode"}
                    and event.get("element")
                }
            ),
            "speech_interruption_count": sum(
                1
                for event in events
                if event["type"] in {"speech", "speech_episode"}
                and event.get("interrupted")
            ),
            "speech_cancel_count": sum(
                1 for event in events if event["type"] in {"speech_cancel", "speech_canceled"}
            ),
            "marker_count": sum(1 for event in events if event["type"] == "marker"),
            "hint_count": sum(1 for event in events if event["type"] == "hint"),
            "step_count": len(self.list_steps(session_id)),
            "issue_count": issue_count,
            "shortcut_counts": dict(
                sorted(shortcuts.items(), key=lambda item: (-item[1], item[0].lower()))
            ),
        }

    @staticmethod
    def _event_chord(event: Dict[str, Any]) -> str:
        payload = event.get("payload", {})
        return str(
            payload.get("chord")
            or payload.get("gesture")
            or payload.get("display_name")
            or payload.get("key")
            or ""
        )

    @classmethod
    def _normalized_event_key(cls, event: Dict[str, Any]) -> str:
        payload = event.get("payload", {})
        identifiers = payload.get("identifiers")
        if isinstance(identifiers, list) and identifiers:
            raw = str(identifiers[0])
        else:
            raw = cls._event_chord(event)
        normalized = raw.lower().replace(" ", "")
        normalized = re.sub(r"^kb(?:\([^)]*\))?:", "", normalized)
        normalized = normalized.replace("arrowleft", "left").replace("leftarrow", "left")
        normalized = normalized.replace("arrowright", "right").replace("rightarrow", "right")
        normalized = normalized.replace("arrowup", "up").replace("uparrow", "up")
        normalized = normalized.replace("arrowdown", "down").replace("downarrow", "down")
        return normalized

    def _deduplicated_key_events(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        candidates = [
            event
            for event in events
            if event["type"] in {"input", "keyboard"} and self._event_chord(event)
        ]
        result: List[Dict[str, Any]] = []
        for event in candidates:
            if result:
                previous = result[-1]
                same_chord = (
                    self._normalized_event_key(previous)
                    == self._normalized_event_key(event)
                )
                delta_ms = abs(
                    (
                        parse_timestamp(event["timestamp"])
                        - parse_timestamp(previous["timestamp"])
                    ).total_seconds()
                    * 1000
                )
                different_source = previous["source"] != event["source"]
                if same_chord and different_source and delta_ms <= 180:
                    if event["source"] == "nvda":
                        result[-1] = event
                    continue
            result.append(event)
        return result

    def build_interactions(self, session_id: str) -> List[Dict[str, Any]]:
        """원본 이벤트를 사람이 읽는 '행동' 단위로 접는다.

        키 입력 하나에 도착 요소(focus)와 그때의 NVDA 안내(speech)를 묶고,
        페이지 URL은 직전 브라우저 이벤트에서 이월해 모든 행동에 붙인다.
        원본은 그대로 보존되며 각 행동의 `event_ids`로 되짚을 수 있다.
        """
        session = self.get_session(session_id)
        events = self.list_events(session_id, limit=10000)
        steps = {step["id"]: step for step in session.get("steps", [])}
        started_at = parse_timestamp(session["started_at"]) if session.get("started_at") else None

        interactions: List[Dict[str, Any]] = []
        current: Optional[Dict[str, Any]] = None
        current_url = ""
        current_title = ""

        def element_of(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            element = event.get("element") or {}
            name = element.get("name") or element.get("accessible_name") or ""
            role = element.get("role") or ""
            # 키를 누르는 순간의 포커스는 전환 중이라 이름 없이 "알 수 없음"으로
            # 잡히는 경우가 대부분이다. 이런 껍데기는 없는 것으로 보고, 도착
            # 요소는 뒤따르는 focus 이벤트에서 채운다.
            if not name and role in {"", "알 수 없음", "unknown"}:
                return None
            return {
                "name": name,
                "role": role,
                "scope": element.get("scope", "unknown"),
                "ia2_unique_id": element.get("ia2_unique_id"),
                "unique_id": element.get("unique_id", ""),
            }

        def new_row(event: Dict[str, Any], kind: str) -> Dict[str, Any]:
            offset = None
            if started_at:
                offset = round(
                    (parse_timestamp(event["timestamp"]) - started_at).total_seconds(), 2
                )
            step = steps.get(event.get("step_id") or "")
            row = {
                "seq": len(interactions) + 1,
                "timestamp": event["timestamp"],
                "offset_s": offset,
                "kind": kind,
                "key": "",
                "step_id": event.get("step_id"),
                "step_title": step["title"] if step else "",
                "url": current_url,
                "page_title": current_title,
                "element": element_of(event),
                "speech": [],
                "detail": {},
                "event_ids": [event["id"]],
            }
            interactions.append(row)
            return row

        for event in events:
            event_type = event["type"]
            payload = event.get("payload") or {}
            if event.get("url"):
                current_url = event["url"]
            # 문서 제목은 브라우저 확장 값만 쓴다. NVDA는 창 제목(브라우저 이름
            # 포함)을 보내므로 페이지 단위 집계에 적합하지 않다.
            if event["source"] == "browser" and event.get("page_title"):
                current_title = event["page_title"]

            if event_type in {"input", "keyboard"}:
                current = new_row(event, "input")
                current["key"] = (
                    payload.get("display_name")
                    or payload.get("chord")
                    or payload.get("gesture")
                    or payload.get("key")
                    or ""
                )
            elif event_type == "focus":
                if current and current["kind"] == "input" and not current["element"]:
                    current["element"] = element_of(event)
                    current["event_ids"].append(event["id"])
                else:
                    current = new_row(event, "focus")
            elif event_type in {"speech", "speech_episode"}:
                listened = None
                if event.get("speech_end_ts"):
                    delta = (
                        parse_timestamp(event["speech_end_ts"])
                        - parse_timestamp(event["timestamp"])
                    ).total_seconds()
                    if delta >= 0:
                        listened = round(delta, 2)
                spoken = {
                    "text": payload.get("normalized_text")
                    or payload.get("raw_text")
                    or payload.get("text", ""),
                    "listened_s": listened,
                    "interrupted": bool(event.get("interrupted")),
                }
                if current and current["kind"] in {"input", "focus", "navigation", "page_ready"}:
                    current["speech"].append(spoken)
                    current["event_ids"].append(event["id"])
                else:
                    current = new_row(event, "speech")
                    current["speech"].append(spoken)
            elif event_type in {"speech_cancel", "speech_canceled"}:
                if current:
                    current["event_ids"].append(event["id"])
            elif event_type == "dom_mutation":
                continue  # 저수준 기록은 원본 events에만 남긴다.
            elif event_type in {"navigation", "history", "page_ready"}:
                current = new_row(event, "navigation" if event_type != "page_ready" else "page_ready")
                current["detail"] = {
                    "direction": payload.get("direction", ""),
                    "transition_type": payload.get("transition_type", ""),
                }
            elif event_type == "marker":
                row = new_row(event, "marker")
                row["detail"] = {
                    "intensity": payload.get("intensity"),
                    "label": payload.get("label", ""),
                }
                current = None
            elif event_type == "hint":
                row = new_row(event, "hint")
                row["detail"] = {"text": payload.get("text", "")}
                current = None
            elif event_type in {"step_start", "step_end"}:
                row = new_row(event, event_type)
                row["detail"] = {
                    "title": payload.get("title", ""),
                    "position": payload.get("position"),
                    "reason": payload.get("reason", ""),
                }
                current = None
            else:
                current = new_row(event, event_type)
                current["detail"] = dict(payload)
        return interactions

    def export_interactions_csv(self, session_id: str) -> bytes:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "seq",
                "timestamp",
                "offset_s",
                "kind",
                "key",
                "element_name",
                "element_role",
                "element_scope",
                "ia2_unique_id",
                "speech_text",
                "listened_s",
                "interrupted",
                "speech_count",
                "url",
                "page_title",
                "step_title",
                "detail",
                "event_ids",
            ]
        )
        for row in self.build_interactions(session_id):
            element = row["element"] or {}
            speech = row["speech"][0] if row["speech"] else {}
            writer.writerow(
                [
                    row["seq"],
                    row["timestamp"],
                    row["offset_s"],
                    row["kind"],
                    row["key"],
                    element.get("name", ""),
                    element.get("role", ""),
                    element.get("scope", "unknown"),
                    element.get("ia2_unique_id", ""),
                    speech.get("text", ""),
                    speech.get("listened_s", ""),
                    speech.get("interrupted", ""),
                    len(row["speech"]),
                    row["url"],
                    row["page_title"],
                    row["step_title"],
                    json.dumps(row["detail"], ensure_ascii=False) if row["detail"] else "",
                    " ".join(str(item) for item in row["event_ids"]),
                ]
            )
        return ("﻿" + output.getvalue()).encode("utf-8")

    def export_json(self, session_id: str) -> bytes:
        payload = self.get_session(session_id, include_related=True)
        # 원본 events는 그대로 두고, 읽기·분석용 파생 뷰를 함께 담는다.
        payload["interactions"] = self.build_interactions(session_id)
        payload["summary"]["interaction_count"] = len(payload["interactions"])
        source_counts = {
            source: sum(1 for event in payload["events"] if event["source"] == source)
            for source in ("nvda", "browser", "dashboard")
        }
        payload["summary"]["source_event_counts"] = source_counts

        environment_fields = (
            "nvda_version",
            "nvda_addon_version",
            "synthesizer",
            "speech_rate",
            "browser",
            "browser_extension_version",
        )
        checks = {
            "session_finished": payload["status"] in {"completed", "abandoned"},
            "nvda_data_present": source_counts["nvda"] > 0,
            "browser_data_present": source_counts["browser"] > 0,
            "environment_complete": all(
                payload["environment"].get(field) not in (None, "")
                for field in environment_fields
            ),
            "steps_defined": bool(payload["steps"]),
            "step_outcomes_recorded": bool(payload["steps"])
            and all(
                step.get("outcome") in {"complete", "assisted", "blocked"}
                for step in payload["steps"]
            ),
            "element_scope_recorded": any(
                (event.get("element") or {}).get("scope")
                in {"web_content", "browser_ui"}
                for event in payload["events"]
                if event["source"] == "nvda"
            ),
        }
        warning_labels = {
            "session_finished": "세션이 아직 종료되지 않았습니다.",
            "nvda_data_present": "NVDA 키·포커스·발화 이벤트가 없습니다.",
            "browser_data_present": "브라우저 URL·페이지 변화 이벤트가 없습니다.",
            "environment_complete": "NVDA·브라우저 환경 정보가 일부 비어 있습니다.",
            "steps_defined": "step이 없어 step별 분석을 할 수 없습니다.",
            "step_outcomes_recorded": "일부 step의 결과 코드가 비어 있습니다.",
            "element_scope_recorded": "웹 콘텐츠와 브라우저 UI 범위 구분이 없는 구버전 기록입니다.",
        }
        critical_checks = (
            "session_finished",
            "nvda_data_present",
            "browser_data_present",
            "environment_complete",
        )
        payload["data_quality"] = {
            "collection_check_passed": all(checks[name] for name in critical_checks),
            "checks": checks,
            "warnings": [
                warning_labels[name] for name, passed in checks.items() if not passed
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    def export_csv(self, session_id: str) -> bytes:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "id",
                "timestamp",
                "speech_end_ts",
                "interrupted",
                "step_id",
                "source",
                "type",
                "url",
                "page_title",
                "element",
                "payload",
            ]
        )
        for event in self.list_events(session_id, limit=10000):
            writer.writerow(
                [
                    event["id"],
                    event["timestamp"],
                    event["speech_end_ts"],
                    event["interrupted"],
                    event["step_id"],
                    event["source"],
                    event["type"],
                    event["url"],
                    event["page_title"],
                    json.dumps(event["element"], ensure_ascii=False),
                    json.dumps(event["payload"], ensure_ascii=False),
                ]
            )
        return ("\ufeff" + output.getvalue()).encode("utf-8")

    def build_result_package(self) -> Dict[str, Any]:
        """\uc644\ub8cc\ub41c \uc138\uc158 \uc804\uccb4\ub97c \uc804\ub2ec\uc6a9 ZIP \ud558\ub098\ub85c \ubb36\uc5b4 `\uacb0\uacfc` \ud3f4\ub354\uc5d0 \ub9cc\ub4e0\ub2e4."""
        base = self.db_path.parent
        root = base.parent if base.name == "data" else base
        package_dir = root / "\uacb0\uacfc"
        package_dir.mkdir(parents=True, exist_ok=True)

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, participant, status, created_at, round FROM sessions"
                " ORDER BY created_at ASC"
            ).fetchall()
        finished = [row for row in rows if row["status"] in {"completed", "abandoned"}]
        active_count = sum(1 for row in rows if row["status"] == "active")

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_path = package_dir / "\uacb0\uacfc\ud328\ud0a4\uc9c0_{}.zip".format(stamp)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as bundle:
            for session in finished:
                label = re.sub(r'[\\/:*?"<>|\s]+', "_", session["participant"] or "\ubb34\uae30\uba85")
                name = "sessions/{}_r{}_{}_{}".format(
                    label,
                    session["round"] or 1,
                    (session["created_at"] or "")[:10],
                    session["id"][:8],
                )
                bundle.writestr(name + ".json", self.export_json(session["id"]))
                bundle.writestr(name + ".csv", self.export_csv(session["id"]))
                bundle.writestr(
                    name + "_interactions.csv",
                    self.export_interactions_csv(session["id"]),
                )
            with tempfile.TemporaryDirectory() as temporary:
                backup_path = Path(temporary) / "recorder.sqlite3"
                with closing(sqlite3.connect(str(self.db_path))) as source, closing(
                    sqlite3.connect(str(backup_path))
                ) as target:
                    source.backup(target)
                bundle.write(backup_path, "recorder.sqlite3")
            kit_info = root / "KIT-INFO.txt"
            if kit_info.exists():
                bundle.writestr("KIT-INFO.txt", kit_info.read_bytes())
            bundle.writestr(
                "\ud328\ud0a4\uc9c0\uc815\ubcf4.txt",
                "\n".join(
                    [
                        "A11y Task Recorder \uacb0\uacfc \ud328\ud0a4\uc9c0",
                        "\uc0dd\uc131 \uc2dc\uac01: {}".format(utc_now()),
                        "\uc644\ub8cc \uc138\uc158 \uc218: {}".format(len(finished)),
                        "\uc9c4\ud589 \uc911\uc774\ub77c \uc81c\uc678\ub41c \uc138\uc158 \uc218: {}".format(active_count),
                        "sessions/ \ud3f4\ub354: \uc138\uc158\ubcc4 JSON\u00b7CSV \ub0b4\ubcf4\ub0b4\uae30",
                        "recorder.sqlite3: \ubcf5\uad6c\uc6a9 \uc804\uccb4 \uae30\ub85d \ubc31\uc5c5(SQLite)",
                        "",
                    ]
                ),
            )
        return {
            "path": str(zip_path),
            "file_name": zip_path.name,
            "session_count": len(finished),
            "active_session_count": active_count,
        }


class RecorderHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], store: RecorderStore):
        super().__init__(address, RecorderHandler)
        self.store = store
        # NVDA 애드온이 마지막으로 서버를 호출한 시각. 애드온이 로드되지 않은
        # 채 평가가 진행되어 키 입력·발화가 통째로 유실되는 사고를 감지한다.
        self.nvda_last_seen: Optional[str] = None

    def handle_error(self, request, client_address):
        # 브라우저가 keep-alive 연결을 끊는 것은 정상 동작이므로 평가자가 보는
        # 서버 창에 traceback을 남기지 않는다.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class RecorderHandler(BaseHTTPRequestHandler):
    server: RecorderHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[{}] {}".format(self.log_date_time_string(), fmt % args))

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, {}".format(BROWSER_CLIENT_HEADER),
        )
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        try:
            self._handle_get()
        except ApiError as error:
            self._json({"error": error.message}, error.status)
        except Exception as error:
            self._json({"error": "서버 오류: {}".format(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        try:
            self._handle_post()
        except ApiError as error:
            self._json({"error": error.message}, error.status)
        except Exception as error:
            self._json({"error": "서버 오류: {}".format(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PATCH(self) -> None:
        try:
            self._handle_patch()
        except ApiError as error:
            self._json({"error": error.message}, error.status)
        except Exception as error:
            self._json({"error": "서버 오류: {}".format(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self) -> None:
        try:
            match = re.fullmatch(r"/api/issues/([^/]+)", urlparse(self.path).path)
            if not match:
                raise ApiError(HTTPStatus.NOT_FOUND, "API 경로를 찾을 수 없습니다.")
            self.server.store.delete_issue(match.group(1))
            self._json({"ok": True})
        except ApiError as error:
            self._json({"error": error.message}, error.status)
        except Exception as error:
            self._json({"error": "서버 오류: {}".format(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/api/health":
            last_seen = self.server.nvda_last_seen
            connected = False
            if last_seen:
                age = (datetime.now(timezone.utc) - parse_timestamp(last_seen)).total_seconds()
                connected = age <= NVDA_LIVENESS_SECONDS
            self._json(
                {
                    "ok": True,
                    "time": utc_now(),
                    "nvda_connected": connected,
                    "nvda_last_seen": last_seen,
                }
            )
            return
        if path == "/api/active-session":
            # 애드온은 urllib으로 이 엔드포인트를 주기적으로 호출한다.
            # 브라우저 확장(fetch)과 구분해 애드온 생존 신호로 사용한다.
            agent = self.headers.get("User-Agent", "")
            if "Python-urllib" in agent or self.headers.get("X-A11y-Client") == "nvda-addon":
                self.server.nvda_last_seen = utc_now()
            self._json({"session": self.server.store.active_session()})
            return
        if path == "/api/sessions":
            self._json({"sessions": self.server.store.list_sessions()})
            return
        match = re.fullmatch(r"/api/sessions/([^/]+)", path)
        if match:
            self._json({"session": self.server.store.get_session(match.group(1))})
            return
        match = re.fullmatch(r"/api/sessions/([^/]+)/events", path)
        if match:
            after_id = int(query.get("after_id", ["0"])[0])
            limit = int(query.get("limit", ["2000"])[0])
            self._json(
                {"events": self.server.store.list_events(match.group(1), after_id, limit)}
            )
            return
        match = re.fullmatch(r"/api/sessions/([^/]+)/issues", path)
        if match:
            self._json({"issues": self.server.store.list_issues(match.group(1))})
            return
        match = re.fullmatch(r"/api/sessions/([^/]+)/steps", path)
        if match:
            self._json({"steps": self.server.store.list_steps(match.group(1))})
            return
        match = re.fullmatch(r"/api/sessions/([^/]+)/export-interactions\.csv", path)
        if match:
            session_id = match.group(1)
            self._bytes(
                self.server.store.export_interactions_csv(session_id),
                "text/csv; charset=utf-8",
                "interactions-{}.csv".format(session_id),
            )
            return
        match = re.fullmatch(r"/api/sessions/([^/]+)/export\.(json|csv)", path)
        if match:
            session_id, extension = match.groups()
            if extension == "json":
                self._bytes(
                    self.server.store.export_json(session_id),
                    "application/json; charset=utf-8",
                    "session-{}.json".format(session_id),
                )
            else:
                self._bytes(
                    self.server.store.export_csv(session_id),
                    "text/csv; charset=utf-8",
                    "session-{}.csv".format(session_id),
                )
            return
        self._serve_static(path)

    def _handle_post(self) -> None:
        path = urlparse(self.path).path
        data = self._read_json()
        if path == "/api/sessions":
            self._json({"session": self.server.store.create_session(data)}, HTTPStatus.CREATED)
            return
        if path == "/api/events":
            if data.get("source") == "browser":
                self._require_browser_client()
            self._json({"event": self.server.store.add_event(data)}, HTTPStatus.CREATED)
            return
        match = re.fullmatch(r"/api/sessions/([^/]+)/(start|stop)", path)
        if match:
            session_id, action = match.groups()
            if action == "start":
                session = self.server.store.start_session(session_id)
            else:
                session = self.server.store.stop_session(session_id, data)
            self._json({"session": session})
            return
        match = re.fullmatch(r"/api/sessions/([^/]+)/rerun", path)
        if match:
            session = self.server.store.rerun_session(match.group(1))
            self._json({"session": session}, HTTPStatus.CREATED)
            return
        match = re.fullmatch(r"/api/sessions/([^/]+)/issues", path)
        if match:
            issue = self.server.store.create_issue(match.group(1), data)
            self._json({"issue": issue}, HTTPStatus.CREATED)
            return
        match = re.fullmatch(r"/api/sessions/([^/]+)/steps", path)
        if match:
            step = self.server.store.create_step(match.group(1), data)
            self._json({"step": step}, HTTPStatus.CREATED)
            return
        match = re.fullmatch(
            r"/api/sessions/([^/]+)/steps/([^/]+)/(start|finish)", path
        )
        if match:
            session_id, step_id, action = match.groups()
            step = self.server.store.transition_step(session_id, step_id, action)
            self._json({"step": step})
            return
        match = re.fullmatch(r"/api/sessions/([^/]+)/hints", path)
        if match:
            hint = self.server.store.add_hint(match.group(1), data)
            self._json({"event": hint}, HTTPStatus.CREATED)
            return
        if path == "/api/export-package":
            result = self.server.store.build_result_package()
            opener = getattr(os, "startfile", None)
            if opener and data.get("open_folder", True):
                try:
                    opener(str(Path(result["path"]).parent))
                except OSError:
                    pass
            self._json({"package": result})
            return
        raise ApiError(HTTPStatus.NOT_FOUND, "API 경로를 찾을 수 없습니다.")

    def _handle_patch(self) -> None:
        path = urlparse(self.path).path
        data = self._read_json()
        match = re.fullmatch(r"/api/sessions/([^/]+)", path)
        if match:
            environment_merge = data.get("environment_merge")
            if isinstance(environment_merge, dict) and {
                "browser",
                "browser_extension_version",
            }.intersection(environment_merge):
                self._require_browser_client()
            self._json({"session": self.server.store.update_session(match.group(1), data)})
            return
        match = re.fullmatch(r"/api/sessions/([^/]+)/steps/([^/]+)", path)
        if match:
            session_id, step_id = match.groups()
            self._json(
                {"step": self.server.store.update_step_outcome(session_id, step_id, data)}
            )
            return
        raise ApiError(HTTPStatus.NOT_FOUND, "API 경로를 찾을 수 없습니다.")

    def _read_json(self) -> Dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "JSON 요청만 지원합니다.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Content-Length가 올바르지 않습니다.")
        if length > 2_000_000:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "요청 크기가 너무 큽니다.")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "JSON 형식이 올바르지 않습니다.")
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "JSON 객체가 필요합니다.")
        return data

    def _require_browser_client(self) -> None:
        if self.headers.get(BROWSER_CLIENT_HEADER) != BROWSER_CLIENT_ID:
            raise ApiError(
                HTTPStatus.FORBIDDEN,
                "전용 평가 브라우저에서 보낸 기록만 허용합니다.",
            )

    def _serve_static(self, path: str) -> None:
        if path in {"", "/"}:
            target = STATIC_DIR / "index.html"
        else:
            relative = path.lstrip("/")
            target = (STATIC_DIR / relative).resolve()
            if STATIC_DIR.resolve() not in target.parents:
                raise ApiError(HTTPStatus.NOT_FOUND, "파일을 찾을 수 없습니다.")
        if not target.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, "파일을 찾을 수 없습니다.")
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._bytes(target.read_bytes(), content_type)

    def _json(self, data: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(
        self, body: bytes, content_type: str, download_name: Optional[str] = None
    ) -> None:
        self.send_response(HTTPStatus.OK)
        self._cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'")
        if download_name:
            self.send_header(
                "Content-Disposition", 'attachment; filename="{}"'.format(download_name)
            )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin", "")
        allowed = (
            origin.startswith("chrome-extension://")
            or origin.startswith("edge-extension://")
            or origin in {"http://127.0.0.1:8765", "http://localhost:8765"}
        )
        if allowed:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")


def create_server(host: str, port: int, db_path: Path) -> RecorderHTTPServer:
    return RecorderHTTPServer((host, port), RecorderStore(db_path))


def main() -> None:
    parser = argparse.ArgumentParser(description="A11y Task Recorder local server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    server = create_server(args.host, args.port, args.db)
    url = "http://{}:{}".format(args.host, server.server_address[1])
    print("A11y Task Recorder: {}".format(url))
    print("데이터베이스: {}".format(args.db.resolve()))
    print("종료하려면 Ctrl+C를 누르세요.")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
