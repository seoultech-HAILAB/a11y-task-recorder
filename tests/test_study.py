import json
import tempfile
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from recorder.server import ApiError, RecorderStore, RecorderHTTPServer, utc_now


class StudyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "study.sqlite3"
        self.catalog_path = Path(self.temp.name) / "catalog.json"
        self.catalog = {
            "schema_version": 1, "id": "catalog-opaque", "title": "Study",
            "source": {"spec_version": "v1", "spec_sha256": "a" * 64,
                       "assignments_sha256": "b" * 64},
            "testers": [{"id": tester, "tasks": [
                {"id": tester + "-assignment-" + str(n), "task_id": "opaque-" + str(n),
                 "order": n, "block": 1 if n <= 6 else 2, "site_name": "Site",
                 "instruction": "Find item " + str(n), "start_url": "https://example.com/" + str(n),
                 "time_limit_seconds": 600, "preparation": ["Prepare screen reader"]}
                for n in range(1, 13)]} for tester in ("T01", "T02")],
        }
        self.catalog_path.write_text(json.dumps(self.catalog), encoding="utf-8")
        self.store = RecorderStore(self.db)
        self.store.study_catalog_path = self.catalog_path

    def prepare(self, tester="T01"):
        return self.store.prepare_study(tester)

    def finish(self, session, outcome="completed"):
        self.store.start_session(session["id"], study_ready=True)
        return self.store.stop_session(session["id"], {
            "status": "completed" if outcome == "completed" else "abandoned",
            "study_outcome": outcome, "answer": "my answer", "notes": "observations",
        })

    def test_order_restart_and_final_twelve(self):
        for n in range(1, 13):
            session = self.prepare()
            self.assertEqual(n, session["study"]["order"])
            self.assertEqual(self.prepare()["id"], session["id"])
            self.assertEqual("https://example.com/" + str(n), session["target_url"])
            self.finish(session, ["completed", "blocked", "timeout"][(n - 1) % 3])
            self.store = RecorderStore(self.db)
            self.store.study_catalog_path = self.catalog_path
            self.assertEqual(n, self.store.get_study("T01")["finished"])
        self.assertIsNone(self.store.get_study("T01")["current_order"])
        with self.assertRaises(ApiError):
            self.prepare()
        self.assertEqual(12, self.store.get_study()["testers"][0]["finished"])

    def test_duplicate_concurrent_prepare_and_active_conflict(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            sessions = list(pool.map(lambda _: self.prepare(), range(12)))
        self.assertEqual(1, len({session["id"] for session in sessions}))
        first = sessions[0]
        self.store.start_session(first["id"], study_ready=True)
        self.assertEqual(first["id"], self.prepare()["id"])
        with self.assertRaises(ApiError):
            self.prepare("T02")
        self.assertEqual(first["id"], self.store.active_session()["id"])

    def test_manual_active_cannot_be_displaced(self):
        manual = self.store.create_session({"title": "manual"})
        self.store.start_session(manual["id"])
        with self.assertRaises(ApiError):
            self.prepare()
        self.store.stop_session(manual["id"], {"status": "abandoned"})
        self.assertEqual("draft", self.store.rerun_session(manual["id"])["status"])

    def test_technical_retry_preserves_attempt_and_is_idempotent(self):
        original = self.finish(self.prepare(), "technical")
        self.assertEqual(0, self.store.get_study("T01")["finished"])
        self.assertEqual(original["id"], self.prepare()["id"])
        with ThreadPoolExecutor(max_workers=6) as pool:
            retries = list(pool.map(lambda _: self.store.retry_study("T01", original["id"]), range(12)))
        self.assertEqual(1, len({session["id"] for session in retries}))
        retry = retries[0]
        self.assertNotEqual(original["id"], retry["id"])
        self.assertEqual(original["study"], retry["study"])
        self.assertEqual(2, retry["round"])
        self.assertEqual("technical", self.store.get_session(original["id"])["study_outcome"])
        self.finish(retry)
        self.assertEqual(2, self.prepare()["study"]["order"])

    def test_linkage_content_and_source_are_frozen(self):
        session = self.prepare()
        for data in ({"study": {}}, {"title": "changed"}, {"participant": "T02"},
                     {"scenario": "changed"}, {"target_url": "https://other.test"},
                     {"environment": {}}, {"study_outcome": "completed"}):
            with self.assertRaises(ApiError):
                self.store.update_session(session["id"], data)
        updated = self.store.update_session(session["id"], {"environment_merge": {"nvda_version": "test"}})
        self.assertEqual("test", updated["environment"]["nvda_version"])
        updated = self.store.update_session(session["id"], {"notes": "operator observation"})
        self.assertEqual("operator observation", updated["notes"])
        self.catalog["testers"][0]["tasks"][0]["task_id"] = "replacement"
        self.catalog_path.write_text(json.dumps(self.catalog), encoding="utf-8")
        restarted = RecorderStore(self.db)
        restarted.study_catalog_path = self.catalog_path
        self.assertEqual("opaque-1", restarted.get_study("T01")["tasks"][0]["task_id"])
        self.finish(session)
        exported = json.loads(self.store.export_json(session["id"]))
        self.assertEqual("my answer", exported["study_answer"])
        self.assertEqual("NOT_SCORED", exported["study_evaluation"]["status"])
        self.assertEqual(self.catalog["source"], exported["study"]["source"])
        with self.assertRaises(ApiError):
            self.store.rerun_session(session["id"])

    def test_reject_illegal_start_stop_and_retry(self):
        session = self.prepare()
        with self.assertRaises(ApiError):
            self.store.start_session(session["id"])
        with self.assertRaises(ApiError):
            self.store.stop_session(session["id"], {"study_outcome": "completed"})
        self.store.start_session(session["id"], study_ready=True)
        for data in ({}, {"study_outcome": "anything"}, {"study_outcome": []},
                     {"status": "completed", "study_outcome": "blocked"}):
            with self.assertRaises(ApiError):
                self.store.stop_session(session["id"], data)
        self.assertEqual("active", self.store.get_session(session["id"])["status"])
        with self.assertRaises(ApiError):
            self.store.retry_study("T02", session["id"])

    def test_concurrent_start_allows_only_one_active(self):
        sessions = [self.prepare(tester) for tester in ("T01", "T02")]
        def start(session):
            try:
                return self.store.start_session(session["id"], study_ready=True)["id"]
            except ApiError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(start, sessions))
        self.assertEqual(1, sum(result is not None for result in results))

    def test_preparation_receipt_is_separate_nonparticipant_evidence(self):
        session = self.prepare()
        receipt = {"method": "operator_automation", "actions": ["reset cart"],
                   "quantity": 2, "excluded_from_participant_recording": True}
        for invalid in ({"method": "participant"}, dict(receipt, task_id="override"),
                        dict(receipt, actions=["x" * 9000]), dict(receipt, excluded_from_participant_recording=False)):
            with self.assertRaises(ApiError):
                self.store.start_session(session["id"], study_ready=True, study_setup=invalid)
        started = self.store.start_session(session["id"], study_ready=True, study_setup=receipt)
        self.assertEqual(receipt, started["study_setup"])
        self.assertEqual([], self.store.list_events(session["id"]))
        self.assertEqual(receipt, json.loads(self.store.export_json(session["id"]))["study_setup"])
        with self.assertRaises(ApiError):
            self.store.update_session(session["id"], {"study_setup": {}})

    def test_assigned_quality_uses_task_report_without_fabricating_steps(self):
        session = self.prepare()
        draft = json.loads(self.store.export_json(session["id"]))
        self.assertFalse(draft["data_quality"]["checks"]["study_outcome_recorded"])
        self.finish(session, "blocked")
        exported = json.loads(self.store.export_json(session["id"]))
        checks = exported["data_quality"]["checks"]
        self.assertTrue(checks["study_outcome_recorded"])
        self.assertNotIn("steps_defined", checks)
        self.assertNotIn("step_outcomes_recorded", checks)
        self.assertFalse(exported["data_quality"]["collection_check_passed"])
        self.assertEqual([], exported["steps"])
        self.assertEqual([], exported["events"])
        self.assertEqual("NOT_SCORED", exported["study_evaluation"]["status"])
        self.assertFalse(any("step" in warning for warning in exported["data_quality"]["warnings"]))
        manual = self.store.create_session({"title": "Manual"})
        manual_checks = json.loads(self.store.export_json(manual["id"]))["data_quality"]["checks"]
        self.assertFalse(manual_checks["steps_defined"])
        self.assertFalse(manual_checks["step_outcomes_recorded"])
        self.assertNotIn("study_outcome_recorded", manual_checks)

    def test_catalog_response_does_not_expose_extra_fields(self):
        self.catalog["testers"][0]["tasks"][0].update({"answer": "secret", "variant": "FE", "ground_truth": "secret"})
        self.catalog_path.write_text(json.dumps(self.catalog), encoding="utf-8")
        serialized = json.dumps(self.store.get_study("T01"))
        self.assertNotIn("secret", serialized)
        self.assertNotIn("variant", serialized)

    def test_http_routes_require_preparation_and_live_nvda(self):
        server = RecorderHTTPServer(("127.0.0.1", 0), self.store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(path, body=None):
            data = None if body is None else json.dumps(body).encode()
            req = Request("http://127.0.0.1:%s%s" % (server.server_port, path), data=data,
                          headers={"Content-Type": "application/json", "User-Agent": "Browser-test"})
            try:
                with urlopen(req) as response:
                    return response.status, json.load(response)
            except HTTPError as response:
                with response:
                    return response.code, json.load(response)
        try:
            self.assertEqual(200, request("/api/study")[0])
            status, body = request("/api/study/T01/prepare", {})
            self.assertEqual(200, status)
            session_id = body["session"]["id"]
            path = "/api/sessions/" + session_id
            self.assertEqual(409, request(path + "/start", {"study_ready": True})[0])
            request("/api/active-session")
            self.assertEqual(409, request(path + "/start", {"study_ready": True})[0])
            server.nvda_last_seen = utc_now()
            self.assertEqual(400, request(path + "/start", {})[0])
            self.assertEqual(200, request(path + "/start", {"study_ready": True})[0])
            with patch("recorder.server.STOP_DRAIN_SECONDS", 0):
                self.assertEqual(200, request(path + "/stop", {
                    "status": "abandoned", "study_outcome": "technical", "answer": "observed",
                })[0])
            status, body = request("/api/study/T01/retry", {"session_id": session_id})
            self.assertEqual(200, status)
            self.assertNotEqual(session_id, body["session"]["id"])
            self.assertEqual(1, request("/api/study/T01")[1]["current_order"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
