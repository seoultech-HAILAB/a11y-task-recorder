"""Remote task assignment (0.6.0): the study server can replace the kit's catalog.

A background loop fetches `<upload origin>/study/catalog/<catalog id>` with the kit's access
code as the bearer token and keeps the newest valid answer in `settings`. Nothing changes while
a task is under way: the new catalog is applied only when the next task is prepared
(`apply_pending`), inside that transaction. Rules:

- same catalog id as the kit, higher `revision`;
- start URLs only on the kit catalog's own origins, and the kit's upload URL is kept, so a
  compromised server cannot point the frame or the results anywhere else;
- an assignment that already has an attempt keeps its task (`task_id`); saved results are
  never touched;
- a prepared but unstarted attempt (draft) whose task changed is closed as aborted
  (`catalog_update`) and the task is prepared again from the new catalog.

Every network or validation error only updates the status; the kit's catalog keeps working.
"""

import json
import ssl
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, Optional
from urllib.parse import quote, urlsplit

from recorder.common import open_no_redirect, utc_now
from recorder.study import load_catalog, parse_catalog, task_origins

CATALOG_PATH = "/study/catalog/"
FETCH_TIMEOUT_SECONDS = 10
REFRESH_SECONDS = 60
MAX_CATALOG_BYTES = 512 * 1024
# A draft whose task differs in any of these is prepared again.
TASK_KEYS = ("task_id", "start_url", "instruction", "time_limit_seconds", "preparation", "requires_answer")


class RemoteCatalog:
    def __init__(self, store: Any, uploads: Any, interval: float = REFRESH_SECONDS) -> None:
        self.store = store
        self.uploads = uploads
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # ---- settings -----------------------------------------------------------------
    def _get(self, connection: Any, key: str) -> str:
        row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else ""

    def _set(self, connection: Any, **values: str) -> None:
        for key, value in values.items():
            connection.execute("INSERT INTO settings(key, value) VALUES (?, ?)"
                               " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                               ("remote_catalog_" + key, value))

    def _note(self, **values: str) -> None:
        with self.store.connect() as connection:
            self._set(connection, checked_at=utc_now(), **values)

    def status(self) -> Dict[str, Any]:
        with self.store.connect() as connection:
            catalog = self.store._study_catalog(connection)
            pending = self._get(connection, "remote_catalog_pending")
            return {
                "url": self.url() or "",
                "revision": int(catalog.get("revision", 0)),
                "pending_revision": int(json.loads(pending).get("revision", 0)) if pending else None,
                "checked_at": self._get(connection, "remote_catalog_checked_at"),
                "applied_at": self._get(connection, "remote_catalog_applied_at"),
                "last_error": self._get(connection, "remote_catalog_error"),
            }

    # ---- fetch --------------------------------------------------------------------
    def url(self, settings: Optional[Dict[str, Any]] = None) -> Optional[str]:
        settings = self.uploads.settings() if settings is None else settings
        parts = urlsplit(settings["url"] or "")
        if not settings["code"] or parts.scheme not in {"https", "http"} or not parts.netloc:
            return None
        kit = load_catalog(self.store.study_catalog_path)
        return "{}://{}{}{}".format(parts.scheme, parts.netloc, CATALOG_PATH, quote(kit["id"], safe=""))

    def refresh(self) -> Dict[str, Any]:
        """Fetch once and keep a valid newer catalog as pending."""
        if not self._lock.acquire(blocking=False):
            return {"skipped": "busy"}
        try:
            try:
                settings = self.uploads.settings()
                url = self.url(settings)
            except (OSError, ValueError, KeyError, TypeError):
                return {"skipped": "no_kit_catalog"}
            if not url:
                return {"skipped": "not_configured"}
            request = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "Authorization": "Bearer {}".format(settings["code"]),
                "X-Recorder-Version": self.uploads.version,
            })
            try:
                with open_no_redirect(request, timeout=FETCH_TIMEOUT_SECONDS,
                                      context=ssl.create_default_context()) as response:
                    body = response.read(MAX_CATALOG_BYTES + 1)
            except urllib.error.HTTPError as error:
                # 404: the server has no catalog for this kit, so the kit's own stays in use.
                self._note(error="" if error.code == 404 else "http_{}".format(error.code))
                return {"status": error.code}
            except Exception as error:
                self._note(error=type(error).__name__[:200])
                return {"error": type(error).__name__}
            try:
                if len(body) > MAX_CATALOG_BYTES:
                    raise ValueError("catalog too large")
                catalog = self.validate(json.loads(body.decode("utf-8")))
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                self._note(error="invalid: {}".format(error)[:200])
                return {"error": "invalid"}
            with self.store.connect() as connection:
                # Serialize with apply_pending: neither a newer pending response nor
                # a just-applied revision may be replaced by a stale network result.
                connection.execute("BEGIN IMMEDIATE")
                current = self.store._study_catalog(connection)
                pending = self._get(connection, "remote_catalog_pending")
                pending_catalog = json.loads(pending) if pending else None
                newest = current
                if pending_catalog and pending_catalog["revision"] > int(current.get("revision", 0)):
                    newest = pending_catalog
                if catalog["revision"] == int(newest.get("revision", 0)) and catalog != newest:
                    self._set(connection, checked_at=utc_now(), error="revision_conflict")
                    return {"error": "revision_conflict"}
                if catalog["revision"] > int(newest.get("revision", 0)):
                    self._set(connection, pending=json.dumps(catalog, ensure_ascii=False))
                self._set(connection, checked_at=utc_now(), error="")
            return {"revision": catalog["revision"]}
        finally:
            self._lock.release()

    def validate(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        kit = load_catalog(self.store.study_catalog_path)
        # Reject coercions before parse_catalog normalizes optional flags. Otherwise
        # a valid remote revision can install tasks the extension cannot start.
        for tester in raw["testers"]:
            for task in tester["tasks"]:
                if "requires_answer" in task and type(task["requires_answer"]) is not bool:
                    raise ValueError("requires_answer must be boolean")
                if type(task["time_limit_seconds"]) is not int or task["time_limit_seconds"] <= 0:
                    raise ValueError("time_limit_seconds must be a positive integer")
                value = task["start_url"]
                if not isinstance(value, str):
                    raise ValueError("invalid assignment URL")
                parts = urlsplit(value)
                parts.port  # Also reject malformed/out-of-range ports.
                if (parts.scheme not in {"http", "https"} or not parts.hostname or
                        parts.username or parts.password or parts.query or parts.fragment):
                    raise ValueError("invalid assignment URL")
        catalog = parse_catalog(raw)
        if catalog["id"] != kit["id"]:
            raise ValueError("catalog id differs from the kit")
        if catalog.get("revision", 0) < 1:
            raise ValueError("remote catalog needs a revision")
        def canonical_origin(value: str) -> tuple:
            parts = urlsplit(value)
            port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
            return parts.scheme, parts.hostname, port

        if not {canonical_origin(value) for value in task_origins(catalog)} <= {
                canonical_origin(value) for value in task_origins(kit)}:
            raise ValueError("start URL outside the kit's sites")
        catalog.pop("upload_url", None)
        if kit.get("upload_url"):
            catalog["upload_url"] = kit["upload_url"]
        return catalog

    # ---- apply --------------------------------------------------------------------
    def apply_pending(self, connection: Any) -> Optional[int]:
        """Called inside _prepare_study's transaction. Returns the applied revision."""
        pending = self._get(connection, "remote_catalog_pending")
        if not pending:
            return None
        catalog = json.loads(pending)
        current = self.store._study_catalog(connection)
        old_tasks = {task["id"]: task for tester in current["testers"] for task in tester["tasks"]}
        error = ""
        if catalog["id"] != current["id"] or catalog["revision"] <= int(current.get("revision", 0)):
            error = "stale"
        elif connection.execute("SELECT 1 FROM sessions WHERE status IN ('active', 'answering') LIMIT 1").fetchone():
            return None  # a task is under way; try at the next preparation
        else:
            new_tasks = {task["id"]: task for tester in catalog["testers"] for task in tester["tasks"]}
            new_owners = {task["id"]: tester["id"] for tester in catalog["testers"] for task in tester["tasks"]}
            for row in connection.execute(
                "SELECT a.assignment_id, a.tester, s.study, s.started_at, s.target_url, s.scenario"
                " FROM study_attempts a JOIN sessions s ON s.id = a.session_id"
                " WHERE a.catalog_id = ?", (current["id"],)
            ).fetchall():
                task = new_tasks.get(row["assignment_id"])
                recorded = json.loads(row["study"] or "{}")
                if task and new_owners[row["assignment_id"]] != row["tester"]:
                    error = "assignment {} changed its tester".format(row["assignment_id"])
                    break
                if task and task["task_id"] != recorded.get("task_id"):
                    error = "assignment {} changed its task".format(row["assignment_id"])
                    break
                if task and row["started_at"]:
                    # Progress joins by assignment id. Reusing it for a different
                    # task definition would mark the new task already completed.
                    snapshot = {"task_id": recorded.get("task_id"), "start_url": row["target_url"],
                                "instruction": row["scenario"], "preparation": recorded.get("preparation"),
                                "time_limit_seconds": recorded.get("time_limit_seconds")}
                    if "requires_answer" in recorded:
                        snapshot["requires_answer"] = recorded["requires_answer"]
                    elif row["assignment_id"] in old_tasks:
                        snapshot["requires_answer"] = old_tasks[row["assignment_id"]].get("requires_answer", True)
                    if any(task.get(key) != value for key, value in snapshot.items()):
                        error = "assignment {} changed executed content".format(row["assignment_id"])
                        break
        if error:
            self._set(connection, pending="", error=error[:200])
            return None
        now = utc_now()
        for row in connection.execute(
            "SELECT s.id, a.assignment_id FROM sessions s JOIN study_attempts a ON s.id = a.session_id"
            " WHERE s.status = 'draft' AND a.catalog_id = ?", (current["id"],)
        ).fetchall():
            old, new = old_tasks.get(row["assignment_id"]), new_tasks.get(row["assignment_id"])
            if new is None or old is None or any(old.get(key) != new.get(key) for key in TASK_KEYS):
                connection.execute("UPDATE sessions SET status = 'aborted', ended_at = ?, aborted_at = ?,"
                                   " abort_reason = 'catalog_update' WHERE id = ?", (now, now, row["id"]))
        connection.execute("UPDATE study_catalog SET catalog = ? WHERE singleton = 1",
                           (json.dumps(catalog, ensure_ascii=False),))
        self._set(connection, pending="", applied_at=now, error="")
        return catalog["revision"]

    # ---- loop ---------------------------------------------------------------------
    def start_loop(self) -> None:
        if self._thread:
            return

        def loop() -> None:
            while True:
                try:
                    self.refresh()
                except Exception:
                    pass  # a side channel; the kit's catalog keeps working
                if self._stop.wait(self.interval):
                    return

        self._thread = threading.Thread(target=loop, name="a11yRecorderCatalog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
