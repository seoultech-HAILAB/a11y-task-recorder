#!/usr/bin/env python3
"""평가 기관 배포용 올인원 키트를 생성한다.

키트에는 Chrome for Testing(확장 자동 로드), 임베디드 Python(별도 설치 불필요),
기록 서버, NVDA 애드온, 설치 안내문이 포함된다. 산출물 ZIP 하나를 전달받은
평가 기관은 압축 해제 → `평가시작.bat` 실행만 하면 된다.

브랜드 Chrome은 137부터 --load-extension을 지원하지 않지만 Chrome for
Testing에서는 계속 지원되므로 개발자 모드나 스토어 등록 없이 확장을 로드한다.

외부 패키지 없이 표준 라이브러리만 사용한다.

    py -3 scripts\\build_release_kit.py
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
echo A11y Task Recorder NVDA 애드온을 확인하는 중입니다...
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
echo NVDA와 Recorder 애드온이 시작될 때까지 기다리는 중입니다...
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
<title>A11y Task Recorder 평가 키트 설치 안내</title>
<style>
body { font-family: "Malgun Gothic", sans-serif; max-width: 46rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.7; color: #1a1a1a; }
h1 { font-size: 1.6rem; } h2 { font-size: 1.25rem; margin-top: 2rem; }
ol li, ul li { margin: 0.4rem 0; }
kbd { background: #eee; border: 1px solid #bbb; border-radius: 4px; padding: 0 0.35em; font-family: inherit; }
.note { background: #f5f2e9; border-left: 4px solid #b0a583; padding: 0.6rem 1rem; }
</style>
</head>
<body>
<h1>A11y Task Recorder 평가 키트 설치 안내</h1>
<p>이 키트는 스크린 리더(NVDA) 웹 과업 평가를 기록하는 도구입니다.
프로그램 설치나 개발 지식 없이, 아래 순서만 따라 하면 됩니다.</p>

<h2>준비물</h2>
<ul>
<li>64비트 Windows 10 또는 11</li>
<li>인터넷 연결, 소리를 들을 수 있는 스피커나 이어폰</li>
</ul>

<h2>처음 한 번만 ①: NVDA 설치</h2>
<p>이미 NVDA(스크린 리더)가 설치되어 있다면 이 단계는 건너뜁니다.</p>
<ol>
<li><a href="https://www.nvaccess.org/download/">NVDA 다운로드 페이지</a>에서 <strong>Download NVDA</strong>를 눌러 설치 파일을 받습니다.</li>
<li>받은 <strong>nvda_….exe</strong>를 실행합니다. 보안 확인 창이 나오면 <strong>예</strong>를 누릅니다.</li>
<li>사용권 계약에 동의하고 <strong>이 컴퓨터에 NVDA 설치</strong>를 선택해 설치를 마칩니다.</li>
<li>설치 후 NVDA 음성이 나오면 정상입니다.</li>
</ol>

<h2>평가 시작하기</h2>
<ol>
<li><strong>평가시작.bat</strong>을 더블클릭합니다.</li>
<li>처음 실행할 때 Windows 보안 확인 창이 나오면 <strong>추가 정보 → 실행</strong>을 선택합니다. (이 키트는 연구실에서 전달받은 파일일 때만 실행하세요.)</li>
<li>Recorder 애드온 설치, NVDA 재시작과 평가용 Chrome 실행은 자동으로 진행됩니다.</li>
<li>검은색 <strong>서버 창</strong>과 <strong>평가용 브라우저</strong>(Chrome for Testing)가 열리고, 대시보드 상단에 <strong>NVDA 연결됨</strong>이 표시되는지 확인합니다.</li>
<li>서버 창은 닫지 말고 그대로 두세요. 이 창을 닫으면 기록이 중단됩니다.</li>
<li>대시보드에서 세션을 만들고 <strong>기록 시작</strong>을 누른 뒤, 같은 브라우저에서 평가 과업을 진행합니다.</li>
</ol>

<h2>평가 중 단축키</h2>
<ul>
<li><kbd>NVDA</kbd>+<kbd>Control</kbd>+<kbd>L</kbd> — 기록 상태 확인음 (상승음 두 번: 기록 중, 낮은음: 대기 중)</li>
<li><kbd>NVDA</kbd>+<kbd>Control</kbd>+<kbd>I</kbd> — 불편한 순간 표시 (높은 확인음)</li>
</ul>

<h2>평가를 마치면</h2>
<ol>
<li>대시보드에서 <strong>완료하고 종료</strong>를 누르고, 회고 확인과 의견을 저장합니다.</li>
<li>기록 보관함 옆의 <strong>결과 패키지 만들기</strong> 버튼을 누릅니다. <strong>결과</strong> 폴더가 자동으로 열리고 그 안에 ZIP 파일 하나가 생깁니다.</li>
<li>그 ZIP 파일 하나를 연구 담당자에게 전달합니다(이메일 또는 안내받은 업로드 링크).</li>
<li>평가용 브라우저와 서버 창을 닫습니다.</li>
</ol>

<h2>문제 해결</h2>
<ul>
<li><strong>대시보드가 안 열려요</strong> — 서버 창이 열려 있는지 확인하고, 브라우저에서 <code>http://127.0.0.1:8765</code>를 다시 엽니다.</li>
<li><strong>확인음이 낮은음만 나요</strong> — 서버 창이 닫혀 있지 않은지, 대시보드에서 세션이 <strong>기록 중</strong>인지 확인합니다.</li>
<li><strong>NVDA가 애드온을 인식하지 못해요</strong> — NVDA 메뉴 → 도구 → 애드온 스토어 → 설치된 애드온에서 A11y Task Recorder가 활성 상태인지 확인하고 NVDA를 재시작합니다.</li>
<li><strong>일부 동영상이 재생되지 않아요</strong> — 평가용 브라우저에는 일부 상용 동영상 코덱이 없습니다. 과업에 문제가 되면 연구 담당자에게 알려 주세요.</li>
</ul>

<h2>어떤 데이터가 기록되나요?</h2>
<p>기록은 세션이 <strong>기록 중</strong>인 동안에만 이루어집니다. 참여자 동의 안내에 그대로 사용할 수 있습니다.</p>
<ul>
<li><strong>NVDA</strong>: 누른 키(예: Tab), 그 순간 포커스된 요소의 이름·역할, NVDA가 읽어 준 안내 문구 전문과 읽기 시작·중단 시각, 불편 표시 시점</li>
<li><strong>평가용 브라우저</strong>: 방문한 페이지 주소(검색어 등 매개변수 제외), 페이지 제목, 화면 변화 횟수(내용은 저장하지 않음)</li>
<li><strong>연구자 입력</strong>: 시나리오·참여자 코드·step, 힌트, 문제 라벨, 종합 의견</li>
<li><strong>환경 정보</strong>: NVDA 버전·합성기, 브라우저·확장 버전</li>
</ul>
<div class="note">
<p><strong>수집하지 않는 것</strong>: 키보드로 입력한 실제 글자·비밀번호·폼 내용(문자 에코는
<strong>[입력 문자 음성 출력 숨김]</strong>으로 대체), 음성·오디오 녹음과 화면 녹화, 주소창의
검색어(쿼리)와 해시. 모든 기록은 이 컴퓨터의 <strong>data</strong> 폴더에만 저장되며,
<strong>결과 패키지 만들기</strong>로 ZIP을 만들어 직접 보낼 때만 컴퓨터 밖으로 나갑니다.</p>
</div>

<h2>문의</h2>
<p>(담당자 이름과 연락처를 여기에 적어 주세요)</p>
</body>
</html>
"""


def read_manifest_version(path: Path, key: str = "version") -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(key):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("{}에서 {} 값을 찾지 못했습니다.".format(path, key))


def download(url: str, target: Path) -> Path:
    if target.exists() and target.stat().st_size > 0:
        print("캐시 사용: {}".format(target.name))
        return target
    print("다운로드: {}".format(url))
    target.parent.mkdir(parents=True, exist_ok=True)
    with urlopen(url) as response, open(target, "wb") as handle:
        shutil.copyfileobj(response, handle)
    print("  완료 ({:.1f} MB)".format(target.stat().st_size / 1024 / 1024))
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


def build_kit(python_version: str, chrome_version: str) -> Path:
    addon_version = read_manifest_version(ROOT / "nvda-addon" / "manifest.ini")
    extension_version = json.loads(
        (ROOT / "browser-extension" / "manifest.json").read_text(encoding="utf-8")
    )["version"]
    addon_file = ROOT / "dist" / "a11yTaskRecorder-{}.nvda-addon".format(addon_version)
    print("NVDA 애드온 설치 파일 생성 중...")
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
    print("Chrome for Testing 선택 버전: {}".format(cft["version"]))
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

    print("구성 요소 복사 중...")
    shutil.copytree(ROOT / "recorder", app / "recorder")
    shutil.copytree(ROOT / "static", app / "static")
    shutil.copytree(ROOT / "browser-extension", app / "browser-extension")
    shutil.copy2(ROOT / "run.py", app / "run.py")
    shutil.copy2(addon_file, staging / addon_file.name)

    print("Chrome for Testing 압축 해제 중...")
    extract_flat(cft_zip, app / "chrome")
    print("임베디드 Python 압축 해제 중...")
    extract_flat(python_zip, app / "python")

    # 임베디드 Python은 ._pth에 적힌 경로만 sys.path로 쓰므로 app 폴더를 추가한다.
    pth_file = next((app / "python").glob("python*._pth"))
    pth_file.write_text(pth_file.read_text(encoding="utf-8") + "..\n", encoding="utf-8")

    (app / "kit_server.py").write_text(KIT_SERVER_PY, encoding="utf-8")
    (staging / "평가시작.bat").write_text(
        LAUNCHER_BAT.replace("@@EXTENSION_VERSION@@", extension_version),
        encoding="utf-8",
        newline="\r\n",
    )
    (staging / "설치안내.html").write_text(
        GUIDE_HTML.replace("@@ADDON_VERSION@@", addon_version), encoding="utf-8"
    )
    (staging / "KIT-INFO.txt").write_text(
        "\n".join(
            [
                "A11y Task Recorder 평가 키트",
                "생성 시각: {}".format(datetime.now(timezone.utc).isoformat(timespec="seconds")),
                "애드온/서버 버전: {}".format(addon_version),
                "브라우저 확장 버전: {}".format(extension_version),
                "Chrome for Testing: {}".format(cft["version"]),
                "임베디드 Python: {}".format(python_version),
                "",
                "연구 보고 시 브라우저는 'Chrome for Testing {}(동일 버전 Chrome과 같은 엔진)'으로 기재하세요.".format(cft["version"]),
                "",
            ]
        ),
        encoding="utf-8",
    )

    print("ZIP 생성 중...")
    zip_path = ROOT / "dist" / "{}.zip".format(kit_name)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(staging.rglob("*")):
            bundle.write(path, Path(kit_name) / path.relative_to(staging))
    print("완료: {} ({:.1f} MB)".format(zip_path, zip_path.stat().st_size / 1024 / 1024))
    print("검증용 폴더: {}".format(staging))
    return zip_path


def main() -> None:
    parser = argparse.ArgumentParser(description="평가 기관 배포용 키트 생성")
    parser.add_argument("--python-version", default=DEFAULT_PYTHON_VERSION)
    parser.add_argument(
        "--chrome-version",
        default=DEFAULT_CHROME_VERSION,
        help="고정 Chrome for Testing 버전. 최신판은 'latest'를 사용합니다.",
    )
    args = parser.parse_args()
    build_kit(args.python_version, args.chrome_version)


if __name__ == "__main__":
    main()
