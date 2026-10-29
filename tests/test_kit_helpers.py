"""0.5.9 kit helpers: the add-on is installed only when it changed, and the server stops on its own."""
import importlib.util
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "kit" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


kit_addon = load("kit_addon")
sys.path.insert(0, str(ROOT))
kit_server = load("kit_server")


class AddonSyncTests(unittest.TestCase):
    def package(self, kit, version):
        for old in kit.glob("a11yTaskRecorder-*.nvda-addon"):
            old.unlink()
        with zipfile.ZipFile(kit / "a11yTaskRecorder-{}.nvda-addon".format(version), "w") as bundle:
            bundle.writestr("manifest.ini", 'name = a11yTaskRecorder\nversion = {}\n'.format(version))
            bundle.writestr("globalPlugins/a11yTaskRecorder.py", "# plugin\n")

    def test_installs_once_then_leaves_a_running_nvda_alone(self):
        with tempfile.TemporaryDirectory() as temp:
            kit, appdata = Path(temp, "kit"), Path(temp, "appdata")
            kit.mkdir()
            self.package(kit, "0.5.9")
            self.assertEqual(kit_addon.INSTALLED, kit_addon.sync(kit, appdata))
            self.assertEqual(0, kit_addon.sync(kit, appdata), "same version: no NVDA restart")
            self.package(kit, "0.6.0")
            self.assertEqual(kit_addon.INSTALLED, kit_addon.sync(kit, appdata), "a new kit installs again")
            installed = (appdata / "nvda/addons/a11yTaskRecorder/manifest.ini").read_text()
            self.assertEqual("0.6.0", kit_addon.manifest_version(installed))

    def test_a_missing_package_is_a_failure_the_launcher_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(1, kit_addon.sync(Path(temp), Path(temp, "appdata")))

    def test_the_kit_manifest_version_is_read(self):
        text = (ROOT / "nvda-addon" / "manifest.ini").read_text(encoding="utf-8")
        self.assertRegex(kit_addon.manifest_version(text), r"^\d+\.\d+\.\d+$")


class ServerIdleTests(unittest.TestCase):
    def setUp(self):
        kit_server._last_dashboard["at"] = None

    def request(self, **headers):
        kit_server.note_request(SimpleNamespace(headers=headers))

    def test_only_the_recorder_screen_keeps_the_server_running(self):
        self.request(**{"X-A11y-Client": "nvda-addon"})
        self.request(**{"User-Agent": "Python-urllib/3.11"})
        self.request(**{"User-Agent": "Mozilla/5.0 (Windows NT; Windows NT 10.0; ko-KR) WindowsPowerShell/5.1.26100"})
        self.assertIsNone(kit_server._last_dashboard["at"], "the add-on heartbeat does not count")
        self.assertFalse(kit_server.idle(time.monotonic() + 10_000), "never stops before the screen opened")
        self.request(**{"User-Agent": "Mozilla/5.0 Chrome/151"})
        now = time.monotonic()
        self.assertFalse(kit_server.idle(now + kit_server.IDLE_SECONDS - 1))
        self.assertTrue(kit_server.idle(now + kit_server.IDLE_SECONDS + 1))


if __name__ == "__main__":
    unittest.main()


class LauncherTests(unittest.TestCase):
    def test_a_running_nvda_is_restarted_only_for_a_new_add_on(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import build_release_kit
        launcher = build_release_kit.LAUNCHER_BAT
        check, restart, shortcut = (launcher.index(text) for text in (
            'tasklist /FI "IMAGENAME eq nvda.exe"', "kit_nvda_restart.ps1", 'start "" "%NVDALNK%"'))
        self.assertLess(check, restart)
        self.assertLess(restart, launcher.index(":start_nvda"), "the shortcut is only for an NVDA that is off")
        self.assertLess(launcher.index(":start_nvda"), shortcut)
        self.assertIn('if not "%ADDON_RESULT%"=="10" goto nvda_ready', launcher)
        self.assertIn('start "A11y Task Recorder 서버" /min', launcher)
        comments = [line for line in launcher.splitlines() if line.lstrip().lower().startswith("rem")]
        self.assertTrue(comments)
        for line in comments:
            self.assertTrue(line.isascii(), "cmd misreads non-ASCII comments under chcp 65001: " + line)
        for helper in build_release_kit.KIT_HELPERS:
            self.assertTrue((ROOT / "scripts" / "kit" / helper).is_file(), helper)
