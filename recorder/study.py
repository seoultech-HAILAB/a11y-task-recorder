"""Participant-safe catalog fields and result codes for assigned collection."""

import json
from pathlib import Path
from urllib.parse import urlsplit


ADVANCING_OUTCOMES = frozenset({"completed", "blocked", "timeout"})
STUDY_OUTCOMES = ADVANCING_OUTCOMES | {"technical"}
TASK_FIELDS = (
    "id", "task_id", "order", "block", "site_name", "instruction",
    "start_url", "time_limit_seconds", "preparation",
)
SOURCE_FIELDS = ("spec_version", "spec_sha256", "assignments_sha256")
# Optional participant-safe flags with their defaults (older catalogs omit them).
OPTIONAL_TASK_FIELDS = {"requires_answer": True}


def validate_setup(receipt: dict) -> dict:
    allowed = {"method", "actions", "quantity", "product_id", "prepared_at", "checked_at",
               "excluded_from_participant_recording"}
    if not isinstance(receipt, dict) or set(receipt) - allowed:
        raise ValueError("Invalid setup receipt fields")
    if receipt.get("method") not in ("operator_automation", "operator_state_check", "not_required"):
        raise ValueError("Invalid setup method")
    if receipt.get("excluded_from_participant_recording") is not True:
        raise ValueError("Setup must be excluded from participant recording")
    actions = receipt.get("actions", [])
    if not isinstance(actions, list) or len(actions) > 40 or not all(
        isinstance(item, str) and len(item) <= 256 for item in actions
    ):
        raise ValueError("Invalid setup actions")
    if "quantity" in receipt and (type(receipt["quantity"]) is not int or not 0 <= receipt["quantity"] <= 999):
        raise ValueError("Invalid setup quantity")
    for key in ("product_id", "prepared_at", "checked_at"):
        if key in receipt and (not isinstance(receipt[key], str) or len(receipt[key]) > 128):
            raise ValueError("Invalid setup value")
    if len(json.dumps(receipt).encode("utf-8")) > 8192:
        raise ValueError("Setup receipt too large")
    return receipt


def load_catalog(path: Path) -> dict:
    """Allowlist participant fields before storing or exposing a catalog."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw["schema_version"] != 1 or not raw["id"] or not raw["testers"]:
        raise ValueError("Invalid study catalog")
    catalog = {
        "schema_version": 1, "id": raw["id"], "title": raw["title"],
        "source": {key: raw["source"][key] for key in SOURCE_FIELDS},
        "testers": [],
    }
    # Optional: where results go when tasks run on a site we do not host (e.g. a partner's test copies).
    if raw.get("upload_url"):
        upload_url = raw["upload_url"]
        parts = urlsplit(upload_url) if isinstance(upload_url, str) else None
        if not parts or parts.scheme not in {"https", "http"} or not parts.netloc:
            raise ValueError("Invalid upload URL")
        catalog["upload_url"] = upload_url
    tester_ids, assignment_ids = set(), set()
    for tester in raw["testers"]:
        if tester["id"] in tester_ids:
            raise ValueError("Duplicate tester")
        tester_ids.add(tester["id"])
        tasks = []
        for order, task in enumerate(tester["tasks"], 1):
            if task["order"] != order or task["id"] in assignment_ids:
                raise ValueError("Invalid assignment order or identity")
            if not isinstance(task["preparation"], list) or not all(
                isinstance(item, str) for item in task["preparation"]
            ):
                raise ValueError("Invalid preparation")
            assignment_ids.add(task["id"])
            entry = {key: task[key] for key in TASK_FIELDS}
            for key, default in OPTIONAL_TASK_FIELDS.items():
                entry[key] = bool(task.get(key, default))
            tasks.append(entry)
        if not tasks:
            raise ValueError("Empty assignments")
        catalog["testers"].append({"id": tester["id"], "tasks": tasks})
    return catalog
