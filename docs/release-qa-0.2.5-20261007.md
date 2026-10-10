# Recorder 0.2.5 diagnostic candidate QA

Status: Linux engineering checks and local packaging passed. Actual Windows
PowerShell execution and actual NVDA collection are NOT RUN. This candidate adds
the Windows verification procedure; it does not claim that procedure has passed.
No commit, push, public upload, deployment, or GitHub release was performed here.

## Changes since 0.2.4

- Add-on and browser extension versions are both 0.2.5. The existing 0.2.4 kit and
  add-on remain intact with their previously reported SHA-256 hashes.
- Kit root now contains `검수실행.bat`. Run `평가시작.bat` first when no participant
  session is active, then double-click `검수실행.bat` for the actual diagnostic.
- Native Chrome extension/profile arguments are explicitly quoted before
  `Start-Process`, preserving paths containing spaces. The batch launcher quotes
  its script/kit paths and disables delayed expansion.
- Before creating a diagnostic session or launching Chrome, the diagnostic checks
  for an existing active session and fails without changing it. It checks ownership
  again before browser activation and every sent key. Cleanup addresses only its
  own diagnostic session; it never restarts NVDA or stops another session.
- The diagnostic does not post synthetic NVDA events. It requires actual NVDA
  input, timed non-redacted speech episodes, browser records, completed status,
  and matching JSON/event CSV/interaction CSV row counts.
- PASS and FAIL both write `diagnostic-result.json`, including status, timestamps,
  evidence kind, actual-validation state, session ID, counts, and error/cleanup
  details. Available session exports are retained even on failure.

Default result location in the extracted kit:

`diagnostic-results/<yyyyMMdd-HHmmss-fff>/diagnostic-result.json`

The same folder contains `session.json`, `events.csv`, and `interactions.csv` when
available. If the kit is unwritable, the fallback is
`%TEMP%\a11y-recorder-verification-<timestamp>\diagnostic-result.json` and the console
prints that exact path. A missing server/NVDA or existing participant session must
produce FAIL/NOT_RUN, never actual NVDA PASS. The batch exit code is preserved.

## Verification on this Linux host

- `python -m unittest discover -v`: 35 discovered, 33 passed, 2 Windows-only tests
  skipped. The new Linux checks build a small fixture kit under a path containing
  spaces and verify launcher/script/checklist inclusion, versions, CRLF and quoting.
  Native Windows argument handling is still not executed here.
- Windows-only tests are provided for existing-participant protection and
  disconnected-NVDA failure persistence. They use API stubs to test failure control
  flow, never to establish actual speech validity; they have not run on this host.
- `node --test tests/browser_extension.test.cjs`: 2 passed.
- Python compilation and `git diff --check`: passed.
- Full Windows kit build and archive validation: passed. CRC, source/archive byte
  equality, actual diagnostic script inclusion, one-click launcher quoting/CRLF,
  runtime presence and absence of participant data/Python caches were checked.
- No dependencies were added. No desktop credentials were requested or stored.

## Candidate artifacts

`dist/A11yTaskRecorder-0.2.5-chrome151.0.7922.71.zip`

212,279,099 bytes; SHA-256:
`faaa48641841b89cd8f48496934f6fb83fe84074b45dfef66c48b08c6b6dcc29`

`dist/a11yTaskRecorder-0.2.5.nvda-addon`

7,787 bytes; SHA-256:
`ae8ecef1dc8e2ad3e0cffb5f37022461f62505b4823e95c26853be8946cc9f22`

Machine-readable packaging evidence: `dist/qa/release-artifacts.json`.
Checksums: `dist/SHA256SUMS`.

The remaining Windows release gate is `docs/windows-release-validation.md`, also
included at the kit root. The one-click diagnostic covers engineering collection
and export; the rest of the checklist still covers speech fidelity, privacy,
session boundaries, installation, and reconnect behavior. Public hosted availability
is managed separately by the parent deployment task and is not claimed here.
