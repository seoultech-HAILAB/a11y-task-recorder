"""0.6.0 free task: after the assigned tasks, a site and goal the tester chooses, kept apart."""
import json
import tempfile
import unittest
from pathlib import Path

from recorder.common import ApiError
from recorder.free_task import free_url
from recorder.server import RecorderStore


def catalog():
    return {"schema_version": 1, "id": "catalog-free", "title": "Study",
            "source": {"spec_version": "v1", "spec_sha256": "a" * 64, "assignments_sha256": "b" * 64},
            "testers": [{"id": "T01", "tasks": [
                {"id": "T01-a%d" % n, "task_id": "t%d" % n, "order": n, "block": 1, "site_name": "Site",
                 "instruction": "Find item %d" % n, "start_url": "https://study.example.test/s/0123456789ab/",
                 "time_limit_seconds": 600, "preparation": []} for n in (1, 2)]}]}


class FreeTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "catalog.json"
        path.write_text(json.dumps(catalog()), encoding="utf-8")
        self.store = RecorderStore(Path(self.temp.name) / "free.sqlite3")
        self.store.study_catalog_path = path

    def finish(self, session, outcome="completed"):
        self.store.start_session(session["id"], study_ready=True)
        self.store.end_task(session["id"], {"reason": "participant_gesture"})
        return self.store.stop_session(session["id"], {
            "status": "completed" if outcome == "completed" else "abandoned", "study_outcome": outcome, "answer": ""})

    def finish_assigned(self):
        for _ in range(2):
            self.finish(self.store.prepare_study("T01"))

    def test_only_after_every_assigned_task(self):
        self.finish(self.store.prepare_study("T01"))
        with self.assertRaises(ApiError) as caught:
            self.store.create_free_task("T01", {"url": "www.example.com", "goal": "날씨 보기"})
        self.assertEqual(409, caught.exception.status)

    def test_free_task_runs_and_keeps_assignment_progress(self):
        self.finish_assigned()
        session = self.store.create_free_task("T01", {"url": "www.example.com/a?q=1", "goal": "  오늘  날씨 보기 "})
        self.assertEqual("https://www.example.com/a?q=1", session["target_url"])
        self.assertEqual("오늘 날씨 보기", session["scenario"])
        self.assertEqual(("free", 1, "free_tab"), (session["study"]["kind"], session["study"]["free_index"],
                                                     session["study"]["ui_mode"]))
        done = self.finish(session, "blocked")
        self.assertEqual("abandoned", done["status"])
        plan = self.store.get_study("T01")
        self.assertEqual((2, 2, None), (plan["finished"], plan["total"], plan["current_order"]))
        second = self.store.create_free_task("T01", {"url": "https://news.example.org/", "goal": "기사 읽기"})
        self.assertEqual(2, second["study"]["free_index"])

    def test_an_unstarted_free_task_is_replaced(self):
        self.finish_assigned()
        first = self.store.create_free_task("T01", {"url": "www.example.com", "goal": "하나"})
        second = self.store.create_free_task("T01", {"url": "www.example.com", "goal": "둘"})
        self.assertEqual(("aborted", "free_task_replaced"),
                         (self.store.get_session(first["id"])["status"], self.store.get_session(first["id"])["abort_reason"]))
        self.assertEqual(1, second["study"]["free_index"])
        self.store.start_session(second["id"], study_ready=True)
        with self.assertRaises(ApiError):
            self.store.create_free_task("T01", {"url": "www.example.com", "goal": "셋"})

    def test_url_and_goal_are_checked(self):
        self.assertEqual("http://example.com/", free_url("http://example.com/"))
        for bad in ["", "ftp://example.com/", "https://user:pw@example.com/", "https://exa mple.com/",
                    "http://127.0.0.1:8765/", "javascript:alert(1)"]:
            with self.subTest(bad), self.assertRaises(ApiError):
                free_url(bad)
        self.finish_assigned()
        with self.assertRaises(ApiError):
            self.store.create_free_task("T01", {"url": "www.example.com", "goal": ""})
        with self.assertRaises(ApiError):
            self.store.create_free_task("T01", {"url": "www.example.com", "goal": "x", "tester": "T02"})


if __name__ == "__main__":
    unittest.main()
