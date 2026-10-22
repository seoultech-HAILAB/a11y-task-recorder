"""0.3.4 server upload side channel: queue, retry, hash verification, no effect on the task flow."""
import json
import hashlib
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from recorder.server import RecorderHTTPServer, RecorderStore
from recorder.study import load_catalog
from recorder.upload import UploadManager, builtin_access_code, parse_access_code

FAKE_KIT_CODE = "fake-kit-code-for-tests-0001"


class FakeUploadServer:
    """Stands in for the hosted study server's /upload/session."""

    def __init__(self):
        self.requests = []
        self.mode = "ok"
        self.seen = set()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                digest = hashlib.sha256(body).hexdigest()
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                       "session": self.headers.get("X-Session-Id"),
                                       "sha": self.headers.get("X-Content-Sha256"), "body": body})
                if outer.mode == "unauthorized":
                    reply, status = {"error": "unauthorized"}, 401
                elif outer.mode == "wrong_hash":
                    reply, status = {"ok": True, "sha256": "0" * 64, "stored": True}, 201
                else:
                    key = (self.headers.get("X-Session-Id"), digest)
                    duplicate = key in outer.seen
                    outer.seen.add(key)
                    reply, status = {"ok": True, "sha256": digest, "stored": not duplicate, "duplicate": duplicate,
                                     "revision": 1}, 200 if duplicate else 201
                data = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%s/upload/session" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def catalog():
    return {"schema_version": 1, "id": "catalog-upload", "title": "Study",
            "source": {"spec_version": "v1", "spec_sha256": "a" * 64, "assignments_sha256": "b" * 64},
            "testers": [{"id": "T01", "tasks": [
                {"id": "T01-a%d" % n, "task_id": "t%d" % n, "order": n, "block": (n - 1) // 4 + 1, "site_name": "Site",
                 "instruction": "Find item %d" % n, "start_url": "https://study.example.test/s/0123456789ab/index.html",
                 "time_limit_seconds": 600, "preparation": []} for n in range(1, 13)]}]}


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "catalog.json"
        path.write_text(json.dumps(catalog()), encoding="utf-8")
        self.store = RecorderStore(Path(self.temp.name) / "upload.sqlite3")
        self.store.study_catalog_path = path
        self.fake = FakeUploadServer()
        self.addCleanup(self.fake.close)

    def finished_session(self, outcome="completed"):
        session = self.store.prepare_study("T01")
        self.store.start_session(session["id"], study_ready=True)
        return self.store.stop_session(session["id"], {
            "status": "completed" if outcome == "completed" else "abandoned", "study_outcome": outcome, "answer": "x"})

    def manager(self, url=None, enabled=True, builtin_code=""):
        manager = UploadManager(self.store, enabled=enabled, upload_url=self.fake.url if url is None else url,
                                builtin_code=builtin_code)
        manager.version = "test"
        return manager

    def test_default_target_is_the_catalog_origin_and_code_is_never_echoed(self):
        manager = self.manager(url="")
        status = manager.status()
        self.assertEqual("https://study.example.test", status["target"])
        self.assertFalse(status["configured"])
        manager.configure({"code": "secret-code-value"})
        self.assertNotIn("secret-code-value", json.dumps(manager.status()))
        self.assertTrue(manager.status()["configured"])

    def test_catalog_upload_url_overrides_the_task_site_origin(self):
        data = catalog()
        data["upload_url"] = "https://ours.example.test/upload/session"
        for task in data["testers"][0]["tasks"]:
            task["start_url"] = "http://partner.example.test/site/"
        self.store.study_catalog_path.write_text(json.dumps(data), encoding="utf-8")
        manager = self.manager(url="", builtin_code=FAKE_KIT_CODE)
        self.assertEqual("https://ours.example.test/upload/session", manager.settings()["url"])
        # The access code is only ever offered for our own server, never the partner's task site.
        self.assertEqual("https://ours.example.test", manager.access()["origin"])

    def test_invalid_catalog_upload_url_is_rejected(self):
        data = catalog()
        data["upload_url"] = "ftp://ours.example.test/upload"
        self.store.study_catalog_path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_catalog(self.store.study_catalog_path)

    def test_unreachable_server_leaves_session_queued_and_task_flow_intact(self):
        manager = self.manager(url="http://127.0.0.1:9/upload/session")
        manager.configure({"code": "code"})
        session = self.finished_session()
        self.assertTrue(manager.enqueue(session["id"]))
        result = manager.flush()
        self.assertEqual({"attempted": 1, "sent": 0, "failed": 1}, result)
        status = manager.status()
        self.assertEqual(1, status["pending"])
        self.assertEqual(1, status["items"][0]["attempts"])
        self.assertTrue(status["items"][0]["last_error"])
        self.assertEqual("completed", self.store.get_session(session["id"])["status"])
        self.assertEqual(2, self.store.get_study("T01")["current_order"])

    def test_successful_upload_verifies_hash_and_does_not_resend(self):
        manager = self.manager()
        manager.configure({"code": "code"})
        session = self.finished_session("blocked")
        manager.enqueue(session["id"])
        self.assertEqual({"attempted": 1, "sent": 1, "failed": 0}, manager.flush())
        self.assertEqual("Bearer code", self.fake.requests[0]["auth"])
        self.assertEqual(session["id"], self.fake.requests[0]["session"])
        body = json.loads(self.fake.requests[0]["body"])
        self.assertEqual("blocked", body["study_outcome"])
        self.assertEqual(hashlib.sha256(self.fake.requests[0]["body"]).hexdigest(), self.fake.requests[0]["sha"])
        self.assertEqual({"attempted": 0, "sent": 0, "failed": 0}, manager.flush())
        self.assertEqual(1, len(self.fake.requests))
        status = manager.status()
        self.assertEqual((1, 0, 1), (status["sent"], status["pending"], status["total_finished"]))
        # A re-queue (e.g. corrected answer) sends again; the server reports duplicate or revision.
        manager.enqueue(session["id"])
        self.assertEqual(1, manager.flush()["sent"])
        self.assertEqual(2, len(self.fake.requests))

    def test_wrong_echoed_hash_or_unauthorized_keeps_session_pending(self):
        manager = self.manager()
        manager.configure({"code": "code"})
        session = self.finished_session()
        manager.enqueue(session["id"])
        self.fake.mode = "wrong_hash"
        self.assertEqual(1, manager.flush()["failed"])
        self.assertEqual("hash_mismatch", manager.status()["items"][0]["last_error"])
        self.fake.mode = "unauthorized"
        self.assertEqual(1, manager.flush()["failed"])
        self.assertTrue(manager.status()["items"][0]["last_error"].startswith("http_401"))
        self.fake.mode = "ok"
        self.assertEqual(1, manager.flush()["sent"])
        self.assertEqual(3, manager.status()["items"][0]["attempts"])

    def test_disabled_or_unconfigured_upload_makes_no_network_calls(self):
        manager = self.manager(enabled=False)
        manager.configure({"code": "code"})
        session = self.finished_session()
        manager.enqueue(session["id"])
        self.assertEqual("disabled", manager.flush()["skipped"])
        manager.configure({"enabled": True, "code": ""})
        self.assertEqual("not_configured", manager.flush()["skipped"])
        self.assertEqual([], self.fake.requests)
        manual = self.store.create_session({"title": "manual"})
        self.store.start_session(manual["id"])
        self.store.stop_session(manual["id"], {"status": "completed"})
        self.assertFalse(manager.enqueue(manual["id"]))

    def test_kit_code_wins_over_saved_code_is_the_bearer_and_never_echoed(self):
        manager = self.manager(builtin_code=FAKE_KIT_CODE)
        manager.configure({"code": "locally-saved-code-0002"})
        status = manager.status()
        self.assertEqual((True, "kit"), (status["configured"], status["code_source"]))
        self.assertNotIn(FAKE_KIT_CODE, json.dumps(status))
        session = self.finished_session()
        manager.enqueue(session["id"])
        self.assertEqual(1, manager.flush()["sent"])
        self.assertEqual("Bearer " + FAKE_KIT_CODE, self.fake.requests[0]["auth"])
        self.assertEqual({"origin": "https://study.example.test", "code": FAKE_KIT_CODE}, manager.access())
        local = self.manager(builtin_code="")
        self.assertEqual("local", local.status()["code_source"])
        local.configure({"code": ""})
        self.assertEqual({"origin": "https://study.example.test", "code": None}, local.access())

    def test_builtin_code_comes_from_environment_then_code_file(self):
        self.assertEqual(FAKE_KIT_CODE, parse_access_code(json.dumps({"code": FAKE_KIT_CODE})))
        self.assertEqual(FAKE_KIT_CODE, parse_access_code(json.dumps({"accessCode": FAKE_KIT_CODE, "signingKey": "k" * 40})))
        self.assertEqual("", parse_access_code("{not json"))
        self.assertEqual("", parse_access_code("x" * 201))
        path = Path(self.temp.name) / "study_access.txt"
        path.write_text(json.dumps({"code": FAKE_KIT_CODE}), encoding="utf-8")
        with unittest.mock.patch.dict("os.environ", {"A11Y_RECORDER_ACCESS_CODE_FILE": str(path)}, clear=False):
            os.environ.pop("A11Y_RECORDER_ACCESS_CODE", None)
            self.assertEqual(FAKE_KIT_CODE, builtin_access_code())
            os.environ["A11Y_RECORDER_ACCESS_CODE"] = "env-code-for-tests-00003"
            self.assertEqual("env-code-for-tests-00003", builtin_access_code())
        with unittest.mock.patch.dict("os.environ", {"A11Y_RECORDER_ACCESS_CODE_FILE": str(path) + ".missing"}, clear=False):
            os.environ.pop("A11Y_RECORDER_ACCESS_CODE", None)
            self.assertEqual("", builtin_access_code())

    def test_access_route_answers_only_the_local_dashboard(self):
        server = RecorderHTTPServer(("127.0.0.1", 0), self.store)
        server.uploads = self.manager(builtin_code=FAKE_KIT_CODE)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%s" % server.server_port

        def get(headers):
            req = Request(base + "/api/study/access", headers={"User-Agent": "test", **headers})
            try:
                with urlopen(req) as response:
                    return response.status, json.load(response), response.headers.get("Access-Control-Allow-Origin")
            except HTTPError as response:
                with response:
                    return response.code, json.load(response), None
        try:
            status, body, cors = get({})
            self.assertEqual((200, FAKE_KIT_CODE, "https://study.example.test"), (status, body["code"], body["origin"]))
            self.assertIsNone(cors)
            self.assertEqual(200, get({"Origin": base, "Sec-Fetch-Site": "same-origin"})[0])
            for headers in ({"Origin": "https://evil.example"}, {"Origin": "chrome-extension://abcdef"},
                            {"Host": "evil.example:%s" % server.server_port}, {"Sec-Fetch-Site": "cross-site"}):
                status, body, _ = get(headers)
                self.assertEqual(403, status, headers)
                self.assertNotIn(FAKE_KIT_CODE, json.dumps(body))
            # The tester route is not shadowed for real tester ids.
            with urlopen(base + "/api/study/T01") as response:
                self.assertEqual(200, response.status)
        finally:
            server.shutdown()
            server.server_close()

    def test_http_routes_expose_counts_and_stop_queues_in_background(self):
        server = RecorderHTTPServer(("127.0.0.1", 0), self.store)
        server.uploads = self.manager(url="http://127.0.0.1:9/upload/session")
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def request(path, body=None):
            data = None if body is None else json.dumps(body).encode()
            req = Request("http://127.0.0.1:%s%s" % (server.server_port, path), data=data,
                          headers={"Content-Type": "application/json", "User-Agent": "test"})
            try:
                with urlopen(req) as response:
                    return response.status, json.load(response)
            except HTTPError as response:
                with response:
                    return response.code, json.load(response)
        try:
            status, body = request("/api/uploads")
            self.assertEqual(200, status)
            self.assertEqual(0, body["uploads"]["total_finished"])
            request("/api/uploads/config", {"code": "code", "enabled": True})
            session = request("/api/study/T01/prepare", {})[1]["session"]
            request("/api/active-session")  # NVDA liveness (Python-urllib user agent is accepted)
            self.store.start_session(session["id"], study_ready=True)
            with unittest.mock.patch("recorder.server.STOP_DRAIN_SECONDS", 0), \
                    unittest.mock.patch("recorder.server.time.sleep", lambda _s: None):
                status, body = request("/api/sessions/%s/stop" % session["id"],
                                       {"status": "completed", "study_outcome": "completed", "answer": "두부"})
            self.assertEqual(200, status)
            self.assertEqual("completed", body["session"]["status"])
            for _ in range(50):
                uploads = request("/api/uploads")[1]["uploads"]
                if uploads["items"] and uploads["items"][0]["attempts"]:
                    break
                threading.Event().wait(0.05)
            self.assertEqual(1, uploads["pending"])
            status, body = request("/api/uploads/flush", {})
            self.assertEqual(1, body["result"]["failed"])
        finally:
            server.shutdown()
            server.server_close()


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
