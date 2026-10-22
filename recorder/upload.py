"""Background upload of finished assigned-task sessions to the study server (0.3.4+).

The result package ZIP stays the primary hand-off. Uploads are an extra path that
must never block or fail a session: every network error is swallowed, the session
stays queued, and the next task end, the package export, the retry loop, or the
facilitator's "지금 전송" button tries again. The study access code is reused as the
bearer token. Since 0.4.0 it is built into the kit (`app/study_access.txt`) or given by
the environment; a code saved in the local database is only the fallback.
"""

import hashlib
import json
import os
import ssl
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from recorder.common import ApiError, json_load, utc_now

UPLOAD_PATH = "/upload/session"
UPLOAD_TIMEOUT_SECONDS = 20
RETRY_INTERVAL_SECONDS = 120
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
ENV_ENABLED = "A11Y_RECORDER_UPLOAD"
ENV_URL = "A11Y_RECORDER_UPLOAD_URL"
ENV_ACCESS_CODE = "A11Y_RECORDER_ACCESS_CODE"
ENV_ACCESS_CODE_FILE = "A11Y_RECORDER_ACCESS_CODE_FILE"
# In the kit this is app/study_access.txt (written by build_release_kit --access-code-file).
KIT_ACCESS_FILE = Path(__file__).resolve().parent.parent / "study_access.txt"
MAX_CODE_LENGTH = 200


def default_enabled() -> bool:
    return os.environ.get(ENV_ENABLED, "1").strip().lower() not in {"0", "false", "off", "no"}


def parse_access_code(text: str) -> str:
    """A code file holds the bare code or JSON with `code` (or the hosting credentials' `accessCode`)."""
    value = text.strip()
    if value.startswith("{"):
        try:
            data = json.loads(value)
            value = str(data.get("code") or data.get("accessCode") or "").strip()
        except (ValueError, AttributeError):
            return ""
    return value if 0 < len(value) <= MAX_CODE_LENGTH and "\n" not in value else ""


def builtin_access_code() -> str:
    """The kit's study access code: environment variable, then the code file. Never logged."""
    code = parse_access_code(os.environ.get(ENV_ACCESS_CODE, ""))
    if code:
        return code
    path = Path(os.environ.get(ENV_ACCESS_CODE_FILE, "").strip() or KIT_ACCESS_FILE)
    try:
        return parse_access_code(path.read_text(encoding="utf-8-sig"))
    except OSError:
        return ""


class UploadManager:
    """Queue in SQLite (`uploads`), settings in `settings`, one sender at a time."""

    def __init__(self, store: Any, enabled: Optional[bool] = None, upload_url: Optional[str] = None,
                 retry_interval: float = RETRY_INTERVAL_SECONDS, builtin_code: Optional[str] = None) -> None:
        self.store = store
        self.builtin_code = builtin_access_code() if builtin_code is None else builtin_code
        self.enabled_default = default_enabled() if enabled is None else enabled
        self.env_url = upload_url if upload_url is not None else os.environ.get(ENV_URL, "").strip()
        self.retry_interval = retry_interval
        self._flush_lock = threading.Lock()
        self._stop = threading.Event()
        self._retry_thread: Optional[threading.Thread] = None
        self.version = ""
        self._ensure_tables()

    # ---- storage -------------------------------------------------------------------
    def _ensure_tables(self) -> None:
        with self.store.connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS uploads (
                    session_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL DEFAULT 'pending',
                    sha256 TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    last_attempt_at TEXT,
                    sent_at TEXT,
                    server_sha256 TEXT,
                    revision INTEGER,
                    queued_at TEXT NOT NULL
                )
                """
            )

    def _setting(self, connection: Any, key: str, fallback: str = "") -> str:
        row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else fallback

    def _set_setting(self, connection: Any, key: str, value: str) -> None:
        connection.execute("INSERT INTO settings(key, value) VALUES (?, ?)"
                           " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def settings(self) -> Dict[str, Any]:
        with self.store.connect() as connection:
            enabled = self._setting(connection, "upload_enabled", "1" if self.enabled_default else "0") == "1"
            saved = self._setting(connection, "upload_code")
            url = self._setting(connection, "upload_url") or self.env_url or self._derived_url(connection)
        code = self.builtin_code or saved
        source = "kit" if self.builtin_code else "local" if saved else ""
        return {"enabled": enabled, "code": code, "code_source": source, "url": url}

    def _catalog_upload_url(self, connection: Any) -> str:
        """The catalog's upload_url, else the copies' origin (first start URL) + /upload/session."""
        try:
            catalog = self.store._study_catalog(connection)
            url = catalog.get("upload_url") or catalog["testers"][0]["tasks"][0]["start_url"]
        except Exception:
            return ""
        parts = urlsplit(url)
        if parts.scheme not in {"https", "http"} or not parts.netloc:
            return ""
        return url if catalog.get("upload_url") else "{}://{}{}".format(parts.scheme, parts.netloc, UPLOAD_PATH)

    def _catalog_origin(self, connection: Any) -> str:
        """Origin of our own study server: the only place the access code may be posted."""
        parts = urlsplit(self._catalog_upload_url(connection))
        return "{}://{}".format(parts.scheme, parts.netloc) if parts.netloc else ""

    def _derived_url(self, connection: Any) -> str:
        return self._catalog_upload_url(connection)

    def access(self) -> Dict[str, Any]:
        """For the local dashboard's automatic iframe login only (GET /api/study/access)."""
        with self.store.connect() as connection:
            origin = self._catalog_origin(connection)
        return {"origin": origin or None, "code": self.settings()["code"] or None}

    def configure(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if "enabled" in data:
                self._set_setting(connection, "upload_enabled", "1" if data.get("enabled") else "0")
            if "code" in data:
                code = str(data.get("code") or "")
                if len(code) > MAX_CODE_LENGTH:
                    raise ApiError(400, "접속 코드가 너무 깁니다.")
                self._set_setting(connection, "upload_code", code)
            if "url" in data:
                url = str(data.get("url") or "").strip()
                if url and urlsplit(url).scheme not in {"https", "http"}:
                    raise ApiError(400, "전송 주소가 올바르지 않습니다.")
                self._set_setting(connection, "upload_url", url)
        return self.status()

    # ---- queue ---------------------------------------------------------------------
    def enqueue(self, session_id: str) -> bool:
        """Queue a finished assigned-task session (re-queues after an answer correction)."""
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status, study FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if not row or row["status"] not in {"completed", "abandoned"} or not json_load(row["study"], {}):
                return False
            connection.execute(
                "INSERT INTO uploads(session_id, state, queued_at) VALUES (?, 'pending', ?)"
                " ON CONFLICT(session_id) DO UPDATE SET state = 'pending', sha256 = NULL",
                (session_id, utc_now()),
            )
        return True

    def status(self) -> Dict[str, Any]:
        settings = self.settings()
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT u.*, s.participant, s.study FROM uploads u JOIN sessions s ON s.id = u.session_id"
                " ORDER BY u.queued_at"
            ).fetchall()
            finished = connection.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE status IN ('completed', 'abandoned') AND study != '{}'"
            ).fetchone()["n"]
        items: List[Dict[str, Any]] = []
        for row in rows:
            study = json_load(row["study"], {})
            items.append({"session_id": row["session_id"], "participant": row["participant"],
                          "order": study.get("order"), "state": row["state"], "attempts": row["attempts"],
                          "last_error": row["last_error"], "sent_at": row["sent_at"], "revision": row["revision"]})
        sent = sum(1 for item in items if item["state"] == "sent")
        pending = len(items) - sent
        target = urlsplit(settings["url"])
        return {
            "enabled": settings["enabled"], "configured": bool(settings["code"]),
            "code_source": settings["code_source"],
            "target": "{}://{}".format(target.scheme, target.netloc) if target.netloc else "",
            "total_finished": finished, "sent": sent, "pending": pending,
            "items": items, "retry_interval_seconds": self.retry_interval,
        }

    # ---- sending -------------------------------------------------------------------
    def flush(self) -> Dict[str, Any]:
        """Try every queued session once; errors only update the queue."""
        if not self._flush_lock.acquire(blocking=False):
            return {"skipped": "busy"}
        try:
            settings = self.settings()
            if not settings["enabled"]:
                return {"skipped": "disabled", "attempted": 0}
            if not settings["code"] or not settings["url"]:
                return {"skipped": "not_configured", "attempted": 0}
            with self.store.connect() as connection:
                queued = [row["session_id"] for row in connection.execute(
                    "SELECT session_id FROM uploads WHERE state = 'pending' ORDER BY queued_at").fetchall()]
            results = {"attempted": len(queued), "sent": 0, "failed": 0}
            for session_id in queued:
                if self._send(session_id, settings):
                    results["sent"] += 1
                else:
                    results["failed"] += 1
            return results
        finally:
            self._flush_lock.release()

    def flush_async(self) -> None:
        threading.Thread(target=self._safe_flush, name="a11yRecorderUpload", daemon=True).start()

    def _safe_flush(self) -> None:
        try:
            self.flush()
        except Exception:
            # Uploading is a side channel; it must never surface to the session.
            pass

    def start_retry_loop(self) -> None:
        if self._retry_thread:
            return
        def loop() -> None:
            while not self._stop.wait(self.retry_interval):
                try:
                    with self.store.connect() as connection:
                        pending = connection.execute(
                            "SELECT 1 FROM uploads WHERE state = 'pending' LIMIT 1").fetchone()
                    if pending:
                        self.flush()
                except Exception:
                    pass
        self._retry_thread = threading.Thread(target=loop, name="a11yRecorderUploadRetry", daemon=True)
        self._retry_thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _record(self, session_id: str, **fields: Any) -> None:
        columns = ", ".join("{} = ?".format(key) for key in fields)
        with self.store.connect() as connection:
            connection.execute("UPDATE uploads SET {} WHERE session_id = ?".format(columns),
                               (*fields.values(), session_id))

    def _send(self, session_id: str, settings: Dict[str, Any]) -> bool:
        now = utc_now()
        try:
            body = self.store.export_json(session_id)
        except Exception as error:
            self._record(session_id, attempts=self._attempts(session_id) + 1, last_attempt_at=now,
                         last_error="export_failed: {}".format(error)[:300])
            return False
        if len(body) > MAX_UPLOAD_BYTES:
            self._record(session_id, attempts=self._attempts(session_id) + 1, last_attempt_at=now,
                         last_error="too_large", sha256=None)
            return False
        digest = hashlib.sha256(body).hexdigest()
        request = urllib.request.Request(
            settings["url"], data=body, method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "Authorization": "Bearer {}".format(settings["code"]),
                "X-Session-Id": session_id,
                "X-Content-Sha256": digest,
                "X-Recorder-Version": self.version,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=UPLOAD_TIMEOUT_SECONDS,
                                        context=ssl.create_default_context()) as response:
                reply = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = json.loads(error.read().decode("utf-8")).get("error", "")
            except Exception:
                pass
            self._record(session_id, attempts=self._attempts(session_id) + 1, last_attempt_at=now,
                         last_error="http_{} {}".format(error.code, detail).strip()[:300], sha256=digest)
            return False
        except Exception as error:
            self._record(session_id, attempts=self._attempts(session_id) + 1, last_attempt_at=now,
                         last_error=type(error).__name__[:300], sha256=digest)
            return False
        if not isinstance(reply, dict) or reply.get("sha256") != digest or not reply.get("ok"):
            self._record(session_id, attempts=self._attempts(session_id) + 1, last_attempt_at=now,
                         last_error="hash_mismatch", sha256=digest,
                         server_sha256=str(reply.get("sha256", "") if isinstance(reply, dict) else "")[:64])
            return False
        self._record(session_id, state="sent", attempts=self._attempts(session_id) + 1, last_attempt_at=now,
                     last_error="", sha256=digest, server_sha256=digest, sent_at=now,
                     revision=reply.get("revision"))
        return True

    def _attempts(self, session_id: str) -> int:
        with self.store.connect() as connection:
            row = connection.execute("SELECT attempts FROM uploads WHERE session_id = ?", (session_id,)).fetchone()
        return int(row["attempts"]) if row else 0
