#!/usr/bin/env python3
"""Build the installable .nvda-addon archive without external dependencies."""

import re
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "nvda-addon"
OUTPUT_DIR = ROOT / "dist"


def addon_version():
    manifest = (SOURCE / "manifest.ini").read_text(encoding="utf-8")
    match = re.search(r"^version\s*=\s*(\S+)\s*$", manifest, re.MULTILINE)
    if not match:
        raise RuntimeError("NVDA add-on version is missing from manifest.ini")
    return match.group(1)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output = OUTPUT_DIR / "a11yTaskRecorder-{}.nvda-addon".format(addon_version())
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(SOURCE.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, path.relative_to(SOURCE).as_posix())
    print(output)


if __name__ == "__main__":
    main()
