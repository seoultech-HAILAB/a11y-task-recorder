"""Isolated collector logic tests, not Windows or NVDA runtime validation."""
import ast
import json
import queue
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


def collector_class():
    source = Path(__file__).resolve().parents[1] / "nvda-addon/globalPlugins/a11yTaskRecorder.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    cls.bases = []
    for node in cls.body:
        if isinstance(node, ast.FunctionDef):
            node.decorator_list = []
    import urllib.request
    namespace = {"queue": queue, "json": json, "urllib": urllib,
                 "utcNow": lambda: "2026-10-07T00:00:00.000Z",
                 "COLLECTOR_URL": "http://127.0.0.1:8765",
                 "REQUEST_TIMEOUT_SECONDS": 0.8, "log": MagicMock(),
                 "safeString": lambda value, limit=1000: str(value)[:limit],
                 "normalizeSpeechText": lambda value: value}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), "exec"), namespace)
    return namespace["GlobalPlugin"]


class AddonBoundaryTests(unittest.TestCase):
    def setUp(self):
        cls = collector_class()
        self.plugin = cls.__new__(cls)
        self.plugin._events = queue.Queue()
        self.plugin._sessionLock = threading.Lock()
        self.plugin._speechLock = threading.RLock()
        self.plugin._activeSession = "session-a"

    def test_queue_binds_session_at_capture(self):
        self.plugin._enqueue({"type": "input"})
        self.plugin._activeSession = "session-b"
        self.assertEqual("session-a", self.plugin._events.get_nowait()["session_id"])

    def test_idle_capture_is_not_queued(self):
        self.plugin._activeSession = None
        self.plugin._enqueue({"type": "input"})
        self.assertTrue(self.plugin._events.empty())

    def test_speech_keeps_original_session_when_finalized_later(self):
        self.plugin._activeSpeech = {
            "session_id": "session-a", "timestamp": "2026-10-07T00:00:00.000Z",
            "fragments": [{"text": "Example link"}], "priority": "normal",
            "redacted": False, "element": {}, "page_title": "Example",
        }
        self.plugin._activeSession = "session-b"
        self.plugin._finalizeSpeech("2026-10-07T00:00:01.000Z", False, "synth_done")
        self.assertEqual("session-a", self.plugin._events.get_nowait()["session_id"])

    def test_browse_mode_editable_speech_is_redacted(self):
        focus = object()
        navigator = object()
        self.plugin._onSpeechQueued.__globals__.update({
            "api": SimpleNamespace(getFocusObject=lambda: focus,
                                   getNavigatorObject=lambda: navigator),
            "time": time, "SPEECH_MERGE_SECONDS": 1.2,
        })
        self.plugin._shouldRecord = lambda obj: True
        self.plugin._isEditable = lambda obj: obj is navigator
        self.plugin._isProtected = lambda obj: False
        self.plugin._objectContext = lambda obj: {}
        self.plugin._foregroundTitle = lambda: "Example"
        self.plugin._activeSpeech = None
        self.plugin._lastTypedCharacterAt = 0
        self.plugin._onSpeechQueued(["PRIVATE-FORM-CONTENT"])
        self.plugin._finalizeSpeech("2026-10-07T00:00:01.000Z", False, "synth_done")
        event = self.plugin._events.get_nowait()
        self.assertTrue(event["payload"]["redacted"])
        self.assertNotIn("PRIVATE-FORM-CONTENT", json.dumps(event))


class GestureClassificationTests(unittest.TestCase):
    """Pilot 2026-10-08: Korean NVDA, unbound Tab and combobox arrows were dropped."""

    def setUp(self):
        cls = collector_class()
        self.plugin = cls.__new__(cls)
        self.plugin._events = queue.Queue()
        self.plugin._sessionLock = threading.Lock()
        self.plugin._speechLock = threading.RLock()
        self.plugin._activeSession = "session-a"
        self.plugin._activeSpeech = None
        self.plugin._lastTypedCharacterAt = 0.0
        self.plugin._shouldRecord = lambda obj: True
        self.plugin._objectContext = lambda obj: {}
        self.plugin._foregroundTitle = lambda: "Example"
        self.plugin._onGesture.__globals__.update({
            "api": SimpleNamespace(getFocusObject=lambda: object()), "time": time,
            "SAFE_UNBOUND_KEYS": {"kb:tab", "kb:shift+tab", "kb:enter", "kb:downarrow"},
        })

    def gesture(self, display, key, script=None):
        return SimpleNamespace(isModifier=False, displayName=display, script=script,
                               identifiers=(f"kb(desktop):{key}", f"kb:{key}"))

    def recorded(self):
        events = []
        while not self.plugin._events.empty():
            events.append(self.plugin._events.get_nowait())
        return [e for e in events if e["type"] == "input"]

    def test_korean_tab_without_script_is_recorded_not_redacted(self):
        self.plugin._onGesture(self.gesture("탭", "tab"))
        self.assertEqual(["탭"], [e["payload"]["gesture"] for e in self.recorded()])
        self.assertEqual(0.0, self.plugin._lastTypedCharacterAt)

    def test_layout_specific_arrow_without_script_is_recorded(self):
        self.plugin._onGesture(self.gesture("아래쪽 화살표", "downarrow"))
        self.assertEqual(1, len(self.recorded()))

    def test_typed_character_is_still_dropped_and_marks_echo(self):
        self.plugin._onGesture(self.gesture("가", "a"))
        self.assertEqual([], self.recorded())
        self.assertGreater(self.plugin._lastTypedCharacterAt, 0)

    def test_unlisted_unbound_key_is_ignored(self):
        self.plugin._onGesture(self.gesture("F5", "f5"))
        self.assertEqual([], self.recorded())
