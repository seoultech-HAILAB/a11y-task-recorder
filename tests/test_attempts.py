"""0.4.4: restart a task, return to it from the result form, cancel on tester change."""
import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from recorder.common import ApiError, parse_timestamp, utc_now
from recorder.server import RecorderStore
from tests.test_study_flow import catalog


class AttemptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "catalog.json"
        path.write_text(json.dumps(catalog(("T01", "T02"))), encoding="utf-8")
        self.store = RecorderStore(Path(self.temp.name) / "data" / "attempts.sqlite3")
        self.store.study_catalog_path = path

    def started(self, tester="T01"):
        session = self.store.prepare_study(tester)
        return self.store.start_session(session["id"], study_ready=True)

    def nvda_event(self, session_id):
        return self.store.add_event({"session_id": session_id, "source": "nvda", "type": "input",
                                     "timestamp": utc_now(), "payload": {"gesture": "tab"}})

    def test_restart_during_a_task_keeps_the_attempt_and_prepares_a_fresh_one(self):
        first = self.started()
        self.nvda_event(first["id"])
        aborted = self.store.abort_task(first["id"], {"reason": "restart", "trigger": "button", "by": "participant"})
        self.assertEqual(("aborted", "restarted", "restart", "restart"),
                         (aborted["status"], aborted["study_outcome"], aborted["abort_reason"], aborted["end_reason"]))
        self.assertIsNotNone(aborted["aborted_at"])
        self.assertEqual(aborted["ended_at"], aborted["task_ended_at"])
        self.assertIsNotNone(aborted["net_task_seconds"])
        self.assertEqual("exclude", aborted["study_attempt"]["analysis"])
        self.assertIsNone(self.store.active_session())
        # Progress still points at task 1, now to be done again; nothing was deleted.
        plan = self.store.get_study("T01")
        self.assertEqual((1, 0, "pending"), (plan["current_order"], plan["finished"], plan["tasks"][0]["status"]))
        events = [event["type"] for event in self.store.list_events(first["id"])]
        self.assertEqual(["input", "task_abort"], events)
        abort = self.store.list_events(first["id"])[-1]["payload"]
        self.assertEqual(("restart", "active", "button", "participant"),
                         (abort["reason"], abort["from_status"], abort["trigger"], abort["by"]))
        second = self.store.prepare_study("T01")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(("draft", 2, first["group_id"]), (second["status"], second["round"], second["group_id"]))
        self.assertEqual({"attempt": 2, "retry_of": first["id"], "redo_reason": "", "next_attempt": None,
                          "analysis": "include"},
                         second["study_attempt"])
        self.assertEqual(second["id"], self.store.get_session(first["id"])["study_attempt"]["next_attempt"])
        self.assertEqual(second["id"], self.store.prepare_study("T01")["id"], "prepare is idempotent again")
        self.assertEqual(second["id"], self.store.current_study_session()["id"])
        started = self.store.start_session(second["id"], study_ready=True)
        self.assertEqual("active", started["status"])
        # Late collector events of the aborted attempt are not mixed into the new one.
        with self.assertRaises(ApiError):
            self.store.add_event({"session_id": first["id"], "source": "nvda", "type": "input",
                                  "timestamp": "2099-01-01T00:00:00Z", "payload": {}})

    def test_restart_from_the_result_form_keeps_how_the_task_was_ended(self):
        session = self.started()
        self.store.end_task(session["id"], {"reason": "participant_gesture"})
        aborted = self.store.abort_task(session["id"], {"reason": "restart", "trigger": "result_form"})
        self.assertEqual(("aborted", "participant_gesture"), (aborted["status"], aborted["end_reason"]))
        payload = self.store.list_events(session["id"])[-1]["payload"]
        self.assertEqual(("answering", "participant_gesture"), (payload["from_status"], payload["previous_end_reason"]))

    def test_abort_is_refused_outside_its_states_and_for_older_attempts(self):
        draft = self.store.prepare_study("T01")
        with self.assertRaises(ApiError):
            self.store.abort_task(draft["id"], {"reason": "restart"})
        with self.assertRaises(ApiError):
            self.store.abort_task(draft["id"], {"reason": "delete"})
        session = self.store.start_session(draft["id"], study_ready=True)
        with self.assertRaises(ApiError):
            self.store.abort_task(session["id"], {"reason": "tester_change"})
        self.store.abort_task(session["id"], {"reason": "restart"})
        with self.assertRaises(ApiError):
            self.store.abort_task(session["id"], {"reason": "restart"})
        saved = self.started()
        self.store.stop_session(saved["id"], {"status": "completed", "study_outcome": "completed", "entered_by": "participant"})
        with self.assertRaises(ApiError):
            self.store.abort_task(saved["id"], {"reason": "restart"})

    def test_tester_change_cancels_only_an_unstarted_draft(self):
        wrong = self.store.prepare_study("T01")
        cancelled = self.store.abort_task(wrong["id"], {"reason": "tester_change", "to_tester": "T02"})
        self.assertEqual(("aborted", "cancelled", None, None),
                         (cancelled["status"], cancelled["study_outcome"], cancelled["task_ended_at"], cancelled["net_task_seconds"]))
        self.assertEqual("T02", self.store.list_events(wrong["id"])[-1]["payload"]["to_tester"])
        right = self.store.prepare_study("T02")
        self.assertEqual(right["id"], self.store.current_study_session()["id"])
        # The right participant for T01 comes later: a fresh attempt, the cancelled draft kept.
        later = self.store.prepare_study("T01")
        self.assertEqual((2, wrong["id"]), (later["study_attempt"]["attempt"], later["study_attempt"]["retry_of"]))

    def test_return_to_the_task_does_not_count_the_result_form_time(self):
        session = self.started()
        with self.store.connect() as connection:
            connection.execute("UPDATE sessions SET started_at = ? WHERE id = ?", ("2026-10-28T00:00:00.000Z", session["id"]))
        self.store.end_task(session["id"], {"reason": "participant_button"})
        with self.store.connect() as connection:
            connection.execute("UPDATE sessions SET task_ended_at = ? WHERE id = ?", ("2026-10-28T00:01:00.000Z", session["id"]))
        resumed = self.store.resume_task(session["id"], {"by": "participant"})
        self.assertEqual("active", resumed["status"])
        gap = self.store.list_events(session["id"])[-1]["payload"]["gap_seconds"]
        self.assertGreater(gap, 0)
        self.assertAlmostEqual(gap, resumed["paused_seconds"], places=1)
        ended = self.store.end_task(session["id"], {"reason": "participant_gesture"})
        stopped = self.store.stop_session(session["id"], {"status": "completed", "study_outcome": "completed", "entered_by": "participant"})
        gross = (parse_timestamp(ended["task_ended_at"]) - parse_timestamp("2026-10-28T00:00:00.000Z")).total_seconds()
        self.assertAlmostEqual(gross - gap, stopped["net_task_seconds"], places=1)

    def test_time_limit_end_cannot_be_resumed_or_restarted(self):
        session = self.started()
        self.store.end_task(session["id"], {"reason": "time_limit"})
        with self.assertRaises(ApiError):
            self.store.resume_task(session["id"], {"by": "participant"})
        with self.assertRaises(ApiError):
            self.store.abort_task(session["id"], {"reason": "restart"})

    def test_export_and_package_show_aborted_attempts_apart(self):
        first = self.started()
        self.nvda_event(first["id"])
        self.store.abort_task(first["id"], {"reason": "restart", "trigger": "button"})
        second = self.started()
        self.store.end_task(second["id"], {"reason": "participant_gesture"})
        self.store.stop_session(second["id"], {"status": "completed", "study_outcome": "completed", "entered_by": "participant"})
        cancelled = self.store.prepare_study("T02")
        self.store.abort_task(cancelled["id"], {"reason": "tester_change", "to_tester": "T01"})

        record = json.loads(self.store.export_json(first["id"]))
        self.assertEqual("exclude", record["study_attempt"]["analysis"])
        self.assertFalse(record["data_quality"]["checks"]["attempt_valid"])
        self.assertFalse(record["data_quality"]["collection_check_passed"])
        self.assertTrue(record["data_quality"]["checks"]["session_finished"])
        self.assertTrue(json.loads(self.store.export_json(second["id"]))["data_quality"]["checks"]["attempt_valid"])

        package = self.store.build_result_package()
        self.assertEqual((1, 1), (package["session_count"], package["aborted_session_count"]))
        with zipfile.ZipFile(package["path"]) as bundle:
            names = bundle.namelist()
            info = bundle.read("패키지정보.txt").decode("utf-8")
            rows = list(csv.DictReader(io.StringIO(bundle.read("attempts.csv").decode("utf-8-sig"))))
        self.assertEqual(1, sum(1 for name in names if name.startswith("sessions/aborted/") and name.endswith(".json")))
        self.assertEqual(1, sum(1 for name in names if name.startswith("sessions/T01") and name.endswith(".json")))
        self.assertIn("다시 시작으로 취소된 시도: 1건", info)
        self.assertIn("테스터 번호 변경으로 취소된 준비 과업: 1건", info)
        by_session = {row["session_id"]: row for row in rows}
        self.assertEqual(("exclude", "restarted", "restart", "1"),
                         tuple(by_session[first["id"]][key] for key in ("analysis", "study_outcome", "abort_reason", "attempt")))
        self.assertEqual(("include", first["id"], "2"),
                         tuple(by_session[second["id"]][key] for key in ("analysis", "retry_of", "attempt")))
        self.assertEqual(("exclude", "cancelled", "tester_change"),
                         tuple(by_session[cancelled["id"]][key] for key in ("analysis", "study_outcome", "abort_reason")))

    def test_aborted_attempts_are_not_queued_for_upload(self):
        from recorder.upload import UploadManager
        session = self.started()
        self.store.abort_task(session["id"], {"reason": "restart"})
        self.assertFalse(UploadManager(self.store).enqueue(session["id"]))


if __name__ == "__main__":
    unittest.main()
