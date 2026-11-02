import json
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import urlopen

from recorder.server import RecorderStore, create_server
from recorder.upload import UploadManager


class ExportEvidenceReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RecorderStore(Path(self.temp.name) / "fixture.sqlite3")
        self.session = self.store.create_session({"title": "Fixture task", "participant": "T-fixture",
                                                  "target_url": "https://copies.fixture.test/search"})
        with patch("recorder.server.utc_now", return_value="2026-01-01T00:00:00.000Z"):
            self.store.start_session(self.session["id"])

    def test_interaction_retains_observed_element_states_and_input_context(self):
        event = self.store.add_event({"session_id": self.session["id"], "source": "nvda", "type": "input",
            "timestamp": "2026-01-01T00:00:01.000Z",
            "element": {"name": "Filter", "role": "checkbox", "scope": "web_content", "frame": "task",
                        "states": ["checked", "focused"], "document_url": "https://copies.fixture.test/search",
                        "frame_attribution": "url_only_unverified"},
            "payload": {"display_name": "Space", "identifiers": ["kb:space"], "browse_state": "browse"}})
        row = self.store.build_interactions(self.session["id"])[0]
        self.assertEqual(["checked", "focused"], row["element"]["states"])
        self.assertEqual("task", row["element"]["frame"])
        self.assertEqual("url_only_unverified", row["element"]["frame_attribution"])
        self.assertEqual("https://copies.fixture.test/search", row["element"]["document_url"])
        self.assertEqual("browse", row["detail"]["browse_state"])
        self.assertEqual(["kb:space"], row["detail"]["identifiers"])
        self.assertIn(event["id"], row["event_ids"])

    def test_speech_preserves_source_timestamp_and_event_identity(self):
        self.store.add_event({"session_id": self.session["id"], "source": "nvda", "type": "input",
                             "timestamp": "2026-01-01T00:00:01.000Z", "payload": {"display_name": "Enter"}})
        event = self.store.add_event({"session_id": self.session["id"], "source": "nvda", "type": "speech_episode",
            "timestamp": "2026-01-01T00:00:01.100Z", "speech_end_ts": "2026-01-01T00:00:01.500Z",
            "payload": {"text": "Results"}})
        speech = self.store.build_interactions(self.session["id"])[0]["speech"][0]
        self.assertEqual(event["id"], speech["event_id"])
        self.assertEqual(event["timestamp"], speech["timestamp"])
        self.assertEqual(event["speech_end_ts"], speech["speech_end_ts"])
        self.assertEqual(0.4, speech["listened_s"])

    def test_unknown_input_context_survives_later_arrival_focus(self):
        self.store.add_event({"session_id": self.session["id"], "source": "nvda", "type": "input",
            "timestamp": "2026-01-01T00:00:01.000Z", "payload": {"display_name": "Tab"},
            "element": {"name": "", "role": "unknown", "frame": "task",
                        "document_url": "https://copies.fixture.test/search", "frame_attribution": "url_only_unverified"}})
        self.store.add_event({"session_id": self.session["id"], "source": "nvda", "type": "focus",
            "timestamp": "2026-01-01T00:00:01.100Z", "element": {"name": "Submit", "role": "button"}})
        row = self.store.build_interactions(self.session["id"])[0]
        self.assertEqual("Submit", row["element"]["name"])
        self.assertEqual("unknown", row["detail"]["input_element"]["role"])
        self.assertEqual("https://copies.fixture.test/search", row["detail"]["input_element"]["document_url"])

    def test_free_exports_are_memory_only_and_do_not_claim_verified_nvda_window(self):
        with self.store.connect() as c:
            c.execute("UPDATE sessions SET study=?, status='completed', study_outcome='completed' WHERE id=?",
                      (json.dumps({"kind": "free", "free_index": 2, "tester": "T-fixture"}), self.session["id"]))
        data = json.loads(self.store.export_json(self.session["id"]))
        self.assertEqual("free", data["study"]["kind"])
        self.assertEqual(2, data["study"]["free_index"])
        self.assertEqual("memory_only", data["data_quality"]["intended_use"])
        self.assertFalse(data["data_quality"]["checks"]["free_nvda_window_attribution_verified"])
        self.assertFalse(data["data_quality"]["collection_check_passed"])

    def test_capture_loss_marker_survives_derived_view_and_blocks_quality_pass(self):
        event = self.store.add_event({"session_id": self.session["id"], "source": "browser", "type": "marker",
            "timestamp": "2026-01-01T00:00:01.000Z", "payload": {
                "kind": "free_launch_buffer_overflow", "evidence_kind": "recorder_metadata", "dropped_events": 3}})
        data = json.loads(self.store.export_json(self.session["id"]))
        row = data["interactions"][0]
        self.assertEqual(3, row["detail"]["dropped_events"])
        self.assertEqual("recorder_metadata", row["detail"]["evidence_kind"])
        self.assertIn(event["id"], row["event_ids"])
        self.assertFalse(data["data_quality"]["checks"]["no_reported_capture_gap"])
        self.assertFalse(data["data_quality"]["collection_check_passed"])

    def test_http_current_free_task_recovers_answering_session(self):
        with self.store.connect() as c:
            c.execute("UPDATE sessions SET study=?, status='answering' WHERE id=?",
                      (json.dumps({"kind": "free", "free_index": 1, "tester": "T-fixture"}), self.session["id"]))
        http = create_server("localhost", 0, self.store.db_path)
        thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
        try:
            with urlopen("http://localhost:{}/api/free-task/current".format(http.server_port), timeout=3) as r:
                session = json.load(r)["session"]
            self.assertEqual(self.session["id"], session["id"])
            self.assertEqual("answering", session["status"])
            self.assertEqual("free", session["study"]["kind"])
        finally:
            http.shutdown(); http.server_close(); thread.join()


class UploadRedirectReviewTests(unittest.TestCase):
    def test_upload_does_not_forward_bearer_or_payload_on_redirect(self):
        hits = []
        class Receiver(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path); self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(302); self.send_header("Location", "/sink"); self.end_headers()
            def log_message(self, *args): pass
        http = ThreadingHTTPServer(("localhost", 0), Receiver)
        thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
        try:
            manager = UploadManager.__new__(UploadManager)
            manager.store = SimpleNamespace(export_json=lambda _: b'{"fixture": true}')
            manager.version = "0.6.1"; manager._attempts = lambda _: 0
            records = []; manager._record = lambda _, **values: records.append(values)
            self.assertFalse(manager._send("fixture-session", {"url": "http://localhost:{}/redirect".format(http.server_port), "code": "fixture-token"}))
            self.assertEqual([], hits)
            self.assertTrue(records[-1]["last_error"].startswith("http_302"))
        finally:
            http.shutdown(); http.server_close(); thread.join()
