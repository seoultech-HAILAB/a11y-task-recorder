"""Validate frozen study assignments and export only participant-safe task fields."""

import argparse
import csv
import hashlib
import io
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
SOURCE_DIR = WORKSPACE / "a11y-research/docs/261007_scenario_plan"
DEFAULT_ASSIGNMENTS = SOURCE_DIR / "tester_tasks.csv"
DEFAULT_SPEC = SOURCE_DIR / "scenario_spec_final.json"
DEFAULT_OPERATOR_CATALOG = WORKSPACE / (
    "a11y-user-context-lab/runs/hosted-benchmark-v3-perfe-20261009/operator-catalog.private.json"
)
DEFAULT_OUTPUT = ROOT / "recorder/study_catalog.json"

# Only this reviewed setup text is safe to carry out of the private catalog.
CART_PREPARATION = (
    "기록 시작 전에 일반 화면 조작으로 팬트리 수납 바구니 1호 3개를 담고 장바구니를 연다. "
    "준비 조작은 참여자 수행에 포함하지 않는다."
)
# The repository is public: the packaged catalog names this placeholder origin, and
# build_release_kit.py writes the real copy origin into the kit only.
PUBLIC_ORIGIN = "https://study-copy.invalid"
HOSTED_PATH = re.compile(r"/s/[0-9a-f]{12}/(?:index\.html|p/[0-9a-f]{16}\.html)")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encode(document):
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def validate_url(value, origin):
    require(isinstance(value, str) and value == value.strip(), "Invalid start URL")
    parsed = urlsplit(value)
    require(parsed.scheme == "https" and parsed.netloc == origin.netloc
            and not parsed.username and not parsed.password and not parsed.query
            and not parsed.fragment and HOSTED_PATH.fullmatch(parsed.path),
            "Start URL must use the HTTPS catalog origin and an opaque hosted path")
    return value


def build_catalog(assignments_path=DEFAULT_ASSIGNMENTS, operator_catalog_path=DEFAULT_OPERATOR_CATALOG,
                  spec_path=DEFAULT_SPEC):
    spec_bytes = Path(spec_path).read_bytes()
    assignments_bytes = Path(assignments_path).read_bytes()
    spec = json.loads(spec_bytes)
    catalog = json.loads(Path(operator_catalog_path).read_bytes())
    require(catalog["version"] == spec["version"], "Catalog version does not match specification")
    require(catalog["sourceSpecSha256"] == digest(spec_bytes), "Specification SHA256 mismatch")
    origin = urlsplit(catalog["origin"])
    require(origin.scheme == "https" and origin.hostname and not origin.username
            and not origin.password and origin.path in ("", "/") and not origin.query
            and not origin.fragment, "Catalog origin must be an HTTPS origin")

    templates = {}
    expected = {}
    require(len(spec["templates"]) == 25, "Specification must contain 25 templates")
    for template in spec["templates"]:
        template_id = template["template"]
        require(template_id not in templates, "Duplicate specification template")
        templates[template_id] = template
        require(template["time_limit_min"] == 10, "Unsupported specification time limit")
        for instance in "ab":
            for variant in ("normal", "fe"):
                expected[(template_id, instance, variant)] = template["instances"][instance]

    tasks = {}
    seen_combinations = set()
    require(len(catalog["tasks"]) == 100, "Catalog must contain exactly 100 tasks")
    for task in catalog["tasks"]:
        task_id = task["id"]
        key = (task["originalTemplateId"], task["instance"], task["variant"])
        require(key in expected and key not in seen_combinations, "Invalid or duplicate catalog combination")
        expected_id = "task-" + digest(f"{spec['version']}:{':'.join(key)}".encode("utf-8"))[:12]
        require(task_id == expected_id and task_id not in tasks, "Hosted task ID mismatch or duplicate")
        detail = expected[key]
        template = templates[key[0]]
        require(task["goal"] == detail["text"] and task["referenceAnswer"] == detail["reference_answer"]
                and task["scoring"] == detail["scoring"] and task["siteName"] == template["site"]
                and task["originalCaseId"] == detail[key[2]]["caseId"], "Catalog task differs from specification")
        require(isinstance(task["goal"], str) and bool(task["goal"])
                and isinstance(task["siteName"], str) and bool(task["siteName"]),
                "Missing instruction or site name")
        require(not re.search(r"\b(?:F[1-5]|FE|normal|fe)\b|(?:PA|DA|CJ|LC|KO)-B-", task["siteName"]),
                "Site name exposes a private condition")
        validate_url(task["startUrl"], origin)
        preparation = task["operatorPreparation"]
        require(preparation == [] or (key[0] == "DA-B-LBL" and preparation == [CART_PREPARATION]),
                "Unreviewed operator preparation cannot be published")
        tasks[task_id] = task
        seen_combinations.add(key)
    require(seen_combinations == set(expected), "Catalog combinations are incomplete")

    reader = csv.DictReader(io.StringIO(assignments_bytes.decode("utf-8-sig"), newline=""))
    require(reader.fieldnames and len(reader.fieldnames) == len(set(reader.fieldnames)),
            "Missing or duplicate assignment columns")
    rows = list(reader)
    require(len(rows) == 240, "Assignments must contain exactly 240 rows")
    testers = [{"id": f"T{number:02d}", "tasks": []} for number in range(1, 21)]
    assignment_ids = set()
    for index, row in enumerate(rows):
        tester = testers[index // 12]
        order = index % 12 + 1
        block = (order - 1) // 4 + 1
        require(None not in row and all(value is not None for value in row.values()), "Malformed assignment row")
        require(row["tester"] == tester["id"] and row["order"] == str(order),
                "Assignment order must remain T01..T20 with orders 1..12")
        require(row["block"] == str(block) and row["time_limit_min"] == "10", "Assignment block or time limit mismatch")
        task = tasks.get(row["hosted_task_id"])
        require(task is not None, "Assignment hosted task ID is absent from catalog")
        require(all(prior["task_id"] != task["id"] for prior in tester["tasks"]),
                "A tester cannot be assigned the same hosted task twice")
        for column, catalog_field in (("template", "originalTemplateId"), ("instance", "instance"),
                ("variant", "variant"), ("instruction", "goal"), ("site", "siteName"),
                ("reference_answer", "referenceAnswer"), ("scoring", "scoring"), ("caseId", "originalCaseId")):
            require(row[column] == task[catalog_field], f"Assignment {column} differs from catalog")
        template = templates[task["originalTemplateId"]]
        condition = "N" if task["variant"] == "normal" else "F"
        task_code = f"{template['site_code']}-{template['fe_code']}{task['instance']}-{condition}"
        require(row["task_code"] == task_code and row["fe_code"] == template["fe_code"],
                "Assignment private task code differs from specification")
        identity = f"{spec['version']}:{tester['id']}:{order}:{task['id']}"
        assignment_id = "assignment-" + digest(identity.encode("utf-8"))[:24]
        require(assignment_id not in assignment_ids, "Duplicate assignment identity")
        assignment_ids.add(assignment_id)
        tester["tasks"].append({
            "id": assignment_id, "task_id": task["id"], "order": order, "block": block,
            "site_name": task["siteName"], "instruction": task["goal"],
            "start_url": PUBLIC_ORIGIN + urlsplit(task["startUrl"]).path,
            # State tasks ("…이 화면에 표시됨 (진행자 확인)") have no spoken answer; the flag leaks no answer text.
            "requires_answer": "진행자 확인" not in task["scoring"],
            "time_limit_seconds": 600, "preparation": task["operatorPreparation"],
        })
    result = {"schema_version": 1, "title": "웹 과업 평가", "source": {
        "spec_version": spec["version"], "spec_sha256": digest(spec_bytes),
        "assignments_sha256": digest(assignments_bytes)}, "testers": testers}
    result["id"] = "study-" + digest(encode(result))[:24]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assignments", type=Path, default=DEFAULT_ASSIGNMENTS)
    parser.add_argument("--operator-catalog", type=Path, default=DEFAULT_OPERATOR_CATALOG)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        result = build_catalog(args.assignments, args.operator_catalog, args.spec)
    except (ValueError, KeyError, TypeError, OSError, csv.Error) as error:
        parser.error(f"Catalog validation failed: {error}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(encode(result))
    print(f"Wrote {len(result['testers'])} testers / 240 assignments to {args.output}")


if __name__ == "__main__":
    main()
