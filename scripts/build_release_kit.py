#!/usr/bin/env python3
"""평가 기관 배포용 올인원 키트를 생성한다.

키트에는 Chrome for Testing(확장 자동 로드), 임베디드 Python(별도 설치 불필요),
기록 서버, NVDA 애드온, 설치 안내문이 포함된다. 산출물 ZIP 하나를 전달받은
평가 기관은 압축 해제 → `평가시작.bat` 실행만 하면 된다.

브랜드 Chrome은 137부터 --load-extension을 지원하지 않지만 Chrome for
Testing에서는 계속 지원되므로 개발자 모드나 스토어 등록 없이 확장을 로드한다.

외부 패키지 없이 표준 라이브러리만 사용한다.

    py -3 scripts\\build_release_kit.py [--access-code-file PATH]

--access-code-file로 사이트 접속 코드를 키트(app/study_access.txt)에 넣는다.
기록기가 사이트 자동 로그인과 결과 전송에 쓰고, 참가자·담당자는 코드를 보거나
입력하지 않는다(2026-10-20 결정). 빼고 만든 키트는 사이트에 접속하지 못한다.
코드 값은 어디에도 출력하지 않는다. 코드가 든 키트는 공개 Releases에 올리지 않는다.
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "dist" / "kit-build"
CACHE_DIR = BUILD_DIR / "cache"
CFT_ENDPOINT = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "last-known-good-versions-with-downloads.json"
)
DEFAULT_PYTHON_VERSION = "3.12.9"
DEFAULT_CHROME_VERSION = "151.0.7922.71"

DIAGNOSTIC_BAT = '''@echo off
setlocal DisableDelayedExpansion
set "RECORDER_DIAGNOSTIC_KIT=%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0verify_live_pipeline.ps1" -KitPath "%RECORDER_DIAGNOSTIC_KIT%"
set "DIAGNOSTIC_EXIT=%ERRORLEVEL%"
echo.
echo Results: diagnostic-results folder in this kit, or the displayed TEMP path.
pause
exit /b %DIAGNOSTIC_EXIT%
'''


def remove_tree(path: Path, attempts: int = 8) -> None:
    """Retry removal while Windows releases antivirus or executable file handles."""
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(1.5)

LAUNCHER_BAT = """@echo off
chcp 65001 >nul
setlocal
set KIT=%~dp0
powershell -NoProfile -Command "try { $h=Invoke-RestMethod 'http://127.0.0.1:8765/api/health' -TimeoutSec 1 } catch { exit 0 }; if($h.ok){ if($h.recorder_version -ne '@@EXTENSION_VERSION@@'){ Write-Host '이전 키트의 서버 창을 닫은 뒤 새 키트에서 다시 실행해 주세요.'; exit 2 }; try { $a=Invoke-RestMethod 'http://127.0.0.1:8765/api/active-session' -TimeoutSec 1; if($a.session){ Write-Host '진행 중인 기록을 유지하고 기록 화면을 다시 엽니다.'; exit 3 } } catch { exit 2 } }; exit 0"
if errorlevel 3 goto resume_dashboard
if errorlevel 1 (
  pause
  exit /b 1
)
rem 매 평가 시작 때 애드온, NVDA, 전용 Chrome 순서를 새로 맞춘다.
rem %ProgramFiles(x86)%의 괄호가 if 블록을 깨뜨리므로 블록 밖에서 변수로 만든다.
set "NVDA64=%ProgramFiles%\\NVDA\\nvda.exe"
set "NVDA86=%ProgramFiles(x86)%\\NVDA\\nvda.exe"
set "NVDALNK=%ProgramData%\\Microsoft\\Windows\\Start Menu\\Programs\\NVDA\\NVDA.lnk"
set "ADDON_DIR=%APPDATA%\\nvda\\addons\\a11yTaskRecorder"
rem 이전 실행의 전용 Chrome이 남아 있으면, 그 사이 재시작된 NVDA가 접근성
rem 후킹에 실패할 수 있다. 개인 Chrome은 건드리지 않고 키트 Chrome만 닫는다.
powershell -NoProfile -Command "$root=[IO.Path]::GetFullPath($env:KIT+'app\\chrome'); Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'chrome.exe' -and $_.ExecutablePath -and $_.ExecutablePath.StartsWith($root,[StringComparison]::OrdinalIgnoreCase) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Milliseconds 800"
rem 동봉된 최신 애드온을 매번 동기화한다. 기존 설치가 오래되었거나 NVDA
rem 업데이트로 사라진 경우에도 평가자가 별도 설치 절차를 밟을 필요가 없다.
echo NVDA 기록 애드온을 준비하는 중입니다...
"%KIT%app\\python\\python.exe" -c "import glob,os,shutil,zipfile; p=sorted(glob.glob(os.path.join(os.environ['KIT'],'a11yTaskRecorder-*.nvda-addon')))[-1]; d=os.path.join(os.environ['APPDATA'],'nvda','addons','a11yTaskRecorder'); shutil.rmtree(d,ignore_errors=True); os.makedirs(d,exist_ok=True); zipfile.ZipFile(p).extractall(d)"
if errorlevel 1 (
  echo 경고: NVDA 애드온 자동 설치에 실패했습니다. 설치안내.html을 확인해 주세요.
) else (
  echo NVDA 애드온 준비 완료.
)
rem NVDA 바로가기를 다시 실행하면 실행 중인 NVDA도 공식 동작으로 재시작된다.
if exist "%NVDALNK%" (
  start "" "%NVDALNK%"
) else if exist "%NVDA64%" (
  start "" "%NVDA64%"
) else if exist "%NVDA86%" (
  start "" "%NVDA86%"
) else (
  echo NVDA가 설치되어 있지 않습니다. 설치안내.html의 'NVDA 설치'를 먼저 진행해 주세요.
)
echo NVDA와 기록 애드온이 켜질 때까지 기다리는 중입니다...
powershell -NoProfile -Command "Start-Sleep -Seconds 10"
powershell -NoProfile -Command "try { if((Invoke-RestMethod 'http://127.0.0.1:8765/api/health' -TimeoutSec 1).ok){ exit 0 } } catch {}; exit 1"
if errorlevel 1 (
  start "A11y Task Recorder 서버" "%KIT%app\\python\\python.exe" "%KIT%app\\kit_server.py" --db "%KIT%data\\recorder.sqlite3"
)
powershell -NoProfile -Command "for($i=0;$i -lt 40;$i++){ try { Invoke-RestMethod 'http://127.0.0.1:8765/api/health' -TimeoutSec 1 | Out-Null; exit 0 } catch { Start-Sleep -Milliseconds 250 } }; exit 1"
if errorlevel 1 (
  echo 서버가 시작되지 않았습니다. "A11y Task Recorder 서버" 창의 메시지를 확인해 주세요.
  pause
  exit /b 1
)
echo NVDA 애드온 연결을 확인하는 중입니다...
powershell -NoProfile -Command "$ok=$false; for($i=0;$i -lt 20;$i++){ try { if((Invoke-RestMethod 'http://127.0.0.1:8765/api/health' -TimeoutSec 1).nvda_connected){$ok=$true;break} } catch {}; Start-Sleep -Milliseconds 700 }; if($ok){ Write-Host 'NVDA 애드온 연결 확인됨.' } else { Write-Host '경고: NVDA 애드온이 연결되지 않았습니다. 기록 시작은 차단됩니다.' }"
:resume_dashboard
start "" "%KIT%app\\chrome\\chrome.exe" --load-extension="%KIT%app\\browser-extension" --user-data-dir="%KIT%app\\chrome-profile-@@EXTENSION_VERSION@@" --no-first-run --no-default-browser-check "http://127.0.0.1:8765"
endlocal
"""

KIT_SERVER_PY = '''"""키트 실행용 서버 래퍼: 안내 문구와 오류 안내를 덧붙인다."""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

print("A11y Task Recorder 서버 창입니다.")
print("이 창을 닫으면 기록이 중단됩니다. 평가가 끝날 때까지 열어 두세요.")
print()

from recorder.server import main

try:
    sys.argv = [sys.argv[0], "--no-browser"] + sys.argv[1:]
    main()
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
'''

GUIDE_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A11y Task Recorder 평가 키트 안내</title>
<style>
body { font-family: "Malgun Gothic", sans-serif; max-width: 46rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.7; color: #1a1a1a; }
h1 { font-size: 1.6rem; } h2 { font-size: 1.25rem; margin-top: 2rem; }
ol li, ul li { margin: 0.4rem 0; }
kbd { background: #eee; border: 1px solid #bbb; border-radius: 4px; padding: 0 0.35em; font-family: inherit; }
table { border-collapse: collapse; } th, td { border-bottom: 1px solid #ccc; padding: 0.3rem 0.8rem 0.3rem 0; text-align: left; vertical-align: top; }
.note { background: #f5f2e9; border-left: 4px solid #b0a583; padding: 0.6rem 1rem; }
</style>
</head>
<body>
<h1>A11y Task Recorder 평가 키트 안내</h1>
<p>화면낭독기(NVDA)로 웹 과업을 수행하는 동안 누른 키와 들은 안내를 기록하는 도구입니다.
이 문서는 <strong>평가용 컴퓨터를 준비하는 담당자</strong>가 읽습니다. 참가자는 화면 안내와 연습 과업으로 사용법을 익힌 뒤 혼자 진행합니다.</p>

<h2>한눈에 보기</h2>
<ol>
<li><strong>처음 한 번</strong>: 평가용 컴퓨터에 NVDA를 설치합니다.</li>
<li><strong>세션 준비 (담당자, 약 3분)</strong>: 키트를 실행하고 테스터 번호를 고릅니다.</li>
<li><strong>과업 진행 (참가자 혼자)</strong>: 연습 1개를 한 뒤 배정된 과업을 합니다. 과업 하나는 최대 10분이고, 4개마다 쉴 수 있습니다.</li>
<li><strong>마무리 (담당자, 약 2분)</strong>: 결과 ZIP을 만들어 연구진에게 보냅니다.</li>
</ol>

<h2>1. 처음 한 번: NVDA 설치</h2>
<p>준비물: 64비트 Windows 10 또는 11, 인터넷, 스피커나 이어폰. 이미 NVDA가 설치되어 있으면 건너뜁니다.</p>
<ol>
<li><a href="https://www.nvaccess.org/download/">NVDA 다운로드 페이지</a>에서 <strong>Download NVDA</strong>를 눌러 설치 파일을 받습니다.</li>
<li>받은 <strong>nvda_….exe</strong>를 실행하고, 보안 확인 창이 나오면 <strong>예</strong>를 누릅니다.</li>
<li>사용권 계약에 동의하고 <strong>이 컴퓨터에 NVDA 설치</strong>를 고릅니다. 설치가 끝나고 NVDA 음성이 나오면 됩니다.</li>
</ol>

<h2>2. 세션 준비 (담당자)</h2>
<ol>
<li>키트 폴더의 <strong>평가시작.bat</strong>을 더블클릭합니다. 처음에 Windows 보안 창이 나오면 <strong>추가 정보</strong>, <strong>실행</strong>을 차례로 누릅니다. 연구진에게 받은 키트일 때만 실행하세요.</li>
<li>잠시 기다리면 NVDA가 다시 켜지고, 검은색 <strong>서버 창</strong>과 기록 화면이 열립니다. 서버 창은 끝날 때까지 <strong>닫지 마세요</strong>. 닫으면 기록이 멈춥니다.</li>
<li>기록 화면 위쪽에 <strong>NVDA 연결됨</strong>이 보이는지 확인합니다.</li>
<li>참가자의 <strong>테스터 번호</strong>(예: T01)를 고르고 <strong>배정 확인</strong>을 누릅니다. 그러면 번호 칸이 사라집니다.</li>
<li>화면에 "사이트가 준비되었습니다"가 나오면 참가자에게 자리를 넘깁니다.</li>
</ol>

<h2>3. 과업 진행 (참가자)</h2>
<p>참가자는 연습 과업에서 아래 순서를 한 번 해 본 뒤 본 과업을 합니다. 담당자는 기술 문제가 생겼을 때만 돕습니다.</p>
<ol>
<li><strong>연습</strong>: 처음에는 <strong>연습 시작</strong>을 누릅니다. 연습 지시문 뒤에 단축키 안내가 나옵니다. 연습은 기록하지 않습니다.</li>
<li><strong>듣기</strong>: 지시문으로 초점이 가면 NVDA가 읽어 줍니다.</li>
<li><strong>시작</strong>: 지시문 바로 아래 <strong>과업 시작</strong> 버튼을 누릅니다. 사이트가 열리고 기록이 시작됩니다.</li>
<li><strong>수행</strong>: 평소처럼 키보드와 NVDA로 과업을 합니다.</li>
<li><strong>끝내기</strong>: 다 했거나 더 진행할 수 없으면 <kbd>NVDA</kbd>+<kbd>Ctrl</kbd>+<kbd>E</kbd>를 누릅니다. 10분이 지나면 저절로 끝납니다.</li>
<li><strong>결과</strong>: <strong>마쳤습니다</strong> 또는 <strong>마치지 못했습니다</strong>를 고르고, 답을 적고 <strong>저장</strong>합니다.</li>
<li><strong>다음</strong>: <strong>다음 과업</strong>을 누르면 2번으로 돌아갑니다. 4개 과업마다 원하는 만큼 쉬어도 됩니다.</li>
</ol>
<p>과업 중에 지시문이 기억나지 않으면 <kbd>NVDA</kbd>+<kbd>Ctrl</kbd>+<kbd>H</kbd>를 누릅니다. 지시문과 끝내는 방법을 다시 읽어 주고, 초점은 사이트에 그대로 있습니다.</p>
<p>사이트 아래에도 <strong>과업 끝내기</strong> 버튼이 있습니다. 실수로 누르지 않도록 Tab으로는 가지 않고, 마우스나 NVDA 브라우즈 모드(아래 화살표)로만 닿습니다. 누르면 "과업을 끝낼까요?"라고 묻습니다. <strong>끝내기</strong>를 누르면 끝나고, <strong>계속하기</strong>를 누르면 사이트로 돌아갑니다.</p>

<h2>평가 중 단축키</h2>
<table>
<tr><th>단축키</th><th>하는 일</th></tr>
<tr><td><kbd>NVDA</kbd>+<kbd>Ctrl</kbd>+<kbd>E</kbd></td><td>과업 끝내기 (묻지 않고 바로 끝남)</td></tr>
<tr><td><kbd>NVDA</kbd>+<kbd>Ctrl</kbd>+<kbd>H</kbd></td><td>지시문 다시 듣기 (끝내는 단축키도 함께 알려 줌)</td></tr>
<tr><td><kbd>NVDA</kbd>+<kbd>Ctrl</kbd>+<kbd>L</kbd></td><td>기록 상태 확인 (높아지는 음 두 번: 기록 중, 낮은 음: 대기 중). 담당자용</td></tr>
</table>
<p>단축키를 누르면 짧은 확인음이 나고, 지금 쓸 수 없는 단축키는 낮은 경고음이 납니다. 과업 시작은 버튼으로 합니다. 참가자에게 단축키를 말로 알려 줄 때는 "NVDA, Ctrl, E를 함께"처럼 쉼표로 끊어 말해 주세요.</p>
<p>노트북 배열에서 CapsLock을 NVDA 키로 쓰려면 NVDA 설정의 <strong>키보드</strong>에서 <strong>CapsLock을 NVDA 키로 사용</strong>을 켜야 합니다. 켜지 않으면 CapsLock 단축키가 동작하지 않고 대문자 잠금만 켜집니다.</p>

<h2>4. 마무리 (담당자)</h2>
<ol>
<li>배정된 과업이 모두 끝나면 "배정된 과업을 모두 마쳤습니다" 화면이 나옵니다.</li>
<li>그 화면의 <strong>결과 패키지 만들기 (담당자용)</strong>를 누릅니다. <strong>결과</strong> 폴더가 열리고 ZIP 파일 하나가 생깁니다.</li>
<li>그 ZIP 파일을 연구진에게 보냅니다(이메일 또는 안내받은 업로드 링크). 과업 기록은 연구 서버로도 자동 전송되지만, <strong>ZIP은 꼭 보내 주세요.</strong></li>
<li>기록 화면(브라우저)과 서버 창을 닫습니다.</li>
</ol>
<p>세션을 중간에 끝내야 하면 기록 화면 주소 끝에 <code>?change-tester=1</code>을 붙여 다시 열고(<code>http://127.0.0.1:8765/?change-tester=1</code>), 나타나는 <strong>기록 관리</strong>에서 결과 패키지를 만듭니다.</p>

<h2>문제 해결</h2>
<ul>
<li><strong>기록 화면이 안 열려요</strong>: 서버 창이 열려 있는지 확인하고, 평가용 브라우저에서 <code>http://127.0.0.1:8765</code>를 다시 엽니다.</li>
<li><strong>배정 확인 뒤 "기록 프로그램에 연결할 수 없습니다"가 나와요</strong>: 평가용 브라우저를 막 연 직후에 생길 수 있습니다. <kbd>F5</kbd>로 기록 화면을 새로 고친 뒤 다시 배정 확인을 누르세요.</li>
<li><strong>"NVDA 연결됨"이 안 보여요</strong>: 기록 화면과 서버 창을 닫고 <strong>평가시작.bat</strong>을 다시 실행해 보세요. 그래도 안 되면 연구진에게 알려 주세요.</li>
<li><strong>"사이트에 접속할 수 없습니다"가 나와요</strong>: 인터넷 연결을 확인하고, 그래도 안 되면 연구진에게 알려 주세요.</li>
<li><strong>단축키를 누르면 낮은 음만 나요</strong>: 지금은 그 단축키를 쓸 수 없는 단계입니다(예: 과업 시작 전에 끝내기). 서버 창이 열려 있는지도 확인하세요.</li>
<li><strong>테스터 번호를 잘못 골랐어요</strong>: <code>http://127.0.0.1:8765/?change-tester=1</code>로 다시 열어 고릅니다. 이미 기록 중인 과업이 있으면 바꿀 수 없습니다.</li>
<li><strong>_연구진용 폴더</strong>: 연구진이 설치를 점검할 때 쓰는 파일입니다. 담당자는 열거나 실행하지 마세요.</li>
<li><strong>새 키트로 바꿔야 해요</strong>: 서버 창과 평가용 브라우저를 닫고, 새 ZIP을 다른 폴더에 풉니다. 기존 기록을 이어가려면 이전 키트의 <code>data</code> 폴더 전체를 새 키트로 복사하고, 이전 폴더는 지우지 말고 보관합니다.</li>
<li><strong>동영상이 재생되지 않아요</strong>: 평가용 브라우저에는 일부 상용 동영상 코덱이 없습니다. 과업에 문제가 되면 연구진에게 알려 주세요.</li>
</ul>

<h2>어떤 데이터가 기록되나요?</h2>
<p>과업을 시작한 순간부터 끝낼 때까지만 기록합니다.</p>
<ul>
<li><strong>NVDA</strong>: 누른 키와 입력한 글자, 그때 초점이 있던 요소의 이름과 역할, NVDA가 읽어 준 안내 전문과 읽기 시작·중단 시각</li>
<li><strong>평가용 브라우저</strong>: 방문한 페이지 주소와 제목, 화면이 바뀐 횟수와 시각(바뀐 내용은 저장하지 않음)</li>
<li><strong>참가자 입력</strong>: 과업을 마쳤는지, 적은 답</li>
<li><strong>환경</strong>: NVDA 버전과 음성, 브라우저와 확장 버전</li>
</ul>
<div class="note">
<p>입력한 글자와 주소의 검색어도 가리지 않고 저장합니다. 과업에 넣는 값은 연구진이 준 가상 값입니다.
<strong>저장하지 않는 것</strong>: 목소리 녹음, 화면 녹화, 화면에 바뀐 내용 자체.
모든 기록은 이 컴퓨터의 <strong>data</strong> 폴더에 저장되고, 과업이 끝날 때마다 그 과업의 기록이 연구진 서버로 전송됩니다. 결과 패키지 ZIP도 따로 연구진에게 보냅니다.</p>
</div>

<h2>문의</h2>
<p>서울과학기술대학교 HAILAB 김유신 연구원 · <a href="mailto:yusinkim@seoultech.ac.kr">yusinkim@seoultech.ac.kr</a></p>
</body>
</html>
"""


def log(message: str = "") -> None:
    """Print build progress without failing on a console or pipe that cannot encode Korean."""
    try:
        print(message)
    except UnicodeEncodeError:
        # e.g. a cp1252 pipe on an English Windows runner: escape instead of aborting the build.
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(message.encode(encoding, "backslashreplace").decode(encoding))


def read_manifest_version(path: Path, key: str = "version") -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(key):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("{}에서 {} 값을 찾지 못했습니다.".format(path, key))


def download(url: str, target: Path) -> Path:
    if target.exists() and zipfile.is_zipfile(target):
        log("캐시 사용: {}".format(target.name))
        return target
    log("다운로드: {}".format(url))
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    try:
        with urlopen(url, timeout=60) as response, open(partial, "wb") as handle:
            shutil.copyfileobj(response, handle)
        if not zipfile.is_zipfile(partial):
            raise ValueError("Downloaded file is not a ZIP archive: {}".format(url))
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    log("  완료 ({:.1f} MB)".format(target.stat().st_size / 1024 / 1024))
    return target


def fetch_cft_stable() -> dict:
    with urlopen(CFT_ENDPOINT) as response:
        data = json.load(response)
    stable = data["channels"]["Stable"]
    win64 = next(
        item["url"]
        for item in stable["downloads"]["chrome"]
        if item["platform"] == "win64"
    )
    return {"version": stable["version"], "url": win64}


def extract_flat(archive: Path, destination: Path) -> None:
    """ZIP을 풀되 최상위 폴더가 하나뿐이면 그 내용물을 destination에 평탄화한다."""
    staging = destination.parent / (destination.name + ".extract")
    if staging.exists():
        shutil.rmtree(staging)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(staging)
    entries = list(staging.iterdir())
    source = entries[0] if len(entries) == 1 and entries[0].is_dir() else staging
    shutil.move(str(source), str(destination))
    if staging.exists():
        remove_tree(staging)


def read_access_code(path: Path) -> str:
    """Code file (bare code or JSON with `code`) -> code. The value is never printed."""
    repo = str(Path(__file__).resolve().parent.parent)
    if repo not in sys.path:
        sys.path.insert(0, repo)
    from recorder.upload import parse_access_code
    try:
        code = parse_access_code(path.read_text(encoding="utf-8-sig"))
    except OSError as error:
        raise SystemExit("접속 코드 파일을 읽지 못했습니다: {} ({})".format(path, error.strerror))
    if not code:
        raise SystemExit("접속 코드 파일에서 코드를 찾지 못했습니다: {}".format(path))
    return code


# The packaged catalog names this placeholder (scripts/build_study_catalog.py, public repository);
# the kit gets the real copy origin from a private file.
PUBLIC_ORIGIN = "https://study-copy.invalid"


def read_site_origin(path: Path) -> str:
    """Origin file (bare origin, or JSON with `origin` such as the lab operator catalog) -> origin. Never printed."""
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except OSError as error:
        raise SystemExit("사본 주소 파일을 읽지 못했습니다: {} ({})".format(path, error.strerror))
    try:
        value = json.loads(text).get("origin", "") if text.startswith("{") else text
    except ValueError:
        value = ""
    parts = urlsplit(value) if isinstance(value, str) else None
    if not parts or parts.scheme != "https" or not parts.hostname or parts.path not in ("", "/") \
            or parts.query or parts.fragment or parts.username or parts.password:
        raise SystemExit("사본 주소 파일에서 HTTPS 주소를 찾지 못했습니다: {}".format(path))
    return "https://" + parts.netloc


def build_kit(python_version: str, chrome_version: str, access_code_file: Optional[Path] = None,
              site_origin_file: Optional[Path] = None) -> Path:
    access_code = read_access_code(access_code_file) if access_code_file else ""
    site_origin = read_site_origin(site_origin_file) if site_origin_file else ""
    addon_version = read_manifest_version(ROOT / "nvda-addon" / "manifest.ini")
    extension_version = json.loads(
        (ROOT / "browser-extension" / "manifest.json").read_text(encoding="utf-8")
    )["version"]
    addon_file = ROOT / "dist" / "a11yTaskRecorder-{}.nvda-addon".format(addon_version)
    log("NVDA 애드온 설치 파일 생성 중...")
    subprocess.check_call([sys.executable, str(ROOT / "scripts" / "build_nvda_addon.py")])

    cft = (
        fetch_cft_stable()
        if chrome_version == "latest"
        else {
            "version": chrome_version,
            "url": (
                "https://storage.googleapis.com/chrome-for-testing-public/"
                + chrome_version
                + "/win64/chrome-win64.zip"
            ),
        }
    )
    log("Chrome for Testing 선택 버전: {}".format(cft["version"]))
    cft_zip = download(cft["url"], CACHE_DIR / "chrome-win64-{}.zip".format(cft["version"]))
    python_zip = download(
        "https://www.python.org/ftp/python/{0}/python-{0}-embed-amd64.zip".format(python_version),
        CACHE_DIR / "python-{}-embed-amd64.zip".format(python_version),
    )

    kit_name = "A11yTaskRecorder-{}-chrome{}".format(addon_version, cft["version"])
    staging = BUILD_DIR / kit_name
    if staging.exists():
        remove_tree(staging)
    app = staging / "app"
    app.mkdir(parents=True)

    log("구성 요소 복사 중...")
    shutil.copytree(ROOT / "recorder", app / "recorder",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "static", app / "static")
    catalog_file = app / "recorder" / "study_catalog.json"
    if site_origin:
        catalog_file.write_text(catalog_file.read_text(encoding="utf-8").replace(PUBLIC_ORIGIN, site_origin),
                                encoding="utf-8", newline="\n")
        log("사본 주소: 키트 배정표에 넣음")
    else:
        log("사본 주소: 넣지 않음 (이 키트로는 사이트에 접속할 수 없음, 배포 금지)")
    shutil.copytree(ROOT / "browser-extension", app / "browser-extension")
    shutil.copy2(ROOT / "run.py", app / "run.py")
    shutil.copy2(addon_file, staging / addon_file.name)
    research = staging / "_연구진용"  # 설치 점검 도구: 담당자가 실수로 실행하지 않게 최상위에서 뺀다
    research.mkdir()
    shutil.copy2(ROOT / "scripts" / "verify_live_pipeline.ps1", research / "verify_live_pipeline.ps1")
    shutil.copy2(ROOT / "docs" / "windows-release-validation.md", research / "windows-release-validation.md")
    shutil.copy2(ROOT / "docs" / "kit-session-checklist.md", staging / "진행안내.md")

    log("Chrome for Testing 압축 해제 중...")
    extract_flat(cft_zip, app / "chrome")
    log("임베디드 Python 압축 해제 중...")
    extract_flat(python_zip, app / "python")

    # 임베디드 Python은 ._pth에 적힌 경로만 sys.path로 쓰므로 app 폴더를 추가한다.
    pth_file = next((app / "python").glob("python*._pth"))
    pth_file.write_text(pth_file.read_text(encoding="utf-8") + "..\n", encoding="utf-8")

    (app / "kit_server.py").write_text(KIT_SERVER_PY, encoding="utf-8")
    if access_code:
        # recorder/upload.py reads app/study_access.txt for the iframe login and the upload bearer.
        (app / "study_access.txt").write_text(access_code + "\n", encoding="utf-8", newline="\n")
        log("사이트 접속 코드: 키트에 내장함")
    else:
        log("사이트 접속 코드: 내장하지 않음 (이 키트로는 사이트에 접속할 수 없음, 배포 금지)")
    (staging / "평가시작.bat").write_text(
        LAUNCHER_BAT.replace("@@EXTENSION_VERSION@@", extension_version),
        encoding="utf-8",
        newline="\r\n",
    )
    (staging / "_연구진용" / "검수실행.bat").write_text(DIAGNOSTIC_BAT, encoding="ascii", newline="\r\n")
    (staging / "설치안내.html").write_text(
        GUIDE_HTML.replace("@@ADDON_VERSION@@", addon_version), encoding="utf-8"
    )
    (staging / "_연구진용" / "KIT-INFO.txt").write_text(
        "\n".join(
            [
                "A11y Task Recorder 평가 키트",
                "생성 시각: {}".format(datetime.now(timezone.utc).isoformat(timespec="seconds")),
                "애드온/서버 버전: {}".format(addon_version),
                "브라우저 확장 버전: {}".format(extension_version),
                "Chrome for Testing: {}".format(cft["version"]),
                "임베디드 Python: {}".format(python_version),
                "사이트 접속 코드: {}".format("키트에 내장" if access_code else "내장하지 않음"),
                "사본 주소: {}".format("배정표에 넣음" if site_origin else "넣지 않음"),
                "",
                "연구 보고 시 브라우저는 'Chrome for Testing {}(동일 버전 Chrome과 같은 엔진)'으로 기재하세요.".format(cft["version"]),
                "",
            ]
        ),
        encoding="utf-8",
    )

    log("ZIP 생성 중...")
    zip_path = ROOT / "dist" / "{}.zip".format(kit_name)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(staging.rglob("*")):
            bundle.write(path, Path(kit_name) / path.relative_to(staging))
    log("완료: {} ({:.1f} MB)".format(zip_path, zip_path.stat().st_size / 1024 / 1024))
    log("검증용 폴더: {}".format(staging))
    return zip_path


def main() -> None:
    parser = argparse.ArgumentParser(description="평가 기관 배포용 키트 생성")
    parser.add_argument("--python-version", default=DEFAULT_PYTHON_VERSION)
    parser.add_argument(
        "--chrome-version",
        default=DEFAULT_CHROME_VERSION,
        help="고정 Chrome for Testing 버전. 최신판은 'latest'를 사용합니다.",
    )
    parser.add_argument(
        "--access-code-file",
        type=Path,
        default=None,
        help="사이트 접속 코드 파일(코드 문자열, 또는 JSON의 code/accessCode). 주면 키트 app/study_access.txt에 넣습니다.",
    )
    parser.add_argument(
        "--site-origin-file",
        type=Path,
        default=None,
        help="사본 주소 파일(주소 문자열, 또는 JSON의 origin, 예: lab의 operator-catalog.private.json). 주면 키트 배정표의 자리표시를 바꿉니다.",
    )
    args = parser.parse_args()
    build_kit(args.python_version, args.chrome_version, args.access_code_file, args.site_origin_file)


if __name__ == "__main__":
    main()
