import contextlib
import csv
import io
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

from recorder.server import RecorderHTTPServer, RecorderStore
from recorder.site_check import site_cause
from tests.test_study_flow import catalog


class SiteCheckTests(unittest.TestCase):
    """Windows pilot 10/28: "사이트에 접속할 수 없습니다" needs a cause the researcher can act on."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "catalog.json"
        path.write_text(json.dumps(catalog()), encoding="utf-8")
        self.store = RecorderStore(Path(self.temp.name) / "data" / "site.sqlite3")
        self.store.study_catalog_path = path

    def test_reasons_map_to_the_four_pilot_causes_and_the_rest(self):
        cases = [
            ({"reason": "login_required", "login": "no_code"}, "login"),
            ({"reason": "login_required", "login": "submitted"}, "login"),  # code refused
            ({"reason": "wrong_url", "login": "submitted"}, "login"),  # access page answered with an error
            ({"reason": "wrong_url"}, "address"),
            ({"reason": "invalid_assignment_url"}, "address"),
            ({"reason": "frame_error", "net_error": "net::ERR_BLOCKED_BY_CSP"}, "address"),
            ({"reason": "frame_error", "net_error": "net::ERR_BLOCKED_BY_RESPONSE"}, "address"),
            ({"reason": "frame_error", "net_error": "net::ERR_NAME_NOT_RESOLVED"}, "network"),
            ({"reason": "loading", "stage": "frame"}, "timeout"),
            ({"reason": "unknown"}, "timeout"),
            ({"reason": "extension_unavailable"}, "extension"),
            ({"reason": "bridge_timeout"}, "extension"),
            ({"reason": "capture_missing"}, "page"),
            ({"reason": "collector_error"}, "recorder"),
        ]
        for payload, cause in cases:
            self.assertEqual(cause, site_cause(payload), payload)

    def test_failed_preparation_is_labelled_kept_on_the_draft_and_packaged(self):
        first = self.store.prepare_study("T01")
        ready = self.store.log_study_event(first["id"], "site_preparation", {"result": "ready", "elapsed_ms": 900})
        self.assertNotIn("cause", ready["payload"])
        failed = self.store.log_study_event(first["id"], "site_preparation", {
            "result": "failed", "reason": "login_required", "login": "submitted", "elapsed_ms": 21000})
        self.assertEqual(("login", "로그인 필요"), (failed["payload"]["cause"], failed["payload"]["cause_label"]))
        self.store.log_study_event(first["id"], "site_preparation", {
            "result": "failed", "reason": "frame_error", "net_error": "net::ERR_BLOCKED_BY_CSP"})
        # The task never started: the draft is not a finished session, but the package still says why.
        self.assertEqual("draft", self.store.get_session(first["id"])["status"])
        package = self.store.build_result_package()
        with zipfile.ZipFile(package["path"]) as bundle:
            rows = list(csv.DictReader(io.StringIO(bundle.read("site_preparation.csv").decode("utf-8-sig"))))
            info = bundle.read("패키지정보.txt").decode("utf-8")
        self.assertEqual(0, package["session_count"])
        self.assertEqual(["ready", "failed", "failed"], [row["result"] for row in rows])
        self.assertEqual(["", "로그인 필요", "주소 허용 안 됨"], [row["cause_label"] for row in rows])
        self.assertEqual({"T01"}, {row["tester"] for row in rows})
        self.assertEqual({"1"}, {row["order"] for row in rows})
        self.assertEqual("net::ERR_BLOCKED_BY_CSP", rows[2]["net_error"])
        self.assertIn("사이트 준비 실패 기록: 2건", info)

    def test_site_navigation_is_a_dashboard_study_event(self):
        session = self.store.start_session(self.store.prepare_study("T01")["id"], study_ready=True)
        event = self.store.log_study_event(session["id"], "site_navigation", {"action": "back", "trigger": "site_key", "moved": True})
        self.assertEqual(("dashboard", "site_navigation"), (event["source"], event["type"]))

    def test_server_window_names_the_cause_without_the_site_address(self):
        session = self.store.prepare_study("T01")
        server = RecorderHTTPServer(("127.0.0.1", 0), self.store)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        output = io.StringIO()
        try:
            body = json.dumps({"type": "site_preparation", "payload": {
                "result": "failed", "reason": "loading", "stage": "frame", "elapsed_ms": 35000}}).encode()
            request = Request("http://127.0.0.1:%s/api/sessions/%s/study-events" % (server.server_port, session["id"]),
                              data=body, headers={"Content-Type": "application/json", "User-Agent": "test"})
            with contextlib.redirect_stdout(output), urlopen(request) as response:
                event = json.load(response)["event"]
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual("준비 시간 초과", event["payload"]["cause_label"])
        line = next(line for line in output.getvalue().splitlines() if "사이트 준비 실패" in line)
        self.assertIn("테스터 T01 과업 1 · 준비 시간 초과 (loading)", line)
        self.assertNotIn("example.com", output.getvalue())


if __name__ == "__main__":
    unittest.main()
