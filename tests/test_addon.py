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
        self.plugin._promptDepth = 0

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

    def speech_globals(self, focus, navigator):
        self.plugin._onSpeechQueued.__globals__.update({
            "api": SimpleNamespace(getFocusObject=lambda: focus,
                                   getNavigatorObject=lambda: navigator),
            "time": time, "SPEECH_MERGE_SECONDS": 1.2,
        })
        self.plugin._shouldRecord = lambda obj: True
        self.plugin._objectContext = lambda obj: {}
        self.plugin._foregroundTitle = lambda: "Example"
        self.plugin._activeSpeech = None

    def test_edit_field_speech_is_recorded_verbatim(self):
        """0.3.3: speech in editable/protected fields is stored as spoken (no redaction)."""
        self.speech_globals(object(), object())
        self.plugin._onSpeechQueued(["예약번호 편집창", "FAKE-PNR-1234"])
        self.plugin._finalizeSpeech("2026-10-07T00:00:01.000Z", False, "synth_done")
        event = self.plugin._events.get_nowait()
        self.assertFalse(event["payload"]["redacted"])
        self.assertEqual("예약번호 편집창 FAKE-PNR-1234", event["payload"]["raw_text"])
        self.assertNotIn("숨김", json.dumps(event, ensure_ascii=False))

    def test_redaction_paths_are_gone_from_addon(self):
        source = (Path(__file__).resolve().parents[1]
                  / "nvda-addon/globalPlugins/a11yTaskRecorder.py").read_text(encoding="utf-8")
        for removed in ("[입력 문자", "_lastTypedCharacterAt", "_isEditable", "_isProtected"):
            self.assertNotIn(removed, source)


class GestureClassificationTests(unittest.TestCase):
    """Pilot 2026-10-08: Korean NVDA, unbound Tab and combobox arrows were dropped.

    0.3.3: typed characters and other unbound keys are recorded too.
    """

    def setUp(self):
        cls = collector_class()
        self.plugin = cls.__new__(cls)
        self.plugin._events = queue.Queue()
        self.plugin._sessionLock = threading.Lock()
        self.plugin._speechLock = threading.RLock()
        self.plugin._activeSession = "session-a"
        self.plugin._activeSpeech = None
        self.plugin._promptDepth = 0
        self.plugin._shouldRecord = lambda obj: True
        self.plugin._objectContext = lambda obj: {"name": "예약번호", "role": "편집창"}
        self.plugin._foregroundTitle = lambda: "Example"
        self.plugin._onGesture.__globals__.update({
            "api": SimpleNamespace(getFocusObject=lambda: object(),
                                   getNavigatorObject=lambda: None),
            "time": time, "SPEECH_MERGE_SECONDS": 1.2,
            "NAMED_UNBOUND_KEYS": {"kb:tab", "kb:shift+tab", "kb:enter", "kb:downarrow"},
        })

    def gesture(self, display, key, script=None, **extra):
        return SimpleNamespace(isModifier=False, displayName=display, script=script,
                               identifiers=(f"kb(desktop):{key}", f"kb:{key}"), **extra)

    def recorded(self):
        events = []
        while not self.plugin._events.empty():
            events.append(self.plugin._events.get_nowait())
        return [e for e in events if e["type"] == "input"]

    def test_korean_tab_without_script_is_recorded_as_key(self):
        self.plugin._onGesture(self.gesture("탭", "tab"))
        events = self.recorded()
        self.assertEqual(["탭"], [e["payload"]["gesture"] for e in events])
        self.assertEqual("key", events[0]["payload"]["input_kind"])

    def test_layout_specific_arrow_without_script_is_recorded(self):
        self.plugin._onGesture(self.gesture("아래쪽 화살표", "downarrow"))
        self.assertEqual(1, len(self.recorded()))

    def test_typed_character_is_recorded_with_actual_key(self):
        self.plugin._onGesture(self.gesture("가", "a"))
        events = self.recorded()
        self.assertEqual(1, len(events))
        payload = events[0]["payload"]
        self.assertEqual("가", payload["gesture"])
        self.assertEqual("가", payload["display_name"])
        self.assertIn("kb:a", payload["identifiers"])
        self.assertEqual("character", payload["input_kind"])
        self.assertEqual("예약번호", events[0]["element"]["name"])

    def test_shifted_character_uses_nvda_is_character(self):
        self.plugin._onGesture(self.gesture("shift+a", "shift+a", isCharacter=True))
        events = self.recorded()
        self.assertEqual(["shift+a"], [e["payload"]["gesture"] for e in events])
        self.assertEqual("character", events[0]["payload"]["input_kind"])

    def test_unlisted_unbound_key_is_recorded_as_key(self):
        self.plugin._onGesture(self.gesture("F5", "f5"))
        events = self.recorded()
        self.assertEqual(["F5"], [e["payload"]["gesture"] for e in events])
        self.assertEqual("key", events[0]["payload"]["input_kind"])

    def test_bound_script_is_recorded_as_command(self):
        def script_nextHeading(gesture):
            """Moves to the next heading"""
        self.plugin._onGesture(self.gesture("h", "h", script=script_nextHeading))
        payload = self.recorded()[0]["payload"]
        self.assertEqual("command", payload["input_kind"])
        self.assertEqual("script_nextHeading", payload["nvda_script"])

    def test_scope_and_modifiers_still_limit_recording(self):
        self.plugin._onGesture(SimpleNamespace(isModifier=True, displayName="shift",
                                               script=None, identifiers=("kb:shift",)))
        self.plugin._shouldRecord = lambda obj: False
        self.plugin._onGesture(self.gesture("가", "a"))
        self.assertEqual([], self.recorded())

    def test_character_echo_right_after_typing_is_recorded(self):
        self.plugin._onGesture(self.gesture("1", "1"))
        self.plugin._onSpeechQueued(["1"])
        self.plugin._finalizeSpeech("2026-10-07T00:00:01.000Z", False, "synth_done")
        speech = [self.plugin._events.get_nowait() for _ in range(self.plugin._events.qsize())]
        episode = [e for e in speech if e["type"] == "speech_episode"][0]
        self.assertEqual("1", episode["payload"]["raw_text"])
        self.assertFalse(episode["payload"]["redacted"])


class SingleWindowAddonTests(unittest.TestCase):
    """0.4.0: one window, frame tagging, recorder prompts and start/end/repeat signals."""

    def setUp(self):
        cls = collector_class()
        self.plugin = cls.__new__(cls)
        self.plugin._events = queue.Queue()
        self.plugin._sessionLock = threading.Lock()
        self.plugin._speechLock = threading.RLock()
        self.plugin._activeSession = "session-a"
        self.plugin._activeSpeech = None
        self.plugin._promptDepth = 0
        self.plugin._shouldRecord = lambda obj: True
        self.plugin._objectContext = lambda obj: {}
        self.plugin._foregroundTitle = lambda: "A11y Task Recorder - Chrome"
        self.plugin._onSpeechQueued.__globals__.update({
            "api": SimpleNamespace(getFocusObject=lambda: object(), getNavigatorObject=lambda: None),
            "time": time, "SPEECH_MERGE_SECONDS": 1.2,
        })

    def test_window_title_no_longer_excludes_recording(self):
        source = (Path(__file__).resolve().parents[1]
                  / "nvda-addon/globalPlugins/a11yTaskRecorder.py").read_text(encoding="utf-8")
        self.assertNotIn('"a11y task recorder" in title', source)
        for gesture in ("kb:NVDA+control+e", "kb:NVDA+control+h",
                        "kb:NVDA+control+shift+s", "kb:NVDA+control+shift+e", "kb:NVDA+control+shift+t"):
            self.assertIn(gesture, source)

    def test_frame_is_task_inside_internal_frame_and_recorder_outside(self):
        roles = SimpleNamespace(INTERNALFRAME="frame", DOCUMENT="document")
        self.plugin._objectFrame.__globals__.update({"controlTypes": SimpleNamespace(Role=roles)})
        top = SimpleNamespace(role="document", parent=None)
        frame = SimpleNamespace(role="frame", parent=top)
        inner_doc = SimpleNamespace(role="document", parent=frame)
        link = SimpleNamespace(role="link", parent=inner_doc)
        heading = SimpleNamespace(role="heading", parent=top)
        self.assertEqual("task", self.plugin._objectFrame(link))
        self.assertEqual("recorder", self.plugin._objectFrame(heading))

    def test_input_in_browse_mode_uses_the_virtual_cursor_object(self):
        # Windows 10/28 (F4): the focus object was "unknown, unavailable" for every key in the frame.
        self.plugin.__dict__.pop("_objectContext", None)
        roles = SimpleNamespace(INTERNALFRAME="frame", DOCUMENT="document")
        top = SimpleNamespace(role="document", parent=None, name="A11y Task Recorder", states=[])
        frame = SimpleNamespace(role="frame", parent=top, name="과업 사이트", states=[])
        inner_doc = SimpleNamespace(role="document", parent=frame, name="한국에너지재단", states=[])
        link = SimpleNamespace(role="link", parent=inner_doc, name="메뉴 바로가기", states=[])
        info = SimpleNamespace(NVDAObjectAtStart=link)
        interceptor = SimpleNamespace(passThrough=False, rootNVDAObject=inner_doc, makeTextInfo=lambda position: info)
        broken = SimpleNamespace(role="unknown", parent=None, name="", states=["unavailable"], treeInterceptor=interceptor)
        self.plugin._inputContext.__globals__.update({
            "controlTypes": SimpleNamespace(Role=roles), "textInfos": SimpleNamespace(POSITION_CARET="caret"),
            "api": SimpleNamespace(getCaretObject=lambda: interceptor, getNavigatorObject=lambda: link)})
        context = self.plugin._inputContext(broken)
        self.assertEqual("browse_cursor", context["context_source"])
        self.assertEqual("web_content", context["scope"])
        self.assertEqual("task", context["frame"])
        self.assertEqual("메뉴 바로가기", context["name"])
        self.assertEqual("unknown", context["focus_object"]["role"])
        # Focus mode (passThrough) keeps the focus object, as before.
        field = SimpleNamespace(role="editabletext", parent=inner_doc, name="검색어", states=[],
                                treeInterceptor=SimpleNamespace(passThrough=True, rootNVDAObject=inner_doc, makeTextInfo=lambda p: info))
        context = self.plugin._inputContext(field)
        self.assertEqual("focus", context["context_source"])
        self.assertEqual("검색어", context["name"])
        self.assertNotIn("focus_object", context)

    def test_prompt_speech_is_tagged_and_not_merged_with_user_speech(self):
        self.plugin._onSpeechQueued(["버튼 재료 보기"])
        spoken = []
        self.plugin._speakPrompt.__globals__.update({"ui": SimpleNamespace(message=lambda text: (
            spoken.append(text), self.plugin._onSpeechQueued([text])))})
        self.plugin._speakPrompt("과업이 끝났습니다. 결과를 입력해 주세요.")
        self.assertEqual(0, self.plugin._promptDepth)
        self.plugin._finalizeSpeech("2026-10-07T00:00:02.000Z", False, "synth_done")
        episodes = [self.plugin._events.get_nowait() for _ in range(self.plugin._events.qsize())]
        episodes = [e for e in episodes if e["type"] == "speech_episode"]
        self.assertEqual(["nvda", "recorder_prompt"], [e["payload"]["origin"] for e in episodes])
        self.assertEqual("과업이 끝났습니다. 결과를 입력해 주세요.", episodes[1]["payload"]["raw_text"])
        self.assertEqual(spoken, ["과업이 끝났습니다. 결과를 입력해 주세요."])

    def test_signal_reply_beeps_and_speaks_only_accepted_text(self):
        calls = []
        self.plugin._queueMain = lambda function, *args: calls.append((getattr(function, "__name__", "fn"), args))
        self.plugin._afterSignal.__globals__.update({"tones": SimpleNamespace(beep=lambda *a: None)})
        self.plugin._afterSignal("start", {"accepted": False, "reason": "no_prepared_task"})
        self.plugin._afterSignal("repeat", {"accepted": True, "speak": "지시문입니다"})
        self.plugin._afterSignal("end", {"accepted": True, "speak": ""})
        self.assertEqual([("<lambda>", (220, 160)), ("<lambda>", (880, 90)), ("_speakPrompt", ("지시문입니다",)),
                          ("<lambda>", (660, 90))], calls)
