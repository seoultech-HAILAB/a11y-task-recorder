#!/usr/bin/env python3
"""Build the installable .nvda-addon archive without external dependencies."""

import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "nvda-addon"
OUTPUT_DIR = ROOT / "dist"
OUTPUT = OUTPUT_DIR / "a11yTaskRecorder-0.2.0.nvda-addon"


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUTPUT, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(SOURCE.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, path.relative_to(SOURCE).as_posix())
    print(OUTPUT)


if __name__ == "__main__":
    main()
