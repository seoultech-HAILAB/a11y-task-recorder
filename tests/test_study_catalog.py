import csv
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/build_study_catalog.py"


class StudyCatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.spec = {"version": "fixture-v1", "templates": []}
        self.catalog = {"version": "fixture-v1", "origin": "https://study.example", "tasks": []}
        for number in range(25):
            template = {"template": f"PRIVATE-{number}", "site": "시험 사이트", "site_code": "TR",
                        "fe_code": "F1", "time_limit_min": 10, "instances": {}}
            for instance in "ab":
                detail = {"text": f"과업 {number} {instance} 지시문", "reference_answer": "PRIVATE ANSWER",
                          "scoring": "PRIVATE SCORING"}
                for variant in ("normal", "fe"):
                    case_id = f"case-{number}-{variant}"
                    detail[variant] = {"caseId": case_id}
                    task_id = "task-" + hashlib.sha256(
                        f"fixture-v1:PRIVATE-{number}:{instance}:{variant}".encode()).hexdigest()[:12]
                    self.catalog["tasks"].append({
                        "id": task_id, "originalTemplateId": template["template"], "instance": instance,
                        "variant": variant, "originalCaseId": case_id, "siteName": template["site"],
                        "goal": detail["text"], "referenceAnswer": detail["reference_answer"],
                        "scoring": detail["scoring"], "operatorPreparation": [],
                        "startUrl": "https://study.example/s/0123456789ab/index.html"})
                template["instances"][instance] = detail
            self.spec["templates"].append(template)
        self.rows = []
        for tester in range(1, 21):
            for order in range(1, 13):
                task = self.catalog["tasks"][(tester * 12 + order) % 100]
                self.rows.append({"tester": f"T{tester:02d}", "order": str(order),
                    "block": str((order - 1) // 4 + 1), "hosted_task_id": task["id"],
                    "template": task["originalTemplateId"], "instance": task["instance"],
                    "variant": task["variant"], "caseId": task["originalCaseId"],
                    "site": task["siteName"], "instruction": task["goal"],
                    "reference_answer": task["referenceAnswer"], "scoring": task["scoring"],
                    "time_limit_min": "10", "fe_code": "F1",
                    "task_code": f"TR-F1{task['instance']}-{'N' if task['variant'] == 'normal' else 'F'}"})

    def write_inputs(self):
        spec_bytes = (json.dumps(self.spec, ensure_ascii=False, indent=2) + "\n").encode()
        (self.root / "spec.json").write_bytes(spec_bytes)
        self.catalog.setdefault("sourceSpecSha256", hashlib.sha256(spec_bytes).hexdigest())
        (self.root / "operator.json").write_text(json.dumps(self.catalog), encoding="utf-8")
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=self.rows[0].keys())
        writer.writeheader()
        writer.writerows(self.rows)
        (self.root / "assignments.csv").write_bytes(stream.getvalue().encode())

    def run_generator(self):
        self.write_inputs()
        return subprocess.run([sys.executable, str(SCRIPT), "--assignments", str(self.root / "assignments.csv"),
            "--operator-catalog", str(self.root / "operator.json"), "--spec", str(self.root / "spec.json"),
            "--output", str(self.root / "public.json")], capture_output=True, text=True)

    def assert_rejected(self):
        result = self.run_generator()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "public.json").exists())
        self.assertIn("error:", result.stderr)

    def test_cli_preserves_all_assignments_without_private_fields(self):
        result = self.run_generator()
        self.assertEqual(result.returncode, 0, result.stderr)
        raw = (self.root / "public.json").read_bytes()
        public = json.loads(raw)
        self.assertEqual(public["schema_version"], 1)
        self.assertEqual([t["id"] for t in public["testers"]], [f"T{i:02d}" for i in range(1, 21)])
        tasks = [task for tester in public["testers"] for task in tester["tasks"]]
        self.assertEqual(len(tasks), 240)
        self.assertEqual(len({t["id"] for t in tasks}), 240)
        for actual, source in zip(tasks, self.rows):
            self.assertEqual(set(actual), {"id", "task_id", "order", "block", "site_name", "instruction",
                                           "start_url", "time_limit_seconds", "preparation"})
            self.assertEqual(actual["instruction"], source["instruction"])
            self.assertEqual(actual["task_id"], source["hosted_task_id"])
            self.assertEqual(actual["order"], int(source["order"]))
            self.assertEqual(actual["time_limit_seconds"], 600)
        for private in (b"PRIVATE", b"reference_answer", b"scoring", b"variant", b"task_code", b"fe_code"):
            self.assertNotIn(private, raw)
        self.assertEqual(public["source"]["assignments_sha256"],
                         hashlib.sha256((self.root / "assignments.csv").read_bytes()).hexdigest())
        self.assertEqual(self.run_generator().returncode, 0)
        self.assertEqual((self.root / "public.json").read_bytes(), raw)

    def test_rejects_each_assignment_contract_mismatch(self):
        for key in ("hosted_task_id", "template", "instance", "variant", "instruction", "site",
                    "reference_answer", "scoring", "caseId", "task_code", "fe_code", "time_limit_min", "block"):
            with self.subTest(key=key):
                original = self.rows[0][key]
                self.rows[0][key] = "tampered"
                self.assert_rejected()
                self.rows[0][key] = original

    def test_rejects_missing_duplicate_and_reordered_assignments(self):
        original = self.rows[:]
        for rows in (original[:-1], [original[0]] + original[:-1],
                     [original[1], original[0]] + original[2:]):
            self.rows = rows
            self.assert_rejected()

    def test_rejects_catalog_duplicate_missing_and_wrong_full_id(self):
        original = self.catalog["tasks"][:]
        for tasks in (original[:-1], original + [original[0]]):
            self.catalog["tasks"] = tasks
            self.assert_rejected()
        self.catalog["tasks"] = original
        original[0]["id"] += "-suffix"
        self.assert_rejected()

    def test_rejects_same_task_assigned_twice_to_one_tester(self):
        self.rows[1] = dict(self.rows[0], order="2")
        self.assert_rejected()

    def test_rejects_catalog_and_spec_mismatch(self):
        for key in ("goal", "referenceAnswer", "siteName", "scoring", "originalCaseId"):
            with self.subTest(key=key):
                original = self.catalog["tasks"][0][key]
                self.catalog["tasks"][0][key] = "tampered"
                self.assert_rejected()
                self.catalog["tasks"][0][key] = original
        self.catalog["sourceSpecSha256"] = "0" * 64
        self.assert_rejected()

    def test_rejects_unsafe_or_revealing_urls(self):
        for url in ("http://study.example/s/0123456789ab/index.html", "javascript:alert(1)",
                    "https://other.example/s/0123456789ab/index.html",
                    "https://secret@study.example/s/0123456789ab/index.html",
                    "https://study.example/s/0123456789ab/index.html?variant=fe",
                    "https://study.example/s/normal/index.html", "https://study.example/fe.html"):
            with self.subTest(url=url):
                self.catalog["tasks"][0]["startUrl"] = url
                self.assert_rejected()

    def test_rejects_unreviewed_operator_preparation(self):
        self.catalog["tasks"][0]["operatorPreparation"] = ["PRIVATE ANSWER or FE condition"]
        self.assert_rejected()

    def test_workspace_sources_reproduce_packaged_240_assignments(self):
        from scripts.build_study_catalog import build_catalog, DEFAULT_ASSIGNMENTS, DEFAULT_OPERATOR_CATALOG, DEFAULT_SPEC
        if not all(path.exists() for path in (DEFAULT_ASSIGNMENTS, DEFAULT_OPERATOR_CATALOG, DEFAULT_SPEC)):
            self.skipTest("Private workspace source documents are not available")
        actual = build_catalog(DEFAULT_ASSIGNMENTS, DEFAULT_OPERATOR_CATALOG, DEFAULT_SPEC)
        packaged = json.loads((ROOT / "recorder/study_catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(actual, packaged)
        self.assertEqual(sum(len(t["tasks"]) for t in actual["testers"]), 240)


if __name__ == "__main__":
    unittest.main()
