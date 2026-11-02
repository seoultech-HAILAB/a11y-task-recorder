"""Data-attribution regressions; these are isolated tests, not an NVDA trial."""
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from tests.test_addon import collector_class


class AddonResearchContextTests(unittest.TestCase):
    def setUp(self):
        cls = collector_class()
        self.plugin = cls.__new__(cls)
        self.plugin._events = queue.Queue()
        self.plugin._sessionLock = threading.Lock()
        self.plugin._speechLock = threading.RLock()
        self.plugin._activeSession = "task-a"
        self.plugin._activeSpeech = None
        self.plugin._promptDepth = 0
        self.roles = SimpleNamespace(DOCUMENT="document", INTERNALFRAME="frame", UNKNOWN="unknown")
        self.plugin._objectFrame.__globals__.update({
            "controlTypes": SimpleNamespace(Role=self.roles),
            "COLLECTOR_URL": "http://recorder.fixture.test:8765",
        })

    def document(self, value="https://site.fixture.test/current", **extra):
        return SimpleNamespace(role="document", parent=None, value=value, **extra)

    def test_non_url_buffer_identifier_does_not_hide_accessible_document_url(self):
        doc = self.document(IAccessibleObject=SimpleNamespace(accValue=lambda _: "https://site.fixture.test/current"), IAccessibleChildID=0)
        obj = SimpleNamespace(treeInterceptor=SimpleNamespace(documentConstantIdentifier="not-a-url"))
        self.assertEqual("https://site.fixture.test/current", self.plugin._documentUrl(doc, obj))

    def test_empty_accessible_value_falls_through_to_document_value(self):
        doc = self.document(IAccessibleObject=SimpleNamespace(accValue=lambda _: ""), IAccessibleChildID=0)
        self.assertEqual("https://site.fixture.test/current", self.plugin._documentUrl(doc, object()))

    def test_valid_buffer_url_keeps_precedence(self):
        obj = SimpleNamespace(treeInterceptor=SimpleNamespace(documentConstantIdentifier="https://site.fixture.test/next"))
        self.assertEqual("https://site.fixture.test/next", self.plugin._documentUrl(self.document(), obj))

    def test_standalone_document_carries_url_and_unverified_ownership(self):
        obj = SimpleNamespace(role="link", parent=self.document(), name="Next", states=[])
        context = self.plugin._objectContext(obj)
        self.assertEqual("task", context["frame"])
        self.assertEqual("url_only_unverified", context["frame_attribution"])
        self.assertEqual("https://site.fixture.test/current", context["document_url"])

    def test_recorder_origin_is_compared_exactly_not_by_string_prefix(self):
        self.plugin._objectFrame.__globals__["COLLECTOR_URL"] = "http://recorder.fixture.test:876"
        own = self.document("http://recorder.fixture.test:876/?tester=fixture")
        other = self.document("http://recorder.fixture.test:8765/")
        self.assertEqual("recorder", self.plugin._objectFrame(own))
        self.assertEqual("task", self.plugin._objectFrame(other))

    def test_address_bar_does_not_borrow_stale_site_caret_or_navigator(self):
        link = SimpleNamespace(role="link", parent=self.document(), name="Old page link", states=[])
        buffer = SimpleNamespace(passThrough=False, rootNVDAObject=link.parent,
                                 makeTextInfo=lambda _: SimpleNamespace(NVDAObjectAtStart=link))
        address_bar = SimpleNamespace(role="combobox", parent=None, name="Address bar", states=[])
        self.plugin._inputContext.__globals__.update({
            "api": SimpleNamespace(getCaretObject=lambda: buffer, getNavigatorObject=lambda: link),
            "textInfos": SimpleNamespace(POSITION_CARET="caret"),
        })
        context = self.plugin._inputContext(address_bar)
        self.assertEqual("focus", context["context_source"])
        self.assertEqual("browser_ui", context["scope"])
        self.assertEqual("Address bar", context["name"])
        self.assertNotIn("document_url", context)
        address_bar.treeInterceptor = buffer
        self.assertEqual("browser_ui", self.plugin._inputContext(address_bar)["scope"])

    def test_unknown_focus_can_still_use_site_caret(self):
        link = SimpleNamespace(role="link", parent=self.document(), name="Current page link", states=[])
        buffer = SimpleNamespace(passThrough=False, rootNVDAObject=link.parent,
                                 makeTextInfo=lambda _: SimpleNamespace(NVDAObjectAtStart=link))
        unknown = SimpleNamespace(role="unknown", parent=None, name="", states=[])
        self.plugin._inputContext.__globals__.update({
            "api": SimpleNamespace(getCaretObject=lambda: buffer, getNavigatorObject=lambda: link),
            "textInfos": SimpleNamespace(POSITION_CARET="caret"),
        })
        context = self.plugin._inputContext(unknown)
        self.assertEqual("browse_cursor", context["context_source"])
        self.assertEqual("Current page link", context["name"])

    def test_cancel_after_focus_leaves_browser_ends_original_speech(self):
        self.plugin._activeSpeech = {
            "session_id": "task-a", "timestamp": "2026-11-02T00:00:00.000Z",
            "fragments": [{"text": "Current page link"}], "priority": "normal",
            "element": {}, "page_title": "Fixture",
        }
        self.plugin._onSpeechCanceled.__globals__["api"] = SimpleNamespace(getFocusObject=lambda: object())
        self.plugin._shouldRecord = lambda _: False
        self.plugin._onSpeechCanceled()
        self.assertIsNone(self.plugin._activeSpeech)
        event = self.plugin._events.get_nowait()
        self.assertEqual("speech_episode", event["type"])
        self.assertEqual("task-a", event["session_id"])
        self.assertEqual("speech_canceled", event["payload"]["end_reason"])
        self.assertTrue(event["interrupted"])
        self.assertTrue(self.plugin._events.empty())

    def test_delayed_input_keeps_old_session_without_new_session_context(self):
        focus = Mock()
        self.plugin._completeInput.__globals__["api"] = SimpleNamespace(getFocusObject=focus)
        self.plugin._activeSession = "task-b"
        event = {"type": "input", "session_id": "task-a", "timestamp": "2026-11-02T00:00:00.000Z",
                 "payload": {"gesture": "Enter"}}
        self.plugin._completeInput(event)
        focus.assert_not_called()
        saved = self.plugin._events.get_nowait()
        self.assertEqual("task-a", saved["session_id"])
        self.assertEqual(event["timestamp"], saved["timestamp"])
        self.assertEqual("Enter", saved["payload"]["gesture"])
        self.assertEqual("session_changed_before_context", saved["payload"]["context_unavailable_reason"])
        self.assertNotIn("element", saved)

    def test_session_change_during_input_context_read_discards_ambiguous_element(self):
        self.plugin._completeInput.__globals__["api"] = SimpleNamespace(getFocusObject=lambda: object())
        self.plugin._browseState = lambda _: "browse"
        self.plugin._foregroundTitle = lambda: "Next task"
        def changed(_):
            self.plugin._activeSession = "task-b"
            return {"name": "Next task element"}
        self.plugin._inputContext = changed
        self.plugin._completeInput({"type": "input", "session_id": "task-a", "payload": {"gesture": "Enter"}})
        saved = self.plugin._events.get_nowait()
        self.assertEqual("task-a", saved["session_id"])
        self.assertNotIn("element", saved)
        self.assertNotIn("page_title", saved)
        self.assertNotIn("browse_state", saved["payload"])
        self.assertEqual("session_changed_during_context", saved["payload"]["context_unavailable_reason"])

    def test_unassigned_queued_input_is_not_rebound_to_current_session(self):
        self.plugin._activeSession = "task-b"
        self.plugin._completeInput({"type": "input", "session_id": None, "payload": {"gesture": "Enter"}})
        self.assertTrue(self.plugin._events.empty())

    def test_same_control_identity_on_new_document_does_not_merge_speech(self):
        focus = object()
        self.plugin._onSpeechQueued.__globals__.update({
            "api": SimpleNamespace(getFocusObject=lambda: focus, getNavigatorObject=lambda: focus),
            "time": SimpleNamespace(monotonic=lambda: 1.0),
            "SPEECH_MERGE_SECONDS": 1.2,
        })
        self.plugin._shouldRecord = lambda _: True
        self.plugin._foregroundTitle = lambda: "Fixture page"
        context = {"role": "button", "name": "Search", "unique_id": "fixture-control",
                   "document_url": "https://site.fixture.test/first"}
        self.plugin._objectContext = lambda _: dict(context)
        self.plugin._onSpeechQueued(["Search button"])
        context["document_url"] = "https://site.fixture.test/second"
        self.plugin._onSpeechQueued(["Search button"])
        self.plugin._finalizeSpeech("2026-11-02T00:00:01.000Z", False, "synth_done")
        episodes = [self.plugin._events.get_nowait() for _ in range(self.plugin._events.qsize())]
        self.assertEqual(2, len(episodes))
        self.assertEqual(["https://site.fixture.test/first", "https://site.fixture.test/second"],
                         [event["element"]["document_url"] for event in episodes])
        self.assertEqual([1, 1], [event["payload"]["fragment_count"] for event in episodes])


if __name__ == "__main__":
    unittest.main()
