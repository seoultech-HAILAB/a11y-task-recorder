"""0.4.0 single-window flow: end-task / resume, pauses, participant answers, signals."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from recorder.common import ApiError
from recorder.server import RecorderHTTPServer, RecorderStore, utc_now
from recorder.signals import SignalBoard


def catalog(testers=("T01",)):
    return {
        "schema_version": 1, "id": "catalog-flow", "title": "Study",
        "source": {"spec_version": "v1", "spec_sha256": "a" * 64, "assignments_sha256": "b" * 64},
        "testers": [{"id": tester, "tasks": [
            {"id": f"{tester}-a{n}", "task_id": f"t{n}", "order": n, "block": (n - 1) // 4 + 1, "site_name": "Site",
             "instruction": f"Find item {n}", "start_url": f"https://example.com/{n}",
             "time_limit_seconds": 600, "preparation": [], "requires_answer": n % 2 == 1}
            for n in range(1, 13)]} for tester in testers],
    }


class StudyFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "catalog.json"
        path.write_text(json.dumps(catalog()), encoding="utf-8")
        self.store = RecorderStore(Path(self.temp.name) / "flow.sqlite3")
        self.store.study_catalog_path = path

    def started(self):
        session = self.store.prepare_study("T01")
        return self.store.start_session(session["id"], study_ready=True)

    def nvda_event(self, session_id, timestamp=None):
        return self.store.add_event({"session_id": session_id, "source": "nvda", "type": "input",
                                     "timestamp": timestamp or utc_now(), "payload": {"gesture": "tab"}})

    def test_requires_answer_is_exposed_without_answers(self):
        plan = self.store.get_study("T01")
        self.assertEqual([True, False], [task["requires_answer"] for task in plan["tasks"][:2]])
        self.assertNotIn("reference_answer", json.dumps(plan))

    def test_end_task_seals_recording_and_stop_records_participant_answer(self):
        session = self.started()
        self.nvda_event(session["id"])
        ended = self.store.end_task(session["id"], {"reason": "participant_gesture"})
        self.assertEqual("answering", ended["status"])
        self.assertEqual("participant_gesture", ended["end_reason"])
        self.assertIsNotNone(ended["task_ended_at"])
        self.assertIsNone(self.store.active_session())
        with self.assertRaises(ApiError):
            self.store.add_event({"session_id": session["id"], "source": "nvda", "type": "input",
                                  "timestamp": "2099-01-01T00:00:00Z", "payload": {}})
        progress = self.store.get_study("T01")
        self.assertEqual("answering", progress["tasks"][0]["status"])
        self.assertEqual(1, progress["current_order"])
        stopped = self.store.stop_session(session["id"], {
            "status": "completed", "study_outcome": "completed", "answer": "두부", "entered_by": "participant"})
        self.assertEqual("completed", stopped["status"])
        self.assertEqual("participant", stopped["study_answer_entered_by"])
        self.assertEqual(ended["task_ended_at"], stopped["task_ended_at"])
        self.assertIsNotNone(stopped["net_task_seconds"])
        self.assertEqual(1, len(stopped["answer_history"]))
        types = [event["type"] for event in self.store.list_events(session["id"])]
        self.assertEqual(["input", "task_end", "answer_saved"], types)
        self.assertEqual(2, self.store.get_study("T01")["current_order"])

    def test_participant_button_end_and_system_time_limit_save(self):
        session = self.started()
        ended = self.store.end_task(session["id"], {"reason": "participant_button"})
        self.assertEqual("participant_button", ended["end_reason"])
        stopped = self.store.stop_session(session["id"], {
            "status": "abandoned", "study_outcome": "timeout", "answer": "", "entered_by": "system"})
        self.assertEqual(("timeout", "system"), (stopped["study_outcome"], stopped["study_answer_entered_by"]))
        with self.assertRaises(ApiError):
            self.store.end_task(self.started()["id"], {"reason": "operator_panel"})

    def test_pre_boundary_nvda_event_is_still_accepted_during_answering(self):
        session = self.started()
        before = utc_now()
        ended = self.store.end_task(session["id"], {"reason": "facilitator_button"})
        late = self.nvda_event(session["id"], before)
        self.assertTrue(late["payload"]["received_after_stop"])
        self.assertEqual("answering", self.store.get_session(ended["id"])["status"])

    def test_resume_returns_to_recording_and_keeps_both_events(self):
        session = self.started()
        self.store.end_task(session["id"], {"reason": "participant_gesture"})
        resumed = self.store.resume_task(session["id"], {"by": "facilitator"})
        self.assertEqual("active", resumed["status"])
        self.assertIsNone(resumed["task_ended_at"])
        self.assertEqual("", resumed["end_reason"])
        self.nvda_event(session["id"])
        self.store.end_task(session["id"], {"reason": "time_limit"})
        types = [event["type"] for event in self.store.list_events(session["id"])]
        self.assertEqual(["task_end", "task_resume", "input", "task_end"], types)
        self.store.stop_session(session["id"], {"status": "abandoned", "study_outcome": "timeout"})
        with self.assertRaises(ApiError):
            self.store.resume_task(session["id"], {})

    def test_pauses_are_subtracted_from_net_task_seconds(self):
        session = self.started()
        paused = self.store.pause_task(session["id"], {"reason": "break"})
        self.assertIsNotNone(paused["paused_at"])
        with self.assertRaises(ApiError):
            self.store.pause_task(session["id"], {"reason": "break"})
        with self.store.connect() as connection:
            connection.execute("UPDATE sessions SET paused_at = ? WHERE id = ?",
                               ("2026-10-12T00:00:00.000Z", session["id"]))
            connection.execute("UPDATE sessions SET started_at = ? WHERE id = ?",
                               ("2026-10-11T23:59:00.000Z", session["id"]))
        with self.store.connect() as connection:
            row = connection.execute("SELECT * FROM sessions WHERE id = ?", (session["id"],)).fetchone()
            self.store._close_pause(connection, row, "2026-10-12T00:02:00.000Z", auto=False)
        ended = self.store.end_task(session["id"], {"reason": "facilitator_button"})
        stopped = self.store.stop_session(session["id"], {"status": "completed", "study_outcome": "completed"})
        from recorder.common import parse_timestamp
        gross = (parse_timestamp(ended["task_ended_at"]) - parse_timestamp("2026-10-11T23:59:00.000Z")).total_seconds()
        self.assertAlmostEqual(gross - 120.0, stopped["net_task_seconds"], places=1)
        events = {event["type"]: event for event in self.store.list_events(session["id"])}
        self.assertEqual(120.0, events["resume"]["payload"]["paused_seconds"])
        self.assertFalse(events["resume"]["payload"]["auto"])

    def test_direct_stop_from_active_fills_task_boundary(self):
        session = self.started()
        stopped = self.store.stop_session(session["id"], {"status": "abandoned", "study_outcome": "technical"})
        self.assertEqual(stopped["ended_at"], stopped["task_ended_at"])
        self.assertEqual("facilitator_stop", stopped["end_reason"])
        self.assertEqual("facilitator", stopped["study_answer_entered_by"])

    def test_answer_can_be_corrected_until_next_task_is_prepared(self):
        session = self.started()
        self.store.end_task(session["id"], {"reason": "participant_gesture"})
        self.store.stop_session(session["id"], {"status": "abandoned", "study_outcome": "blocked",
                                                "answer": "", "entered_by": "participant"})
        fixed = self.store.update_answer(session["id"], {"study_outcome": "completed", "answer": "두부",
                                                         "entered_by": "facilitator"})
        self.assertEqual("completed", fixed["status"])
        self.assertEqual("두부", fixed["study_answer"])
        self.assertEqual(["participant", "facilitator"], [entry["entered_by"] for entry in fixed["answer_history"]])
        with self.assertRaises(ApiError):
            self.store.update_answer(session["id"], {"study_outcome": "timeout"})
        self.store.prepare_study("T01")
        with self.assertRaises(ApiError):
            self.store.update_answer(session["id"], {"study_outcome": "blocked"})

    def test_study_events_are_dashboard_only_and_type_checked(self):
        session = self.store.prepare_study("T01")
        event = self.store.log_study_event(session["id"], "instruction_repeat", {"method": "nvda"})
        self.assertEqual("dashboard", event["source"])
        with self.assertRaises(ApiError):
            self.store.log_study_event(session["id"], "input", {})
        manual = self.store.create_session({"title": "manual"})
        with self.assertRaises(ApiError):
            self.store.log_study_event(manual["id"], "pause", {})

    def test_signal_board_follows_task_state(self):
        board = SignalBoard()
        self.assertFalse(board.decide("start", "nvda_addon", None)["accepted"])
        draft = self.store.prepare_study("T01")
        self.assertTrue(board.decide("start", "nvda_addon", draft)["accepted"])
        self.assertFalse(board.decide("end", "nvda_addon", draft)["accepted"])
        repeat = board.decide("repeat", "nvda_addon", draft)
        # 0.5.0: the scenario sentence alone; how to end is taught in the practice only.
        self.assertEqual("Find item 1", repeat["speak"])
        answering = board.decide("repeat", "nvda_addon", {"id": draft["id"], "status": "answering", "scenario": "Find item 1"})
        self.assertEqual("Find item 1", answering["speak"])
        active = self.store.start_session(draft["id"], study_ready=True)
        self.assertFalse(board.decide("start", "nvda_addon", active)["accepted"])
        end = board.decide("end", "nvda_addon", active)
        self.assertTrue(end["accepted"])
        self.assertEqual("", end["speak"])  # focus mode: the dashboard reads the prompt
        self.assertFalse(board.decide("jump", "nvda_addon", active)["accepted"])
        since = board.since(1)
        self.assertEqual(["repeat", "repeat", "end"], [signal["kind"] for signal in since["signals"]])
        board.practice = True
        practice = board.decide("repeat", "nvda_addon", None)
        self.assertTrue(practice["accepted"])
        # The practice task sentence alone, as a real task's repeat (study.js PRACTICE_TASK).
        self.assertEqual("연습 화면에서 '재료 보기' 버튼을 눌러 첫 번째 재료를 확인하세요.", practice["speak"])
        self.assertNotIn("NVDA", practice["speak"])

    def test_dashboard_csp_allows_plain_http_partner_copies_from_the_catalog(self):
        # Our copies are https (always allowed); a partner's test copy over plain http (SNC)
        # is allowed by its exact catalog origin, and nothing else is added.
        self.assertEqual([], self.store.study_frame_origins())
        data = catalog()
        data["testers"][0]["tasks"][0]["start_url"] = "http://partner.example.test/koref/web/user/"
        data["testers"][0]["tasks"][1]["start_url"] = "http://partner.example.test:8080/cj/"
        data["testers"][0]["tasks"][2]["start_url"] = "http://localhost:8765/practice.html"
        path = Path(self.temp.name) / "partner.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        store = RecorderStore(Path(self.temp.name) / "partner.sqlite3")
        store.study_catalog_path = path
        self.assertEqual(["http://partner.example.test", "http://partner.example.test:8080"], store.study_frame_origins())
        server = RecorderHTTPServer(("127.0.0.1", 0), store)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with urlopen(Request("http://127.0.0.1:%s/" % server.server_port, headers={"User-Agent": "test"})) as response:
                csp = response.headers["Content-Security-Policy"]
        finally:
            server.shutdown()
            server.server_close()
        self.assertIn("frame-src 'self' https: http://127.0.0.1:* http://localhost:*"
                      " http://partner.example.test http://partner.example.test:8080", csp)

    def test_http_flow_end_answer_signals(self):
        server = RecorderHTTPServer(("127.0.0.1", 0), self.store)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def request(path, body=None, method=None):
            data = None if body is None else json.dumps(body).encode()
            req = Request("http://127.0.0.1:%s%s" % (server.server_port, path), data=data, method=method,
                          headers={"Content-Type": "application/json", "User-Agent": "test"})
            try:
                with urlopen(req) as response:
                    return response.status, json.load(response)
            except HTTPError as response:
                with response:
                    return response.code, json.load(response)
        try:
            self.assertEqual("single_window", request("/api/health")[1]["ui_mode"])
            status, body = request("/api/signals", {"kind": "start"})
            self.assertFalse(body["accepted"])
            session = request("/api/study/T01/prepare", {})[1]["session"]
            path = "/api/sessions/" + session["id"]
            status, body = request("/api/signals", {"kind": "repeat"})
            self.assertTrue(body["accepted"])
            self.assertEqual("Find item 1", body["speak"])
            pending = request("/api/signals?after=0")[1]
            self.assertEqual(["repeat"], [signal["kind"] for signal in pending["signals"]])
            self.store.start_session(session["id"], study_ready=True)
            self.assertEqual(409, request(path + "/resume-task", {})[0])
            self.assertEqual(200, request(path + "/pause", {"reason": "technical_support"})[0])
            self.assertEqual(200, request(path + "/resume", {})[0])
            status, body = request(path + "/study-events", {"type": "time_limit_reached", "payload": {"net": 600}})
            self.assertEqual(201, status)
            self.assertEqual(200, request(path + "/end-task", {"reason": "time_limit"})[0])
            self.assertEqual(409, request(path + "/end-task", {"reason": "time_limit"})[0])
            status, body = request(path + "/stop", {"status": "abandoned", "study_outcome": "timeout"})
            self.assertEqual(200, status)
            self.assertEqual("time_limit", body["session"]["end_reason"])
            self.assertEqual(409, request(path + "/answer", {"study_outcome": "completed"}, "PATCH")[0])
            self.assertEqual(200, request("/api/signals/practice", {"on": True})[0])
            self.assertTrue(request("/api/signals", {"kind": "start"})[1]["accepted"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
