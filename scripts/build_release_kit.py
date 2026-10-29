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
rem Comments here stay ASCII: under chcp 65001 cmd ran a piece of a Korean comment as a command
rem ("... is not recognized as an internal or external command", Windows 10/29).
rem Each run sets up the add-on, NVDA and the kit Chrome.
rem The parentheses of %ProgramFiles(x86)% break if blocks, so the paths are set outside them.
set "NVDA64=%ProgramFiles%\\NVDA\\nvda.exe"
set "NVDA86=%ProgramFiles(x86)%\\NVDA\\nvda.exe"
set "NVDALNK=%ProgramData%\\Microsoft\\Windows\\Start Menu\\Programs\\NVDA\\NVDA.lnk"
set "ADDON_DIR=%APPDATA%\\nvda\\addons\\a11yTaskRecorder"
rem A kit Chrome left from an earlier run can miss a restarted NVDA's accessibility hooks.
rem Only the kit's own Chrome is closed; the participant's Chrome is left alone.
powershell -NoProfile -Command "$root=[IO.Path]::GetFullPath($env:KIT+'app\\chrome'); Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'chrome.exe' -and $_.ExecutablePath -and $_.ExecutablePath.StartsWith($root,[StringComparison]::OrdinalIgnoreCase) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Milliseconds 800"
rem 0.5.9: a running NVDA is left alone when the same add-on is installed. Only a new add-on
rem restarts it, with its own program (and its arguments when Windows lets us read them).
rem Restarting through the shortcut brought up an NVDA with other settings (Windows 10/29).
echo NVDA 기록 애드온을 확인하는 중입니다...
"%KIT%app\\python\\python.exe" "%KIT%app\\kit_addon.py"
set "ADDON_RESULT=%errorlevel%"
if "%ADDON_RESULT%"=="1" echo 경고: NVDA 애드온 자동 설치에 실패했습니다. 설치안내.html을 확인해 주세요.
tasklist /FI "IMAGENAME eq nvda.exe" /NH | find /I "nvda.exe" >nul
if errorlevel 1 goto start_nvda
if not "%ADDON_RESULT%"=="10" goto nvda_ready
powershell -NoProfile -ExecutionPolicy Bypass -File "%KIT%app\\kit_nvda_restart.ps1"
if errorlevel 1 goto start_nvda
goto wait_nvda
:start_nvda
if exist "%NVDALNK%" (
  start "" "%NVDALNK%"
) else if exist "%NVDA64%" (
  start "" "%NVDA64%"
) else if exist "%NVDA86%" (
  start "" "%NVDA86%"
) else (
  echo NVDA가 설치되어 있지 않습니다. 설치안내.html의 'NVDA가 설치되어 있지 않아요'를 확인해 주세요.
  goto nvda_ready
)
:wait_nvda
echo NVDA와 기록 애드온이 켜질 때까지 기다리는 중입니다...
powershell -NoProfile -Command "Start-Sleep -Seconds 10"
:nvda_ready
powershell -NoProfile -Command "try { if((Invoke-RestMethod 'http://127.0.0.1:8765/api/health' -TimeoutSec 1).ok){ exit 0 } } catch {}; exit 1"
if errorlevel 1 (
  rem 0.5.9: minimized; it closes by itself 3 minutes after the recorder screen is closed.
  start "A11y Task Recorder 서버" /min "%KIT%app\\python\\python.exe" "%KIT%app\\kit_server.py" --db "%KIT%data\\recorder.sqlite3"
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
start "" "%KIT%app\\chrome\\chrome.exe" --load-extension="%KIT%app\\browser-extension" --user-data-dir="%KIT%app\\chrome-profile-@@EXTENSION_VERSION@@" --no-first-run --no-default-browser-check --start-maximized --app="http://127.0.0.1:8765"
endlocal
"""

# Kit helpers run with the kit's Python (0.5.9): server wrapper, add-on sync, NVDA restart.
KIT_HELPERS = ("kit_server.py", "kit_addon.py", "kit_nvda_restart.ps1")

# 0.5.9: the install guide is written for a participant who reads it with NVDA, and the same
# file is handed out as a link before the kit is unpacked (docs/kit-install-guide.html).
GUIDE_FILE = ROOT / "docs" / "kit-install-guide.html"


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


def check_catalog(path: Path) -> None:
    """Stop the build when the recorder could not load the kit's assignment table.

    10/28: a hand-built catalog without the source hashes shipped, and the kit showed no tester numbers.
    The message names only the error type and a missing key, never catalog values (they hold site addresses).
    """
    repo = str(Path(__file__).resolve().parent.parent)
    if repo not in sys.path:
        sys.path.insert(0, repo)
    from recorder.study import load_catalog
    try:
        load_catalog(path)
    except (OSError, ValueError, KeyError, TypeError) as error:
        detail = "빠진 항목 {}".format(error) if isinstance(error, KeyError) else type(error).__name__
        raise SystemExit("배정표를 기록기가 읽을 수 없습니다({}). 이대로면 테스터 번호가 나오지 않습니다.".format(detail))


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
              site_origin_file: Optional[Path] = None, catalog_file: Optional[Path] = None) -> Path:
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
    if catalog_file:
        # A kit with its own assignment table (e.g. a peer check) must not overwrite the main kit.
        kit_name += "-" + catalog_file.stem.split(".")[0]
    staging = BUILD_DIR / kit_name
    if staging.exists():
        remove_tree(staging)
    app = staging / "app"
    app.mkdir(parents=True)

    log("구성 요소 복사 중...")
    # .omc is the coding agent's local state, never part of the kit.
    skip = shutil.ignore_patterns("__pycache__", "*.pyc", ".omc")
    shutil.copytree(ROOT / "recorder", app / "recorder", ignore=skip)
    shutil.copytree(ROOT / "static", app / "static", ignore=skip)
    kit_catalog = app / "recorder" / "study_catalog.json"
    if catalog_file:
        shutil.copy2(catalog_file, kit_catalog)
        log("배정표: {}".format(catalog_file.name))
    if site_origin:
        kit_catalog.write_text(kit_catalog.read_text(encoding="utf-8").replace(PUBLIC_ORIGIN, site_origin),
                               encoding="utf-8", newline="\n")
        log("사본 주소: 키트 배정표에 넣음")
    elif PUBLIC_ORIGIN in kit_catalog.read_text(encoding="utf-8"):
        log("사본 주소: 넣지 않음 (이 키트로는 사이트에 접속할 수 없음, 배포 금지)")
    else:
        log("사본 주소: 배정표에 이미 있음")
    check_catalog(kit_catalog)
    shutil.copytree(ROOT / "browser-extension", app / "browser-extension", ignore=skip)
    shutil.copy2(ROOT / "run.py", app / "run.py")
    shutil.copy2(addon_file, staging / addon_file.name)
    research = staging / "_연구진용"  # 설치 점검 도구: 담당자가 실수로 실행하지 않게 최상위에서 뺀다
    research.mkdir()
    shutil.copy2(ROOT / "scripts" / "verify_live_pipeline.ps1", research / "verify_live_pipeline.ps1")
    shutil.copy2(ROOT / "docs" / "windows-release-validation.md", research / "windows-release-validation.md")
    # The session notes are for researchers; participants follow the recorder screen (0.5.9).
    shutil.copy2(ROOT / "docs" / "kit-session-checklist.md", research / "진행안내.md")

    log("Chrome for Testing 압축 해제 중...")
    extract_flat(cft_zip, app / "chrome")
    log("임베디드 Python 압축 해제 중...")
    extract_flat(python_zip, app / "python")

    # 임베디드 Python은 ._pth에 적힌 경로만 sys.path로 쓰므로 app 폴더를 추가한다.
    pth_file = next((app / "python").glob("python*._pth"))
    pth_file.write_text(pth_file.read_text(encoding="utf-8") + "..\n", encoding="utf-8")

    for helper in KIT_HELPERS:
        shutil.copy2(ROOT / "scripts" / "kit" / helper, app / helper)
    if access_code:
        # recorder/upload.py reads app/study_access.txt for the iframe login and the upload bearer.
        (app / "study_access.txt").write_text(access_code + "\n", encoding="utf-8", newline="\n")
        log("사이트 접속 코드: 키트에 내장함")
    else:
        # Sites open without a code since 10/28; the code is then only the upload bearer.
        log("사이트 접속 코드: 내장하지 않음 (서버 업로드 없음, 결과는 결과 패키지 ZIP으로 전달)")
    (staging / "평가시작.bat").write_text(
        LAUNCHER_BAT.replace("@@EXTENSION_VERSION@@", extension_version),
        encoding="utf-8",
        newline="\r\n",
    )
    (staging / "_연구진용" / "검수실행.bat").write_text(DIAGNOSTIC_BAT, encoding="ascii", newline="\r\n")
    (staging / "설치안내.html").write_text(
        GUIDE_FILE.read_text(encoding="utf-8").replace("@@ADDON_VERSION@@", addon_version), encoding="utf-8"
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
                "배정표: {}".format(catalog_file.name if catalog_file else "기본(recorder/study_catalog.json)"),
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
    parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="기본 배정표(recorder/study_catalog.json) 대신 넣을 배정표. 파일 이름 첫 부분이 키트 이름 끝에 붙습니다.",
    )
    args = parser.parse_args()
    build_kit(args.python_version, args.chrome_version, args.access_code_file, args.site_origin_file,
              args.catalog)


if __name__ == "__main__":
    main()
