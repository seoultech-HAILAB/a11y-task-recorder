"""키트 실행용 서버 래퍼: 안내 문구와 오류 안내를 덧붙인다.

0.5.9: 요청마다 찍던 줄을 지운다(서버 창으로 가면 NVDA가 그 줄을 계속 읽었다). 기록 화면을
닫으면 3분 뒤 서버도 저절로 끝나, 참가자가 서버 창을 찾아 닫지 않아도 된다.
"""
import os
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

IDLE_SECONDS = 180
_last_dashboard = {"at": None}


def note_request(handler) -> None:
    """Remember when the recorder screen (or the extension) last asked; the NVDA add-on does not count."""
    headers = handler.headers
    if headers is None:
        return
    agent = headers.get("User-Agent", "")
    # The launcher's own PowerShell checks do not mean the screen is open either.
    if headers.get("X-A11y-Client") == "nvda-addon" or "Python-urllib" in agent or "PowerShell" in agent:
        return
    _last_dashboard["at"] = time.monotonic()


def idle(now: float) -> bool:
    last = _last_dashboard["at"]
    return last is not None and now - last > IDLE_SECONDS


def quiet_and_watch(recorder_server) -> None:
    def log_request(self, code="-", size="-"):
        note_request(self)
    recorder_server.RecorderHandler.log_request = log_request

    def watch():
        while True:
            time.sleep(15)
            if idle(time.monotonic()):
                print("기록 화면이 닫혀 서버를 끝냅니다. 기록은 data 폴더에 저장되어 있습니다.")
                os._exit(0)
    threading.Thread(target=watch, daemon=True).start()


def main() -> None:
    print("A11y Task Recorder 서버 창입니다.")
    print("평가 중에는 이 창을 닫지 마세요. 기록 화면을 닫으면 3분 뒤 이 창도 저절로 닫힙니다.")
    print()
    from recorder import server as recorder_server
    quiet_and_watch(recorder_server)
    try:
        sys.argv = [sys.argv[0], "--no-browser"] + sys.argv[1:]
        recorder_server.main()
    except OSError as error:
        if getattr(error, "errno", None) == 10048 or "10048" in str(error):
            print()
            print("서버가 이미 실행 중입니다 (포트 8765 사용 중).")
            print("기존 'A11y Task Recorder 서버' 창을 그대로 사용하면 됩니다.")
            input("엔터 키를 누르면 이 창이 닫힙니다.")
        else:
            traceback.print_exc()
            input("오류가 발생했습니다. 엔터 키를 누르면 이 창이 닫힙니다.")
    except Exception:
        traceback.print_exc()
        input("오류가 발생했습니다. 엔터 키를 누르면 이 창이 닫힙니다.")


if __name__ == "__main__":
    main()
