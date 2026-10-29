"""키트 실행 때 NVDA 기록 애드온을 맞춘다 (0.5.9).

같은 판이 이미 설치되어 있으면 그대로 두어, 켜져 있는 NVDA를 다시 시작하지 않게 한다.
종료 코드: 0 그대로 둠, 10 새로 설치함(NVDA 재시작 필요), 1 실패.
"""
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

NAME = "a11yTaskRecorder"
INSTALLED = 10


def manifest_version(text: str) -> str:
    match = re.search(r'^\s*version\s*=\s*"?([^"\s]+)"?\s*$', text, flags=re.M)
    return match.group(1) if match else ""


def sync(kit: Path, appdata: Path) -> int:
    packages = sorted(kit.glob(NAME + "-*.nvda-addon"))
    if not packages:
        print("경고: 키트에서 NVDA 애드온 파일을 찾지 못했습니다.")
        return 1
    with zipfile.ZipFile(packages[-1]) as bundle:
        wanted = manifest_version(bundle.read("manifest.ini").decode("utf-8", "replace"))
        target = appdata / "nvda" / "addons" / NAME
        manifest = target / "manifest.ini"
        installed = manifest_version(manifest.read_text(encoding="utf-8", errors="replace")) if manifest.is_file() else ""
        if wanted and installed == wanted and (target / "globalPlugins").is_dir():
            print("NVDA 기록 애드온이 이미 준비되어 있습니다.")
            return 0
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        bundle.extractall(target)
    print("NVDA 기록 애드온을 설치했습니다.")
    return INSTALLED


def main() -> int:
    try:
        return sync(Path(os.environ.get("KIT") or Path(__file__).resolve().parents[1]), Path(os.environ["APPDATA"]))
    except Exception as error:  # the launcher reports it and goes on
        print("경고: NVDA 애드온을 준비하지 못했습니다: {}".format(error))
        return 1


if __name__ == "__main__":
    sys.exit(main())
