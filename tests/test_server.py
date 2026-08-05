import ast
import json
import re
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from recorder.server import ApiError, RecorderStore, create_server


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = RecorderStore(Path(self.temporary.name) / "test.sqlite3")
        self.session = self.store.create_session(
            {
                "title": "검색 과업",
                "participant": "P01",
                "target_url": "https://example.com/search",
                "environment": {"nvda_version": "2026.1"},
            }
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_session_event_issue_lifecycle(self):
        started = self.store.start_session(self.session["id"])
        self.assertEqual("active", started["status"])

        browser_tab = self.store.add_event(
            {
                "source": "browser",
                "type": "keyboard",
                "timestamp": "2026-07-29T10:00:00.000Z",
                "payload": {"chord": "Tab"},
            }
        )
        self.store.add_event(
            {
                "source": "nvda",
                "type": "input",
                "timestamp": "2026-07-29T10:00:00.090Z",
                "payload": {
                    "gesture": "탭",
                    "identifiers": ["kb(laptop):tab", "kb:tab"],
                },
            }
        )
        speech = self.store.add_event(
            {
                "source": "nvda",
                "type": "speech",
                "timestamp": "2026-07-29T10:00:00.120Z",
                "payload": {"text": "검색 버튼"},
            }
        )
        self.store.add_event(
            {
                "source": "browser",
                "type": "navigation",
                "payload": {"direction": "back"},
            }
        )
        issue = self.store.create_issue(
            self.session["id"],
            {
                "summary": "변경 안내 없음",
                "severity": "major",
                "tags": "상태 변경, 안내 부족",
                "start_event_id": speech["id"],
                "end_event_id": browser_tab["id"],
            },
        )
        self.assertEqual(browser_tab["id"], issue["start_event_id"])
        self.assertEqual(speech["id"], issue["end_event_id"])
        self.assertEqual(["상태 변경", "안내 부족"], issue["tags"])

        summary = self.store.get_session(self.session["id"])["summary"]
        self.assertEqual(1, summary["tab_forward"])
        self.assertEqual(1, summary["back_count"])
        self.assertEqual(1, summary["speech_count"])
        self.assertEqual(1, summary["issue_count"])

        stopped = self.store.stop_session(
            self.session["id"], {"status": "completed", "notes": "검색이 어려웠음"}
        )
        self.assertEqual("completed", stopped["status"])
        self.assertEqual("검색이 어려웠음", stopped["notes"])

    def test_only_one_active_session(self):
        other = self.store.create_session({"title": "다른 과업"})
        self.store.start_session(self.session["id"])
        with self.assertRaisesRegex(Exception, "이미 진행 중인 세션"):
            self.store.start_session(other["id"])

    def test_completed_session_cannot_be_restarted(self):
        self.store.start_session(self.session["id"])
        self.store.stop_session(self.session["id"], {"status": "completed"})
        with self.assertRaisesRegex(Exception, "초안 상태의 세션만"):
            self.store.start_session(self.session["id"])

    def test_export_does_not_flatten_structured_context(self):
        self.store.start_session(self.session["id"])
        self.store.add_event(
            {
                "source": "nvda",
                "type": "focus",
                "element": {"name": "검색", "role": "편집창"},
                "payload": {"kind": "system_focus"},
            }
        )
        exported = json.loads(self.store.export_json(self.session["id"]).decode("utf-8"))
        self.assertEqual("검색", exported["events"][0]["element"]["name"])
        csv_data = self.store.export_csv(self.session["id"]).decode("utf-8-sig")
        self.assertIn("편집창", csv_data)

    def test_steps_hints_and_speech_episode_are_structured(self):
        step_one = self.store.create_step(
            self.session["id"],
            {
                "title": "검색어 입력",
                "expected_announcement": "검색어 편집창",
            },
        )
        step_two = self.store.create_step(
            self.session["id"], {"title": "검색 결과 확인"}
        )
        self.store.start_session(self.session["id"])
        self.store.transition_step(self.session["id"], step_one["id"], "start")
        speech = self.store.add_event(
            {
                "source": "nvda",
                "type": "speech_episode",
                "timestamp": "2026-07-29T10:00:00.000Z",
                "speech_end_ts": "2026-07-29T10:00:01.350Z",
                "interrupted": True,
                "element": {
                    "name": "검색",
                    "role": "버튼",
                    "ia2_unique_id": -17,
                    "unique_id": "ia2:chrome:10:-17",
                },
                "payload": {
                    "raw_text": "검색 버튼 클릭 가능",
                    "normalized_text": "검색",
                    "fragment_count": 2,
                },
            }
        )
        hint = self.store.add_hint(
            self.session["id"], {"text": "검색 버튼을 찾아보세요."}
        )
        self.store.transition_step(self.session["id"], step_two["id"], "start")
        issue = self.store.create_issue(
            self.session["id"],
            {
                "summary": "검색 버튼 발견이 어려움",
                "start_event_id": speech["id"],
                "end_event_id": hint["id"],
            },
        )

        self.assertEqual(step_one["id"], speech["step_id"])
        self.assertEqual(step_one["id"], hint["step_id"])
        self.assertTrue(speech["interrupted"])
        self.assertEqual("2026-07-29T10:00:01.350Z", speech["speech_end_ts"])
        self.assertEqual(step_one["id"], issue["start_step_id"])
        self.assertEqual(step_one["id"], issue["end_step_id"])

        summary = self.store.get_session(self.session["id"])["summary"]
        self.assertEqual(1, summary["speech_episode_count"])
        self.assertEqual(2, summary["speech_fragment_count"])
        self.assertEqual(1, summary["speech_interruption_count"])
        self.assertEqual(1, summary["hint_count"])
        self.assertEqual(2, summary["step_count"])

        exported = json.loads(self.store.export_json(self.session["id"]).decode("utf-8"))
        self.assertEqual(2, len(exported["steps"]))
        exported_speech = next(
            event for event in exported["events"] if event["type"] == "speech_episode"
        )
        self.assertEqual(
            "ia2:chrome:10:-17", exported_speech["element"]["unique_id"]
        )

    def test_nvda_speech_preprocessing_preserves_raw_name(self):
        addon_path = (
            Path(__file__).resolve().parent.parent
            / "nvda-addon"
            / "globalPlugins"
            / "a11yTaskRecorder.py"
        )
        tree = ast.parse(addon_path.read_text(encoding="utf-8"))
        functions = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name in {"safeString", "normalizeSpeechText"}
        ]
        namespace = {"re": re}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(addon_path), "exec"), namespace)

        self.assertEqual("검색 버튼", namespace["safeString"]("검색 버튼"))
        self.assertEqual(
            "검색",
            namespace["normalizeSpeechText"]("검색 버튼 클릭 가능"),
        )
        self.assertEqual(
            "Checkout",
            namespace["normalizeSpeechText"]("Checkout button clickable"),
        )

    def test_interactions_fold_events_and_carry_url(self):
        self.store.start_session(self.session["id"])
        self.store.add_event(
            {
                "session_id": self.session["id"],
                "source": "browser",
                "type": "navigation",
                "url": "https://example.com/search",
                "page_title": "검색",
                "payload": {"direction": "new"},
            }
        )
        self.store.add_event(
            {
                "session_id": self.session["id"],
                "source": "nvda",
                "type": "input",
                "payload": {"display_name": "탭"},
            }
        )
        self.store.add_event(
            {
                "session_id": self.session["id"],
                "source": "nvda",
                "type": "focus",
                "element": {"name": "로그인", "role": "링크", "ia2_unique_id": -42},
            }
        )
        speech = self.store.add_event(
            {
                "session_id": self.session["id"],
                "source": "nvda",
                "type": "speech_episode",
                "payload": {"normalized_text": "로그인", "raw_text": "로그인 링크"},
            }
        )
        self.store.add_event(
            {
                "session_id": self.session["id"],
                "source": "browser",
                "type": "dom_mutation",
                "url": "https://example.com/search",
                "payload": {"added_nodes": 3},
            }
        )
        self.store.add_event(
            {
                "session_id": self.session["id"],
                "source": "dashboard",
                "type": "marker",
                "payload": {"label": "불편", "intensity": 4},
            }
        )

        interactions = self.store.build_interactions(self.session["id"])
        kinds = [item["kind"] for item in interactions]
        self.assertEqual(["navigation", "input", "marker"], kinds)

        tab = interactions[1]
        self.assertEqual("탭", tab["key"])
        self.assertEqual("로그인", tab["element"]["name"])
        self.assertEqual("링크", tab["element"]["role"])
        self.assertEqual("로그인", tab["speech"][0]["text"])
        # 페이지 URL이 브라우저 이벤트에서 이월된다.
        self.assertEqual("https://example.com/search", tab["url"])
        # 원본으로 되짚을 수 있어야 한다.
        self.assertIn(speech["id"], tab["event_ids"])
        self.assertEqual(4, interactions[2]["detail"]["intensity"])

        payload = json.loads(self.store.export_json(self.session["id"]).decode("utf-8"))
        self.assertEqual(len(interactions), len(payload["interactions"]))
        self.assertIn("events", payload)

        csv_text = self.store.export_interactions_csv(self.session["id"]).decode("utf-8")
        self.assertIn("element_name", csv_text)
        self.assertIn("로그인", csv_text)

    def test_step_outcome_defaults_and_manual_update(self):
        first = self.store.create_step(self.session["id"], {"title": "검색"})
        second = self.store.create_step(self.session["id"], {"title": "결제"})
        self.store.start_session(self.session["id"])
        self.store.transition_step(self.session["id"], first["id"], "start")
        self.store.add_hint(
            self.session["id"], {"text": "검색 메뉴를 알려줌", "step_id": first["id"]}
        )
        self.store.transition_step(self.session["id"], first["id"], "finish")
        self.store.transition_step(self.session["id"], second["id"], "start")
        self.store.stop_session(self.session["id"], {"status": "completed"})

        steps = {
            step["title"]: step
            for step in self.store.get_session(self.session["id"])["steps"]
        }
        self.assertEqual("assisted", steps["검색"]["outcome"])
        self.assertEqual("complete", steps["결제"]["outcome"])

        updated = self.store.update_step_outcome(
            self.session["id"],
            second["id"],
            {"outcome": "blocked", "outcome_note": "결제 버튼을 찾지 못함"},
        )
        self.assertEqual("blocked", updated["outcome"])
        self.assertEqual("결제 버튼을 찾지 못함", updated["outcome_note"])
        with self.assertRaises(ApiError):
            self.store.update_step_outcome(
                self.session["id"], second["id"], {"outcome": "nope"}
            )

    def test_rerun_creates_next_round_with_copied_steps(self):
        self.store.create_step(self.session["id"], {"title": "검색어 입력"})
        self.store.start_session(self.session["id"])
        self.store.stop_session(self.session["id"], {"status": "completed"})
        second = self.store.rerun_session(self.session["id"])
        self.assertEqual(2, second["round"])
        self.assertEqual(self.session["id"], second["group_id"])
        self.assertEqual("draft", second["status"])
        self.assertEqual(1, len(second["steps"]))
        self.assertEqual("검색어 입력", second["steps"][0]["title"])
        self.assertEqual([1, 2], [item["round"] for item in second["rounds"]])
        with self.assertRaises(ApiError):
            self.store.rerun_session(second["id"])

    def test_result_package_bundles_finished_sessions(self):
        self.store.start_session(self.session["id"])
        self.store.stop_session(self.session["id"], {"status": "completed"})
        result = self.store.build_result_package()
        package_path = Path(result["path"])
        self.assertTrue(package_path.exists())
        self.assertEqual(1, result["session_count"])
        self.assertEqual(0, result["active_session_count"])
        with zipfile.ZipFile(package_path) as bundle:
            names = bundle.namelist()
            session_files = [
                name
                for name in names
                if name.startswith("sessions/") and name.endswith(".json")
            ]
            self.assertEqual(1, len(session_files))
            self.assertIn("P01", session_files[0])
            payload = json.loads(bundle.read(session_files[0]).decode("utf-8"))
            self.assertEqual("검색 과업", payload["title"])
            self.assertIn("events", payload)
            self.assertIn("recorder.sqlite3", names)
            self.assertIn("패키지정보.txt", names)

    def test_existing_0_1_database_is_migrated(self):
        legacy_path = Path(self.temporary.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(str(legacy_path))) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    participant TEXT NOT NULL DEFAULT '',
                    target_url TEXT NOT NULL DEFAULT '',
                    scenario TEXT NOT NULL DEFAULT '',
                    expected_announcement TEXT NOT NULL DEFAULT '',
                    environment TEXT NOT NULL DEFAULT '{}',
                    notes TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    ended_at TEXT
                );
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    source TEXT NOT NULL,
                    type TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    url TEXT NOT NULL DEFAULT '',
                    page_title TEXT NOT NULL DEFAULT '',
                    element TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE issues (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    expected_announcement TEXT NOT NULL DEFAULT '',
                    severity TEXT NOT NULL DEFAULT 'moderate',
                    tags TEXT NOT NULL DEFAULT '[]',
                    start_event_id INTEGER,
                    end_event_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
        RecorderStore(legacy_path)
        with closing(sqlite3.connect(str(legacy_path))) as connection:
            session_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(sessions)")
            }
            event_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(events)")
            }
            issue_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(issues)")
            }
            step_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'steps'"
            ).fetchone()
        self.assertIn("prior_site_experience", session_columns)
        self.assertTrue({"step_id", "speech_end_ts", "interrupted"} <= event_columns)
        self.assertTrue({"start_step_id", "end_step_id"} <= issue_columns)
        self.assertIsNotNone(step_table)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.server = create_server(
            "127.0.0.1", 0, Path(self.temporary.name) / "http.sqlite3"
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:{}".format(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, path, method="GET", body=None, origin=None):
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if origin:
            headers["Origin"] = origin
        request = Request(self.base + path, data=data, headers=headers, method=method)
        with urlopen(request, timeout=3) as response:
            return response, response.read()

    def test_static_health_and_cors(self):
        response, body = self.request("/")
        self.assertEqual(200, response.status)
        self.assertIn(b"A11y Task Recorder", body)

        response, body = self.request(
            "/api/health", origin="chrome-extension://abcdefghijklmnop"
        )
        self.assertEqual("chrome-extension://abcdefghijklmnop", response.headers["Access-Control-Allow-Origin"])
        self.assertTrue(json.loads(body)["ok"])

    def test_api_round_trip(self):
        _, body = self.request(
            "/api/sessions", "POST", {"title": "회원 가입", "participant": "P02"}
        )
        session = json.loads(body)["session"]
        self.request("/api/sessions/{}/start".format(session["id"]), "POST", {})
        _, active_body = self.request("/api/active-session")
        self.assertEqual(session["id"], json.loads(active_body)["session"]["id"])
        self.request(
            "/api/events",
            "POST",
            {
                "source": "browser",
                "type": "marker",
                "payload": {"label": "어려움"},
            },
        )
        _, events_body = self.request("/api/sessions/{}/events".format(session["id"]))
        self.assertEqual("marker", json.loads(events_body)["events"][0]["type"])

    def test_health_reports_nvda_liveness(self):
        _, body = self.request("/api/health")
        self.assertFalse(json.loads(body)["nvda_connected"])

        # 애드온은 urllib 기본 User-Agent로 active-session을 폴링한다.
        request = Request(
            self.base + "/api/active-session",
            headers={"User-Agent": "Python-urllib/3.13"},
        )
        with urlopen(request, timeout=3) as response:
            response.read()

        _, body = self.request("/api/health")
        payload = json.loads(body)
        self.assertTrue(payload["nvda_connected"])
        self.assertIsNotNone(payload["nvda_last_seen"])

    def test_export_package_endpoint(self):
        _, body = self.request(
            "/api/sessions", "POST", {"title": "패키지 확인", "participant": "P09"}
        )
        session = json.loads(body)["session"]
        self.request("/api/sessions/{}/start".format(session["id"]), "POST", {})
        self.request(
            "/api/sessions/{}/stop".format(session["id"]), "POST", {"status": "completed"}
        )
        _, package_body = self.request(
            "/api/export-package", "POST", {"open_folder": False}
        )
        package = json.loads(package_body)["package"]
        self.assertEqual(1, package["session_count"])
        self.assertTrue(Path(package["path"]).exists())

    def test_rejects_non_json_write(self):
        request = Request(
            self.base + "/api/sessions",
            data=b"title=unsafe",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=3)
        self.assertEqual(415, caught.exception.code)


if __name__ == "__main__":
    unittest.main()
