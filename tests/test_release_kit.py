import io
import json
import re
import shlex
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import build_release_kit


class ReleaseDownloadTests(unittest.TestCase):
    def test_launcher_rejects_old_server_before_restarting_tools(self):
        launcher = build_release_kit.LAUNCHER_BAT
        self.assertIn('recorder_version', launcher)
        self.assertIn('@@EXTENSION_VERSION@@', launcher)
        self.assertLess(launcher.index('recorder_version'), launcher.index('Get-CimInstance'))
        self.assertIn('if errorlevel 3 goto resume_dashboard', launcher)
        self.assertLess(launcher.index('Get-CimInstance'), launcher.index(':resume_dashboard'))

    def test_failed_download_never_becomes_cached_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "runtime.zip"
            with patch.object(build_release_kit, "urlopen", return_value=io.BytesIO(b"partial")):
                with self.assertRaises(ValueError):
                    build_release_kit.download("https://example.invalid/runtime.zip", target)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".zip.part").exists())

    def test_invalid_existing_cache_is_replaced(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("runtime.exe", b"fixture")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "runtime.zip"
            target.write_bytes(b"interrupted download")
            with patch.object(build_release_kit, "urlopen", return_value=io.BytesIO(archive.getvalue())):
                build_release_kit.download("https://example.invalid/runtime.zip", target)
            self.assertTrue(zipfile.is_zipfile(target))

    def test_korean_progress_does_not_abort_on_non_korean_stdout(self):
        # GitHub's windows-latest pipes stdout as cp1252, which cannot encode Hangul.
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("runtime.exe", b"fixture")
        stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        with tempfile.TemporaryDirectory() as directory, patch("sys.stdout", stdout), patch.object(
            build_release_kit, "urlopen", return_value=io.BytesIO(archive.getvalue())
        ):
            build_release_kit.download("https://example.invalid/runtime.zip", Path(directory) / "runtime.zip")
            stdout.flush()
            output = stdout.buffer.getvalue().decode("cp1252")
        self.assertIn("\\ub2e4\\uc6b4\\ub85c\\ub4dc: https://example.invalid/runtime.zip", output)

    def test_kit_packages_one_click_diagnostic_and_aligned_versions(self):
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="recorder kit with spaces ") as directory:
            root = Path(directory)
            for folder in ("recorder", "static", "browser-extension", "nvda-addon", "scripts", "docs"):
                shutil.copytree(source_root / folder, root / folder,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            shutil.copy2(source_root / "run.py", root / "run.py")
            chrome = root / "chrome.zip"
            python = root / "python.zip"
            with zipfile.ZipFile(chrome, "w") as bundle:
                bundle.writestr("chrome-win64/chrome.exe", b"packaging fixture, not executable")
            with zipfile.ZipFile(python, "w") as bundle:
                bundle.writestr("python.exe", b"packaging fixture, not executable")
                bundle.writestr("python312._pth", "python312.zip\n.\n")
            with patch.object(build_release_kit, "ROOT", root), patch.object(
                build_release_kit, "BUILD_DIR", root / "dist/kit-build"
            ), patch.object(build_release_kit, "download", side_effect=[chrome, python]):
                artifact = build_release_kit.build_kit("3.12.9", "151.0.7922.71")
            version = build_release_kit.read_manifest_version(root / "nvda-addon/manifest.ini")
            self.assertEqual(version, json.loads((root / "browser-extension/manifest.json").read_text(encoding="utf-8"))["version"])
            # 평가시작.bat compares /api/health recorder_version with the extension version.
            init = (root / "recorder/__init__.py").read_text(encoding="utf-8")
            self.assertIn('__version__ = "{}"'.format(version), init)
            prefix = artifact.stem + "/"
            with zipfile.ZipFile(artifact) as bundle:
                launcher = bundle.read(prefix + "_연구진용/검수실행.bat").decode("ascii")
                self.assertIn('"%~dp0verify_live_pipeline.ps1"', launcher)
                self.assertIn('-KitPath "%RECORDER_DIAGNOSTIC_KIT%"', launcher)
                self.assertIn('exit /b %DIAGNOSTIC_EXIT%', launcher)
                self.assertIn("\r\n", launcher)
                self.assertEqual((root / "scripts/verify_live_pipeline.ps1").read_bytes(),
                                 bundle.read(prefix + "_연구진용/verify_live_pipeline.ps1"))
                self.assertIn(prefix + "_연구진용/windows-release-validation.md", bundle.namelist())
                self.assertNotIn(prefix + "app/study_access.txt", bundle.namelist())
            # --access-code-file embeds the code (fake value, 16+ characters) as app/study_access.txt.
            code_file = root / "access.json"
            code_file.write_text(json.dumps({"code": "fake-kit-code-for-tests-0001", "note": "x"}), encoding="utf-8")
            with patch.object(build_release_kit, "ROOT", root), patch.object(
                build_release_kit, "BUILD_DIR", root / "dist/kit-build"
            ), patch.object(build_release_kit, "download", side_effect=[chrome, python]), \
                    patch("sys.stdout", io.StringIO()) as printed:
                artifact = build_release_kit.build_kit("3.12.9", "151.0.7922.71", code_file)
            self.assertNotIn("fake-kit-code-for-tests-0001", printed.getvalue())
            with zipfile.ZipFile(artifact) as bundle:
                self.assertEqual("fake-kit-code-for-tests-0001\n",
                                 bundle.read(prefix + "app/study_access.txt").decode("utf-8"))
                self.assertNotIn("fake-kit-code-for-tests-0001", bundle.read(prefix + "_연구진용/KIT-INFO.txt").decode("utf-8"))

    def test_access_code_file_accepts_bare_or_json_and_rejects_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "code.txt"
            path.write_text("\ufeff  fake-bare-code-for-tests-01 \n", encoding="utf-8")
            self.assertEqual("fake-bare-code-for-tests-01", build_release_kit.read_access_code(path))
            path.write_text('{"code": ""}', encoding="utf-8")
            with self.assertRaises(SystemExit):
                build_release_kit.read_access_code(path)
            with self.assertRaises(SystemExit):
                build_release_kit.read_access_code(Path(directory) / "missing.txt")

    def test_chrome_argument_templates_preserve_paths_with_spaces(self):
        script = (Path(__file__).resolve().parents[1] / "scripts/verify_live_pipeline.ps1").read_text(encoding="utf-8")
        extension = re.search(r"\('([^']+)' -f \$extension\)", script).group(1)
        profile = re.search(r"\('([^']+)' -f \$profile\)", script).group(1)
        command = " ".join((extension.format(r"C:\Lab Kit\app\browser-extension"),
                            profile.format(r"C:\Lab Kit\app\chrome-profile-0.2.5")))
        # These concrete native arguments have no embedded quotes/trailing slashes;
        # non-POSIX tokenization verifies the spaces stay inside two quoted tokens.
        tokens = shlex.split(command, posix=False)
        self.assertEqual(2, len(tokens))
        self.assertEqual('"--load-extension=C:\\Lab Kit\\app\\browser-extension"', tokens[0])
        self.assertEqual('"--user-data-dir=C:\\Lab Kit\\app\\chrome-profile-0.2.5"', tokens[1])
