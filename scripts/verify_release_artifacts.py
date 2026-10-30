"""Inspect generated Windows archives without claiming Windows/NVDA execution."""
import hashlib
import json
import zipfile
from pathlib import Path

from build_release_kit import DEFAULT_CHROME_VERSION, read_manifest_version


def main():
    root = Path(__file__).resolve().parents[1]
    version = read_manifest_version(root / "nvda-addon/manifest.ini")
    name = "A11yTaskRecorder-{}-chrome{}".format(version, DEFAULT_CHROME_VERSION)
    kit = root / "dist" / (name + ".zip")
    addon = root / "dist" / "a11yTaskRecorder-{}.nvda-addon".format(version)
    with zipfile.ZipFile(kit) as bundle:
        assert bundle.testzip() is None, "ZIP CRC check failed"
        names = bundle.namelist()
        assert not any("__pycache__" in entry or entry.endswith(".pyc") for entry in names)
        assert not any("/data/" in entry for entry in names), "Participant data in kit"
        assert bundle.read(name + "/" + addon.name) == addon.read_bytes()
        for folder in ("recorder", "browser-extension", "static"):
            for source in (root / folder).rglob("*"):
                if source.is_file() and "__pycache__" not in source.parts:
                    entry = name + "/app/" + source.relative_to(root).as_posix()
                    assert bundle.read(entry) == source.read_bytes(), entry
        assert name + "/app/chrome/chrome.exe" in names
        assert name + "/app/python/python.exe" in names
        # Installation check tools sit in _연구진용/ so a participant does not run them by mistake.
        research = name + "/_연구진용"
        assert research + "/verify_live_pipeline.ps1" in names
        assert bundle.read(research + "/verify_live_pipeline.ps1") == (root / "scripts/verify_live_pipeline.ps1").read_bytes()
        assert research + "/검수실행.bat" in names
        launcher = bundle.read(research + "/검수실행.bat")
        assert b'"%~dp0verify_live_pipeline.ps1"' in launcher
        assert b'"%RECORDER_DIAGNOSTIC_KIT%"' in launcher
        assert b'\r\n' in launcher
        assert research + "/windows-release-validation.md" in names
    with zipfile.ZipFile(addon) as bundle:
        assert bundle.testzip() is None
        for source in (root / "nvda-addon").rglob("*"):
            if source.is_file() and "__pycache__" not in source.parts:
                assert bundle.read(source.relative_to(root / "nvda-addon").as_posix()) == source.read_bytes()
    artifacts = []
    for artifact in (kit, addon):
        digest = hashlib.sha256()
        with artifact.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        artifacts.append({"file": artifact.name, "bytes": artifact.stat().st_size,
                          "sha256": digest.hexdigest()})
    result = {"status": "PASS", "version": version, "actual_windows_nvda": "NOT_RUN",
              "artifacts": artifacts, "checks": ["ZIP CRC", "source matches packaged code",
              "add-on payload matches", "Windows runtimes present", "no data or Python caches"]}
    output = root / "dist/qa"
    output.mkdir(parents=True, exist_ok=True)
    (output / "release-artifacts.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (root / "dist/SHA256SUMS").write_text("".join(
        "{}  {}\n".format(item["sha256"], item["file"]) for item in artifacts), encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
