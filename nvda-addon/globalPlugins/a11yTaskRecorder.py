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
import synthDriverHandler
import tones
from logHandler import log
from scriptHandler import script
from speech import extensions as speechExtensions


COLLECTOR_URL = "http://127.0.0.1:8765"
POLL_SECONDS = 1.25
REQUEST_TIMEOUT_SECONDS = 0.8
SUPPORTED_BROWSER_APPS = {"chrome", "msedge", "firefox", "brave", "opera"}
SPEECH_MERGE_SECONDS = 1.2
ADDON_VERSION = addonHandler.getCodeAddon().manifest["version"]
SAFE_UNBOUND_KEYS = {
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
    if not text or text.startswith("[입력 문자"):
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
        self._lastTypedCharacterAt = 0.0
        self._speechLock = threading.RLock()
        self._activeSpeech = None
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

    def _onGesture(self, gesture):
        """Observe gestures without changing whether NVDA executes them."""
        try:
            if getattr(gesture, "isModifier", False):
                return True
            displayName = safeString(getattr(gesture, "displayName", ""), 180)
            scriptObject = getattr(gesture, "script", None)
            identifiers = []
            for identifier in getattr(gesture, "identifiers", [])[:3]:
                value = safeString(identifier, 180)
                if value:
                    identifiers.append(value)
            stableGesture = identifiers[0].lower() if identifiers else ""
            isPrintable = len(displayName) == 1 and displayName.isprintable()
            if isPrintable and scriptObject is None:
                # Do not store typed characters. This timestamp also redacts character echo speech.
                self._lastTypedCharacterAt = time.monotonic()
                return True
            if scriptObject is None and stableGesture not in SAFE_UNBOUND_KEYS and not any(
                modifier in stableGesture
                for modifier in ("control+", "ctrl+", "alt+", "windows+", "win+")
            ):
                return True
            focus = api.getFocusObject()
            if not self._shouldRecord(focus):
                return True
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
        try:
            focus = api.getFocusObject()
            if not self._shouldRecord(focus):
                return
            textParts = [item for item in speechSequence if isinstance(item, str)]
            text = " ".join(part.strip() for part in textParts if part.strip())
            if not text:
                return
            redacted = False
            if (
                time.monotonic() - self._lastTypedCharacterAt < 1.2
                or self._isProtected(focus)
                or self._isEditable(focus)
            ):
                text = "[입력 문자 음성 출력 숨김]"
                redacted = True
            navigator = None
            try:
                navigator = api.getNavigatorObject()
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
            with self._speechLock:
                active = self._activeSpeech
                canMerge = (
                    active
                    and active["element_key"] == elementKey
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
                        "timestamp": nowTimestamp,
                        "last_monotonic": nowMonotonic,
                        "element_key": elementKey,
                        "element": element,
                        "page_title": self._foregroundTitle(),
                        "priority": safeString(priority, 80),
                        "redacted": redacted,
                        "fragments": [fragment],
                    }
                else:
                    self._activeSpeech["last_monotonic"] = nowMonotonic
                    self._activeSpeech["redacted"] = (
                        self._activeSpeech["redacted"] or redacted
                    )
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
                        "redacted": active["redacted"],
                        "end_reason": endReason,
                        "preprocessing": "role-descriptor-v1",
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
        if appName not in SUPPORTED_BROWSER_APPS:
            return False
        title = self._foregroundTitle().lower()
        if "a11y task recorder" in title:
            return False
        return True

    def _foregroundTitle(self):
        try:
            return safeString(api.getForegroundObject().name, 1000)
        except Exception:
            return ""

    def _isProtected(self, obj):
        try:
            return controlTypes.State.PROTECTED in obj.states
        except Exception:
            return False

    def _isEditable(self, obj):
        try:
            return obj.role == controlTypes.Role.EDITABLETEXT
        except Exception:
            return False

    def _objectContext(self, obj):
        if obj is None:
            return {}
        context = {}
        context["scope"] = self._objectScope(obj)
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
        with self._sessionLock:
            sessionId = self._activeSession
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
                    self._activeSession = None
            else:
                log.debugWarning(
                    "A11y Task Recorder server returned HTTP %s" % error.code
                )
        except Exception:
            # 네트워크 실패가 NVDA 사용을 방해하지 않도록 조용히 다음 폴링을 기다립니다.
            pass
