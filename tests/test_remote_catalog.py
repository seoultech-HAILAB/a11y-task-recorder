"""0.6.0 remote task assignment: the study server's catalog replaces the kit's at the next preparation."""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from recorder.remote_catalog import RemoteCatalog
from recorder.server import RecorderStore
from recorder.upload import UploadManager

CODE = "fake-kit-code-for-tests-0002"
SITE = "https://study.example.test/s/0123456789ab/"


class FakeCatalogServer:
    """Stands in for the study server's GET /study/catalog/<id>."""

    def __init__(self):
        self.catalog = None
        self.redirect_to = None
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization")})
                if outer.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", outer.redirect_to)
                    self.end_headers()
                    return
                if self.headers.get("Authorization") != "Bearer " + CODE:
                    status, data = 401, b'{"error":"unauthorized"}'
                elif outer.catalog is None:
                    status, data = 404, b'{"error":"not_found"}'
                else:
                    status, data = 200, json.dumps(outer.catalog).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = "http://127.0.0.1:%s" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def task(n, **extra):
    data = {"id": "T01-a%d" % n, "task_id": "t%d" % n, "order": n, "block": 1, "site_name": "Site",
            "instruction": "Find item %d" % n, "start_url": SITE + "p%d.html" % n,
            "time_limit_seconds": 600, "preparation": []}
    data.update(extra)
    return data


def catalog(tasks, revision=None):
    data = {"schema_version": 1, "id": "catalog-remote", "title": "Study",
            "source": {"spec_version": "v1", "spec_sha256": "a" * 64, "assignments_sha256": "b" * 64},
            "testers": [{"id": "T01", "tasks": tasks}]}
    if revision is not None:
        data["revision"] = revision
    return data


class RemoteCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fake = FakeCatalogServer()
        self.addCleanup(self.fake.close)
        kit = catalog([task(1), task(2), task(3)])
        kit["upload_url"] = self.fake.origin + "/upload/session"
        path = Path(self.temp.name) / "catalog.json"
        path.write_text(json.dumps(kit), encoding="utf-8")
        self.store = RecorderStore(Path(self.temp.name) / "remote.sqlite3")
        self.store.study_catalog_path = path
        self.uploads = UploadManager(self.store, enabled=True, upload_url="", builtin_code=CODE)
        self.remote = RemoteCatalog(self.store, self.uploads)
        self.store.remote_catalog = self.remote

    def finish(self, session):
        self.store.start_session(session["id"], study_ready=True)
        self.store.stop_session(session["id"], {"status": "completed", "study_outcome": "completed", "answer": "x"})

    def orders(self):
        return [(t["task_id"], t["status"]) for t in self.store.get_study("T01")["tasks"]]

    def test_url_uses_the_upload_origin_kit_id_and_bearer_code(self):
        self.assertEqual(self.fake.origin + "/study/catalog/catalog-remote", self.remote.url())
        self.assertEqual({"status": 404}, self.remote.refresh())
        self.assertEqual("Bearer " + CODE, self.fake.requests[0]["auth"])
        self.assertEqual("", self.remote.status()["last_error"])

    def test_new_revision_applies_at_the_next_preparation_and_keeps_saved_results(self):
        first = self.store.prepare_study("T01")
        self.finish(first)
        # Server: task 2 changes its instruction, task 3 is dropped, a new task 4 is added.
        self.fake.catalog = catalog([task(1), task(2, instruction="Find item two"), task(3, id="T01-a4", task_id="t4")],
                                    revision=1)
        self.assertEqual({"revision": 1}, self.remote.refresh())
        self.assertEqual(1, self.remote.status()["pending_revision"])
        self.assertEqual(0, self.remote.status()["revision"])
        second = self.store.prepare_study("T01")
        self.assertEqual("Find item two", second["scenario"])
        self.assertEqual(1, second["study"]["catalog_revision"])
        self.assertEqual([("t1", "finished"), ("t2", "draft"), ("t4", "pending")], self.orders())
        self.assertEqual(1, self.remote.status()["revision"])
        self.assertIsNone(self.remote.status()["pending_revision"])
        self.assertEqual("completed", self.store.get_session(first["id"])["status"])

    def test_a_changed_draft_is_prepared_again_and_an_unchanged_one_is_kept(self):
        draft = self.store.prepare_study("T01")
        self.fake.catalog = catalog([task(1), task(2)], revision=1)
        self.remote.refresh()
        self.assertEqual(draft["id"], self.store.prepare_study("T01")["id"])
        self.fake.catalog = catalog([task(1, instruction="Changed"), task(2)], revision=2)
        self.remote.refresh()
        again = self.store.prepare_study("T01")
        self.assertNotEqual(draft["id"], again["id"])
        self.assertEqual("Changed", again["scenario"])
        old = self.store.get_session(draft["id"])
        self.assertEqual(("aborted", "catalog_update"), (old["status"], old["abort_reason"]))

    def test_nothing_changes_while_a_task_is_under_way(self):
        session = self.store.prepare_study("T01")
        self.store.start_session(session["id"], study_ready=True)
        self.fake.catalog = catalog([task(1, instruction="Changed")], revision=1)
        self.remote.refresh()
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.assertIsNone(self.remote.apply_pending(connection))
        self.assertEqual(0, self.remote.status()["revision"])
        self.assertEqual(1, self.remote.status()["pending_revision"])

    def test_rejected_catalogs_leave_the_kit_catalog_in_use(self):
        cases = {
            "other site": catalog([task(1, start_url="https://evil.example.test/s/0123456789ab/")], revision=1),
            "no revision": catalog([task(1)]),
            "other id": dict(catalog([task(1)], revision=1), id="catalog-other"),
            "bad order": catalog([task(2)], revision=1),
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.fake.catalog = data
                self.assertEqual({"error": "invalid"}, self.remote.refresh())
                self.assertTrue(self.remote.status()["last_error"].startswith("invalid"))
                self.assertIsNone(self.remote.status()["pending_revision"])

    def test_upload_url_cannot_be_redirected(self):
        data = catalog([task(1)], revision=1)
        data["upload_url"] = "https://evil.example.test/upload/session"
        self.fake.catalog = data
        self.remote.refresh()
        self.store.prepare_study("T01")
        self.assertEqual(self.fake.origin + "/upload/session", self.uploads.settings()["url"])

    def test_an_assignment_with_results_cannot_change_its_task(self):
        self.finish(self.store.prepare_study("T01"))
        self.fake.catalog = catalog([task(1, task_id="other"), task(2)], revision=1)
        self.remote.refresh()
        self.store.prepare_study("T01")
        self.assertEqual(0, self.remote.status()["revision"])
        self.assertIn("changed its task", self.remote.status()["last_error"])

    def test_unreachable_server_is_only_noted(self):
        self.uploads.env_url = "http://127.0.0.1:9/upload/session"
        self.store.study_catalog_path.write_text(
            json.dumps(dict(catalog([task(1)]), upload_url="http://127.0.0.1:9/upload/session")), encoding="utf-8")
        result = self.remote.refresh()
        self.assertIn("error", result)
        self.assertTrue(self.remote.status()["last_error"])
        self.assertEqual("t1", self.store.prepare_study("T01")["study"]["task_id"])

    def test_newer_pending_revision_is_not_replaced_by_a_later_stale_response(self):
        self.fake.catalog = catalog([task(1, instruction="Newest instruction")], revision=3)
        self.remote.refresh()
        self.fake.catalog = catalog([task(1, instruction="Stale instruction")], revision=2)
        self.remote.refresh()
        self.assertEqual(3, self.remote.status()["pending_revision"])
        session = self.store.prepare_study("T01")
        self.assertEqual("Newest instruction", session["scenario"])

    def test_same_pending_revision_cannot_silently_change_its_content(self):
        self.fake.catalog = catalog([task(1, instruction="First revision content")], revision=2)
        self.remote.refresh()
        self.fake.catalog = catalog([task(1, instruction="Changed without revision")], revision=2)
        self.remote.refresh()
        self.assertEqual("revision_conflict", self.remote.status()["last_error"])
        self.assertEqual("First revision content", self.store.prepare_study("T01")["scenario"])

    def test_started_assignment_cannot_be_relabelled_as_a_different_goal(self):
        first = self.store.prepare_study("T01")
        self.finish(first)
        self.fake.catalog = catalog([task(1, instruction="A different goal"), task(2)], revision=1)
        self.remote.refresh()
        second = self.store.prepare_study("T01")
        self.assertEqual(0, self.remote.status()["revision"])
        self.assertIn("changed executed content", self.remote.status()["last_error"])
        self.assertEqual("t2", second["study"]["task_id"])
        self.assertEqual("Find item 1", self.store.get_session(first["id"])["scenario"])

    def test_executed_url_preparation_limit_and_answer_contract_are_immutable(self):
        self.finish(self.store.prepare_study("T01"))
        for field, value in [("start_url", SITE + "different.html"),
                             ("preparation", ["different precondition"]),
                             ("time_limit_seconds", 30), ("requires_answer", False)]:
            with self.subTest(field=field):
                self.fake.catalog = catalog([task(1, **{field: value}), task(2)], revision=1)
                self.remote.refresh()
                self.store.prepare_study("T01")
                self.assertEqual(0, self.remote.status()["revision"])
                self.assertIn("changed executed content", self.remote.status()["last_error"])

    def test_removed_assignment_cannot_return_with_a_different_recorded_goal(self):
        first = self.store.prepare_study("T01")
        self.finish(first)
        self.fake.catalog = catalog([task(1, id="T01-a2", task_id="t2")], revision=1)
        self.remote.refresh()
        self.store.prepare_study("T01")
        self.assertEqual(1, self.remote.status()["revision"])
        self.fake.catalog = catalog([task(1, instruction="Changed after removal"), task(2)], revision=2)
        self.remote.refresh()
        self.store.prepare_study("T01")
        self.assertEqual(1, self.remote.status()["revision"])
        self.assertIn("changed executed content", self.remote.status()["last_error"])

    def test_assignment_with_history_cannot_move_to_a_different_tester(self):
        self.finish(self.store.prepare_study("T01"))
        changed = catalog([task(1, id="new-assignment", task_id="new-task")], revision=1)
        changed["testers"].append({"id": "T02", "tasks": [task(1)]})
        self.fake.catalog = changed
        self.remote.refresh()
        self.store.prepare_study("T01")
        self.assertEqual(0, self.remote.status()["revision"])
        self.assertIn("changed its tester", self.remote.status()["last_error"])

    def test_catalog_redirect_never_forwards_the_kit_bearer(self):
        receiver = FakeCatalogServer()
        self.addCleanup(receiver.close)
        receiver.catalog = catalog([task(1)], revision=1)
        self.fake.redirect_to = receiver.origin + "/study/catalog/catalog-remote"
        self.assertEqual({"status": 302}, self.remote.refresh())
        self.assertEqual([], receiver.requests)
        self.assertIsNone(self.remote.status()["pending_revision"])

    def test_remote_tasks_must_be_runnable_before_revision_is_queued(self):
        cases = [("start_url", SITE + "page.html?query=fixture"),
                 ("start_url", SITE + "page.html#section"),
                 ("start_url", "https://user:password@study.example.test/page.html"),
                 ("start_url", "https://study.example.test:invalid/page.html"),
                 ("time_limit_seconds", 0), ("time_limit_seconds", -1),
                 ("time_limit_seconds", "600"), ("time_limit_seconds", True),
                 ("requires_answer", "false")]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.fake.catalog = catalog([task(1, **{field: value})], revision=1)
                self.assertEqual({"error": "invalid"}, self.remote.refresh())
                self.assertIsNone(self.remote.status()["pending_revision"])
                self.assertEqual(0, self.remote.status()["revision"])

    def test_origin_comparison_uses_case_and_default_port_equivalence(self):
        self.fake.catalog = catalog([task(1, start_url="https://STUDY.example.test:443/page.html")], revision=1)
        self.assertEqual({"revision": 1}, self.remote.refresh())
        self.assertEqual(1, self.remote.status()["pending_revision"])
        self.fake.catalog = catalog([task(1, start_url="https://study.example.test:444/page.html")], revision=2)
        self.assertEqual({"error": "invalid"}, self.remote.refresh())
        self.assertEqual(1, self.remote.status()["pending_revision"])


if __name__ == "__main__":
    unittest.main()
