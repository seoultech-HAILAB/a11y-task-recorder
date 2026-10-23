# -*- coding: UTF-8 -*-
"""NVDA-side event collector for A11y Task Recorder."""

import json
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import addonHandler
import api
import buildVersion
import controlTypes
import globalPluginHandler
import inputCore
import queueHandler
import synthDriverHandler
import tones
import ui
from logHandler import log
from scriptHandler import script
from speech import extensions as speechExtensions


COLLECTOR_URL = "http://127.0.0.1:8765"
POLL_SECONDS = 1.25
REQUEST_TIMEOUT_SECONDS = 0.8
SUPPORTED_BROWSER_APPS = {"chrome", "msedge", "firefox", "brave", "opera"}
SPEECH_MERGE_SECONDS = 1.2
SIGNAL_SOURCE = "nvda_addon"
ADDON_VERSION = addonHandler.getCodeAddon().manifest["version"]
# Named keys stay "key" even when NVDA shows them as one localized character ("탭").
NAMED_UNBOUND_KEYS = {
    "kb:tab",
    "kb:shift+tab",
    "kb:enter",
    "kb:space",
    "kb:escape",
    "kb:uparrow",
    "kb:downarrow",
    "kb:leftarrow",
    "kb:rightarrow",
    "kb:home",
    "kb:end",
    "kb:pageup",
    "kb:pagedown",
    "kb:backspace",
    "kb:delete",
}


def utcNow():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def safeString(value, limit=1000):
    if value is None:
        return ""
    try:
        return str(value)[:limit]
    except Exception:
        return ""


def normalizeSpeechText(value):
    """Conservatively remove common role/state narration while preserving raw text."""
    text = re.sub(r"\s+", " ", safeString(value, 5000)).strip()
    if not text:
        return text
    patterns = (
        r"(?:,\s*)?(?:clickable|visited|unvisited|selected|collapsed|expanded)$",
        r"(?:,\s*)?(?:heading)(?:\s+level\s+\d+)?$",
        r"(?:,\s*)?(?:link|button|checkbox|radio button|edit|combo box)$",
        r"(?:,\s*)?(?:클릭 가능|방문함|선택됨|축소됨|확장됨)$",
        r"(?:,\s*)?(?:제목|헤딩)(?:\s*(?:수준|레벨)\s*\d+)?$",
        r"(?:,\s*)?(?:링크|버튼|체크박스|라디오 버튼|편집창|콤보상자)$",
    )
    changed = True
    while changed:
        changed = False
        for pattern in patterns:
            cleaned = re.sub(pattern, "", text, flags=re.IGNORECASE).strip(" ,")
            if cleaned != text and cleaned:
                text = cleaned
                changed = True
    return text


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    scriptCategory = "A11y Task Recorder"

    def __init__(self):
        super().__init__()
        self._events = queue.Queue(maxsize=4000)
        self._stopEvent = threading.Event()
        self._activeSession = None
        self._activeTitle = ""
        self._environmentReportedSession = None
        self._sessionLock = threading.Lock()
        self._speechLock = threading.RLock()
        self._activeSpeech = None
        # >0 while this add-on itself is speaking a recorder prompt (ui.message).
        self._promptDepth = 0
        self._speechHook = getattr(
            speechExtensions,
            "pre_speechQueued",
            speechExtensions.pre_speech,
        )
        self._worker = threading.Thread(
            target=self._workerLoop,
            name="a11yTaskRecorderSender",
            daemon=True,
        )
        inputCore.decide_executeGesture.register(self._onGesture)
        self._speechHook.register(self._onSpeechQueued)
        speechExtensions.speechCanceled.register(self._onSpeechCanceled)
        synthDriverHandler.synthDoneSpeaking.register(self._onSynthDoneSpeaking)
        self._worker.start()
        log.info("A11y Task Recorder add-on started")

    def terminate(self):
        try:
            inputCore.decide_executeGesture.unregister(self._onGesture)
            self._speechHook.unregister(self._onSpeechQueued)
            speechExtensions.speechCanceled.unregister(self._onSpeechCanceled)
            synthDriverHandler.synthDoneSpeaking.unregister(self._onSynthDoneSpeaking)
        except Exception:
            log.debugWarning("Could not unregister an A11y Task Recorder extension point", exc_info=True)
        self._stopEvent.set()
        try:
            self._worker.join(timeout=1.2)
        except Exception:
            pass
        super().terminate()

    def event_gainFocus(self, obj, nextHandler):
        try:
            if self._shouldRecord(obj):
                self._enqueue(
                    {
                        "type": "focus",
                        "payload": {"kind": "system_focus"},
                        "element": self._objectContext(obj),
                        "page_title": self._foregroundTitle(),
                    }
                )
        except Exception:
            log.debugWarning("A11y Task Recorder focus capture failed", exc_info=True)
        nextHandler()

    def _layoutFreeGesture(self, identifiers):
        """Return the "kb:<keys>" form; NVDA lists "kb(desktop):<keys>" first."""
        for identifier in identifiers:
            value = identifier.lower()
            if value.startswith("kb(") and "):" in value:
                value = "kb:" + value.split("):", 1)[1]
            if value.startswith("kb:"):
                return value
        return identifiers[0].lower() if identifiers else ""

    def _inputKind(self, gesture, stableGesture, displayName, scriptObject):
        """Classify an input as "command", "key" or "character" without dropping it."""
        if scriptObject is not None:
            return "command"
        if stableGesture in NAMED_UNBOUND_KEYS or any(
            modifier in stableGesture
            for modifier in ("control+", "ctrl+", "alt+", "windows+", "win+")
        ):
            return "key"
        try:
            isCharacter = getattr(gesture, "isCharacter", None)
        except Exception:
            isCharacter = None
        if isCharacter is None:
            isCharacter = len(displayName) == 1 and displayName.isprintable()
        return "character" if isCharacter else "key"

    def _onGesture(self, gesture):
        """Record every non-modifier gesture without changing whether NVDA executes it.

        Since 0.3.3 typed characters are stored as-is (study inputs are researcher-supplied
        fake values); scoping to an active session in a browser stays in _shouldRecord.
        """
        try:
            if getattr(gesture, "isModifier", False):
                return True
            focus = api.getFocusObject()
            if not self._shouldRecord(focus):
                return True
            displayName = safeString(getattr(gesture, "displayName", ""), 180)
            scriptObject = getattr(gesture, "script", None)
            identifiers = []
            for identifier in getattr(gesture, "identifiers", [])[:3]:
                value = safeString(identifier, 180)
                if value:
                    identifiers.append(value)
            stableGesture = self._layoutFreeGesture(identifiers)
            inputKind = self._inputKind(gesture, stableGesture, displayName, scriptObject)
            inputTimestamp = utcNow()
            self._finalizeSpeech(
                speechEndTs=inputTimestamp,
                interrupted=True,
                endReason="user_input",
            )
            scriptName = ""
            scriptDescription = ""
            if scriptObject is not None:
                scriptName = safeString(getattr(scriptObject, "__name__", ""), 180)
                scriptDescription = safeString(getattr(scriptObject, "__doc__", ""), 300)
            self._enqueue(
                {
                    "type": "input",
                    "timestamp": inputTimestamp,
                    "payload": {
                        "gesture": displayName,
                        "display_name": displayName,
                        "identifiers": identifiers,
                        "input_kind": inputKind,
                        "nvda_script": scriptName,
                        "script_description": scriptDescription,
                    },
                    "element": self._objectContext(focus),
                    "page_title": self._foregroundTitle(),
                }
            )
        except Exception:
            log.debugWarning("A11y Task Recorder gesture capture failed", exc_info=True)
        return True

    def _onSpeechQueued(self, speechSequence, priority=None, **kwargs):
        """Store queued speech verbatim, including typed-character echo and edit fields."""
        try:
            focus = api.getFocusObject()
            if not self._shouldRecord(focus):
                return
            textParts = [item for item in speechSequence if isinstance(item, str)]
            text = " ".join(part.strip() for part in textParts if part.strip())
            if not text:
                return
            try:
                navigator = api.getNavigatorObject() or focus
            except Exception:
                navigator = focus
            element = self._objectContext(navigator or focus)
            nowMonotonic = time.monotonic()
            nowTimestamp = utcNow()
            fragment = {
                "timestamp": nowTimestamp,
                "text": text[:5000],
                "command_count": max(0, len(speechSequence) - len(textParts)),
            }
            elementKey = self._elementKey(element)
            origin = "recorder_prompt" if self._promptDepth else "nvda"
            with self._speechLock:
                with self._sessionLock:
                    sessionId = self._activeSession
                if not sessionId:
                    return
                active = self._activeSpeech
                canMerge = (
                    active
                    and active["session_id"] == sessionId
                    and active["element_key"] == elementKey
                    and active.get("origin", "nvda") == origin
                    and nowMonotonic - active["last_monotonic"] <= SPEECH_MERGE_SECONDS
                )
                if active and not canMerge:
                    self._finalizeSpeech(
                        speechEndTs=nowTimestamp,
                        interrupted=False,
                        endReason="next_announcement",
                    )
                if not canMerge:
                    self._activeSpeech = {
                        "session_id": sessionId,
                        "timestamp": nowTimestamp,
                        "last_monotonic": nowMonotonic,
                        "element_key": elementKey,
                        "element": element,
                        "page_title": self._foregroundTitle(),
                        "priority": safeString(priority, 80),
                        "origin": origin,
                        "fragments": [fragment],
                    }
                else:
                    self._activeSpeech["last_monotonic"] = nowMonotonic
                    self._activeSpeech["fragments"].append(fragment)
        except Exception:
            log.debugWarning("A11y Task Recorder speech capture failed", exc_info=True)

    def _onSpeechCanceled(self):
        try:
            focus = api.getFocusObject()
            if self._shouldRecord(focus):
                canceledAt = utcNow()
                self._finalizeSpeech(
                    speechEndTs=canceledAt,
                    interrupted=True,
                    endReason="speech_canceled",
                )
                self._enqueue(
                    {
                        "type": "speech_cancel",
                        "timestamp": canceledAt,
                        "payload": {"kind": "nvda_speech_canceled"},
                        "element": self._objectContext(focus),
                        "page_title": self._foregroundTitle(),
                    }
                )
        except Exception:
            log.debugWarning("A11y Task Recorder speech cancel capture failed", exc_info=True)

    def _onSynthDoneSpeaking(self, synth=None, **kwargs):
        try:
            self._finalizeSpeech(
                speechEndTs=utcNow(),
                interrupted=False,
                endReason="synth_done",
            )
        except Exception:
            log.debugWarning(
                "A11y Task Recorder speech completion capture failed",
                exc_info=True,
            )

    def _finalizeSpeech(self, speechEndTs, interrupted, endReason):
        with self._speechLock:
            active = self._activeSpeech
            if not active:
                return
            self._activeSpeech = None
            fragments = active["fragments"]
            rawText = " ".join(
                safeString(fragment.get("text"), 5000)
                for fragment in fragments
                if fragment.get("text")
            ).strip()
            if not rawText:
                return
            normalizedText = normalizeSpeechText(rawText)
            self._enqueue(
                {
                    "type": "speech_episode",
                    "session_id": active["session_id"],
                    "timestamp": active["timestamp"],
                    "speech_end_ts": speechEndTs,
                    "interrupted": bool(interrupted),
                    "payload": {
                        "text": normalizedText or rawText,
                        "raw_text": rawText,
                        "normalized_text": normalizedText,
                        "fragments": fragments[:40],
                        "fragment_count": len(fragments),
                        "priority": active["priority"],
                        # Schema compatibility only: nothing is redacted since 0.3.3.
                        "redacted": False,
                        "end_reason": endReason,
                        "preprocessing": "role-descriptor-v1",
                        # "recorder_prompt": the add-on read a recorder prompt (instruction, result request).
                        "origin": active.get("origin", "nvda"),
                    },
                    "element": active["element"],
                    "page_title": active["page_title"],
                }
            )

    @script(
        description="현재 지점을 접근성 불편 지점으로 표시합니다.",
        gestures=(
            "kb:NVDA+control+i",
            "kb:NVDA+control+shift+m",
        ),
        category=scriptCategory,
    )
    def script_markIssue(self, gesture):
        focus = api.getFocusObject()
        if not self._isSessionActive():
            tones.beep(220, 140)
            return
        self._enqueue(
            {
                "type": "marker",
                "payload": {
                    "label": "NVDA 단축키로 불편 지점 표시",
                    "gesture": safeString(getattr(gesture, "displayName", ""), 180),
                },
                "element": self._objectContext(focus),
                "page_title": self._foregroundTitle(),
            }
        )
        tones.beep(880, 90)

    # 0.4.2: 안내하는 단축키는 끝내기 NVDA+Ctrl+E, 다시 듣기 NVDA+Ctrl+H 두 개다(NVDA 기본 명령에 없는 조합).
    # 시작은 "과업 시작" 버튼으로 안내하고, 0.4.1까지의 NVDA+Ctrl+Shift+S/E/T는 호환 별칭으로 남긴다.
    @script(
        description="배정된 과업을 시작합니다 (단일 창 기록기).",
        gestures=("kb:NVDA+control+shift+s",),
        category=scriptCategory,
    )
    def script_startTask(self, gesture):
        self._postSignal("start")

    @script(
        description="진행 중인 과업을 끝내고 결과 입력으로 넘어갑니다.",
        gestures=("kb:NVDA+control+e", "kb:NVDA+control+shift+e"),
        category=scriptCategory,
    )
    def script_endTask(self, gesture):
        self._postSignal("end")

    @script(
        description="현재 과업의 지시문을 다시 읽어 줍니다.",
        gestures=("kb:NVDA+control+h", "kb:NVDA+control+shift+t"),
        category=scriptCategory,
    )
    def script_repeatInstruction(self, gesture):
        self._postSignal("repeat")

    def _postSignal(self, kind):
        """Send a start/end/repeat signal from a worker thread; the server decides and may reply text."""
        def run():
            try:
                data = json.dumps({"kind": kind, "source": SIGNAL_SOURCE}).encode("utf-8")
                request = urllib.request.Request(
                    COLLECTOR_URL + "/api/signals",
                    data=data,
                    headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    result = json.loads(response.read().decode("utf-8"))
            except Exception:
                result = {"accepted": False, "reason": "collector_unavailable"}
            self._afterSignal(kind, result)
        threading.Thread(target=run, name="a11yTaskRecorderSignal", daemon=True).start()

    def _afterSignal(self, kind, result):
        """Low beep when the signal does not apply; short confirmation beep, then the reply text."""
        if not isinstance(result, dict) or not result.get("accepted"):
            self._queueMain(tones.beep, 220, 160)
            return
        self._queueMain(tones.beep, 660 if kind == "end" else 880, 90)
        speak = safeString(result.get("speak"), 2000)
        if speak:
            self._queueMain(self._speakPrompt, speak)

    def _queueMain(self, function, *args):
        try:
            queueHandler.queueFunction(queueHandler.eventQueue, function, *args)
        except Exception:
            function(*args)

    def _speakPrompt(self, text):
        """Speak a recorder prompt with NVDA's own voice; the episode is tagged origin=recorder_prompt."""
        self._promptDepth += 1
        try:
            ui.message(text)
        finally:
            self._promptDepth -= 1

    @script(
        description="A11y Task Recorder의 기록 상태를 소리로 확인합니다.",
        gestures=(
            "kb:NVDA+control+l",
            "kb:NVDA+control+shift+r",
        ),
        category=scriptCategory,
    )
    def script_reportStatus(self, gesture):
        if self._isSessionActive():
            tones.beep(880, 80)
            time.sleep(0.04)
            tones.beep(1100, 80)
        else:
            tones.beep(220, 160)

    def _isSessionActive(self):
        with self._sessionLock:
            return bool(self._activeSession)

    def _shouldRecord(self, obj):
        if not self._isSessionActive() or obj is None:
            return False
        try:
            appName = safeString(obj.appModule.appName, 80).lower()
        except Exception:
            return False
        # 0.4.0: the dashboard and the task share one window, so the window title no
        # longer excludes anything. The server's task phase and element.frame do that.
        return appName in SUPPORTED_BROWSER_APPS

    def _foregroundTitle(self):
        try:
            return safeString(api.getForegroundObject().name, 1000)
        except Exception:
            return ""

    def _objectContext(self, obj):
        if obj is None:
            return {}
        context = {}
        context["scope"] = self._objectScope(obj)
        if context["scope"] == "web_content":
            context["frame"] = self._objectFrame(obj)
        try:
            name = safeString(obj.name, 600)
            context["name"] = name
            context["accessible_name"] = name
        except Exception:
            pass
        try:
            role = obj.role
            context["role"] = safeString(getattr(role, "displayString", role), 120)
        except Exception:
            pass
        try:
            context["states"] = [
                safeString(getattr(state, "displayString", state), 80)
                for state in obj.states
            ][:20]
        except Exception:
            pass
        try:
            context["description"] = safeString(obj.description, 600)
        except Exception:
            pass
        try:
            appName = obj.appModule.appName
            context["application"] = safeString(appName, 80)
        except Exception:
            pass
        try:
            ia2UniqueId = getattr(obj, "IA2UniqueID")
            context["ia2_unique_id"] = ia2UniqueId
        except Exception:
            ia2UniqueId = None
        try:
            windowHandle = int(getattr(obj, "windowHandle"))
            context["window_handle"] = windowHandle
        except Exception:
            windowHandle = None
        if ia2UniqueId is not None:
            context["unique_id"] = "ia2:{}:{}:{}".format(
                context.get("application", ""),
                windowHandle if windowHandle is not None else "",
                ia2UniqueId,
            )
        return context

    def _objectScope(self, obj):
        """Distinguish web document objects from the browser's own controls."""
        documentRole = getattr(controlTypes.Role, "DOCUMENT", None)
        current = obj
        for _ in range(20):
            if current is None:
                break
            try:
                if documentRole is not None and current.role == documentRole:
                    return "web_content"
            except Exception:
                pass
            try:
                current = current.parent
            except Exception:
                break
        return "browser_ui"

    def _objectFrame(self, obj):
        """"task" when the object sits inside an embedded frame (the study copy), else "recorder"."""
        frameRole = getattr(controlTypes.Role, "INTERNALFRAME", None)
        current = obj
        for _ in range(40):
            if current is None:
                break
            try:
                if frameRole is not None and current.role == frameRole:
                    return "task"
            except Exception:
                pass
            try:
                current = current.parent
            except Exception:
                break
        return "recorder"

    def _elementKey(self, element):
        return safeString(
            element.get("unique_id")
            or element.get("ia2_unique_id")
            or "{}|{}|{}".format(
                element.get("application", ""),
                element.get("role", ""),
                element.get("name", ""),
            ),
            1000,
        )

    def _enqueue(self, event):
        if not event.get("session_id"):
            with self._sessionLock:
                event["session_id"] = self._activeSession
        if not event["session_id"]:
            return
        if not event.get("timestamp"):
            event["timestamp"] = utcNow()
        event["source"] = "nvda"
        try:
            self._events.put_nowait(event)
        except queue.Full:
            log.debugWarning("A11y Task Recorder queue is full; dropping an event")

    def _workerLoop(self):
        nextPoll = 0.0
        while not self._stopEvent.is_set():
            now = time.monotonic()
            if now >= nextPoll:
                self._pollSession()
                nextPoll = now + POLL_SECONDS
            try:
                event = self._events.get(timeout=0.15)
            except queue.Empty:
                continue
            try:
                self._postEvent(event)
            finally:
                self._events.task_done()

    def _pollSession(self):
        try:
            request = urllib.request.Request(
                COLLECTOR_URL + "/api/active-session",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                data = json.loads(response.read().decode("utf-8"))
            session = data.get("session")
            sessionId = session.get("id") if session else None
            with self._sessionLock:
                previousSession = self._activeSession
            if previousSession != sessionId:
                self._finalizeSpeech(utcNow(), True, "session_boundary_observed")
            with self._sessionLock:
                self._activeSession = sessionId
                self._activeTitle = session.get("title", "") if session else ""
            if sessionId and sessionId != self._environmentReportedSession:
                self._postEnvironment(sessionId)
                self._environmentReportedSession = sessionId
        except Exception:
            with self._sessionLock:
                self._activeSession = None
                self._activeTitle = ""

    def _postEnvironment(self, sessionId):
        environment = {
            "nvda_version": safeString(getattr(buildVersion, "version", ""), 80),
            "nvda_addon_version": safeString(ADDON_VERSION, 80),
        }
        try:
            synth = synthDriverHandler.getSynth()
            environment["synthesizer"] = safeString(getattr(synth, "name", ""), 120)
            environment["speech_rate"] = getattr(synth, "rate", "")
        except Exception:
            pass
        data = json.dumps(
            {"environment_merge": environment},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            COLLECTOR_URL + "/api/sessions/" + sessionId,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="PATCH",
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS):
                pass
        except Exception:
            pass

    def _postEvent(self, event):
        sessionId = event.get("session_id")
        if not sessionId:
            return
        payload = dict(event)
        payload["session_id"] = sessionId
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            COLLECTOR_URL + "/api/events",
            data=data,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS):
                pass
        except urllib.error.HTTPError as error:
            if error.code == 409:
                with self._sessionLock:
                    if self._activeSession == sessionId:
                        self._activeSession = None
            else:
                log.debugWarning(
                    "A11y Task Recorder server returned HTTP %s" % error.code
                )
        except Exception:
            # 네트워크 실패가 NVDA 사용을 방해하지 않도록 조용히 다음 폴링을 기다립니다.
            pass
