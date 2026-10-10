"""Windows-only failure-path tests; these never validate real NVDA speech."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == "nt", "Requires Windows PowerShell; not NVDA validation")
class WindowsDiagnosticFailureTests(unittest.TestCase):
    def run_failure(self, active):
        source = Path(__file__).resolve().parents[1] / "scripts/verify_live_pipeline.ps1"
        with tempfile.TemporaryDirectory(prefix="recorder kit with spaces ") as directory:
            root = Path(directory)
            chrome = root / "app/chrome/chrome.exe"
            chrome.parent.mkdir(parents=True)
            chrome.write_bytes(b"must never execute")
            script = root / "verify_live_pipeline.ps1"
            script.write_bytes(source.read_bytes())
            harness = root / "failure_harness.ps1"
            active_value = "@{ id = 'participant-existing' }" if active else "$null"
            harness.write_text("""
param([string]$Kit)
function Invoke-RestMethod {
    param($Uri, $Method, $TimeoutSec, $Body, $ContentType)
    if ($Method -and $Method -ne 'GET') { throw 'UNEXPECTED WRITE' }
    if ($Uri.EndsWith('/active-session')) { return @{session = ACTIVE_VALUE} }
    if ($Uri.EndsWith('/health')) { return @{nvda_connected = $false} }
    throw 'UNEXPECTED API'
}
function Start-Process { throw 'UNEXPECTED CHROME LAUNCH' }
& (Join-Path $Kit 'verify_live_pipeline.ps1') -KitPath $Kit
# `exit` inside a script run with `&` only sets $LASTEXITCODE; a -File host
# that ends normally still exits 0. Forward the diagnostic's own exit code,
# which the kit's .bat launcher receives directly from `powershell -File`.
exit $LASTEXITCODE
""".replace("ACTIVE_VALUE", active_value), encoding="utf-8")
            run = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                  "-File", str(harness), "-Kit", str(root)],
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(1, run.returncode, run.stdout + run.stderr)
            reports = list((root / "diagnostic-results").glob("*/diagnostic-result.json"))
            self.assertEqual(1, len(reports))
            result = json.loads(reports[0].read_text(encoding="utf-8-sig"))
            self.assertEqual("FAIL", result["status"])
            self.assertEqual("NOT_RUN", result["actual_nvda_validation"])
            self.assertIsNone(result["session_id"])
            self.assertNotIn("UNEXPECTED", result["error"])
            return result

    def test_active_participant_is_untouched_and_failure_is_persisted(self):
        self.assertIn("active session already exists", self.run_failure(True)["error"])

    def test_disconnected_nvda_is_not_reported_as_pass(self):
        self.assertIn("disconnected", self.run_failure(False)["error"])
