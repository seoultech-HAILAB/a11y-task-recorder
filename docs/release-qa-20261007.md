# Recorder 0.2.4 candidate QA - 2026-10-07

Disposition: engineering candidate built and checked. Actual Windows/NVDA runtime
validation is NOT RUN on this Linux host and remains a release gate. No commit,
push, GitHub release, public upload or deployment was performed by this QA task.

Branch: `feature/nvda-recorder-release-review-20261007`.
Base commit: `4c9c7f2a41ceed2da5d8cd468cf8b8fc48c6dbef` plus the uncommitted changes
listed below. Existing `.omc/` and `fig*.html` files were left untouched. The tracked
0.2.2 add-on is unchanged; new artifacts use distinct add-on/extension version 0.2.4.

## Fixed and verified

| Area | Finding and candidate behavior |
| --- | --- |
| NVDA session ownership | Queued events previously received the current session ID at send time. Capture and speech episodes now retain the original ID, including after another participant starts. A rejected old event cannot clear the new session cache. |
| Stop boundary | Arbitrary `allow_inactive` is rejected. Stop fixes the capture boundary immediately and drains up to five seconds of explicitly identified pre-stop NVDA/browser traffic. Stop responses and HTTP exports wait for this drain. Events captured after Stop, before Start, or delivered outside the drain are rejected. |
| Crossing speech | Add-on poll finalizes pending speech on session change. Server clips crossing episodes to pre-stop fragments, records `session_stopped`, and retains separately observed end/interruption metadata. This is a capture cutoff, not a claim of audible synthesis completion. |
| Browser batching | DOM changes no longer postpone their flush indefinitely during continuous mutation. Timestamped count batches let the server discard post-stop changes; clipped batches mark the unavailable exact distinct-target count as null. URLs still remove query/hash. |
| Input privacy | Speech redaction now checks the NVDA navigator object as well as focus, covering editable/protected fields read in browse mode. |
| Complete exports | JSON previously capped at 2,000 events, CSV/interactions at 10,000. All export views now include complete histories; a 10,001-event regression passes. Dashboard paginates and restores chronological display. ID cursors no longer skip late-arriving speech. |
| Release builder | Incomplete downloads cannot become cached ZIPs; generated packages exclude Python caches and include the actual Windows diagnostic and checklist. |
| Windows diagnostic | Success now requires real NVDA input, timed speech episodes, and browser events; failed runs clean up the active diagnostic session. It emits no synthetic NVDA events. |
| Mobile dashboard | Single-column grid now allows the timeline to scroll internally without widening the 390px page. |

## Fresh verification

- `python -m unittest discover -v`: 31 passed, including HTTP delivery of final
  old-session speech while a new participant session is active, stop-grace expiry,
  DOM/speech clipping, privacy, export cardinality, API and archive behavior.
- `node --test tests/browser_extension.test.cjs`: 2 passed, covering delayed browser
  session identity, URL stripping, continuous flush and absence of raw DOM target IDs.
- `python -m compileall -q recorder scripts nvda-addon tests`: passed.
- `node --check` for dashboard, background/content/popup extension scripts and the
  browser QA runner: passed. `git diff --check`: passed.
- `PLAYWRIGHT_MODULE=/home/ys/workspace/A11y-Agents/node_modules/playwright/index.js
  node scripts/verify_dashboard.cjs`: passed on Chromium using an existing installed
  Playwright package. No dependency was added. This uses explicitly synthetic NVDA
  API traffic and is NOT Windows/NVDA validation. Checked disconnected-start block,
  session/step/hint/stop, JSON, result ZIP, post-stop rejection, no page errors, and
  no horizontal page overflow at 390px. Evidence: `dist/qa/dashboard-smoke.json`,
  desktop/mobile PNGs, and `synthetic-result-package.zip`.
- `python scripts/build_release_kit.py`: passed. Chrome for Testing 151.0.7922.71,
  embedded Python 3.12.9, add-on/extension 0.2.4.
- `python scripts/verify_release_artifacts.py`: passed. ZIP CRCs, packaged/source
  equality, nested add-on equality, runtime/checklist presence, and absence of data
  or Python caches. Evidence: `dist/qa/release-artifacts.json` and `dist/SHA256SUMS`.
- New workflow validates Linux/Windows Python and JS logic, with a manual Windows
  artifact build. It has not been run on GitHub and does not publish a release.
- No repository linter/typechecker is configured; `ruff` is not installed. No
  Windows PowerShell execution or real extension/NVDA speech-hook execution occurred.

## Candidate files

`dist/A11yTaskRecorder-0.2.4-chrome151.0.7922.71.zip` (212,277,981 bytes)

SHA-256: `6a6f7c068b7a11fec00a3f2668c5a624553f8f7879e8427166006c353505f2b3`

`dist/a11yTaskRecorder-0.2.4.nvda-addon` (7,787 bytes)

SHA-256: `8f12323f5e498b9a96cbf2a881e911bcf30987a123c4f38645db9891aa4d84f2`

## Remaining limits and required Windows check

Follow `docs/windows-release-validation.md`, also included at the kit root. Its
PowerShell diagnostic reads actual NVDA collection on Windows; it does not silently
substitute fixture speech. Confirm startup/update, privacy in real edit controls,
speech cutoff and drain, navigation, reconnect, export and archive contents on each
supported NVDA version before claiming distribution readiness.

There is no pause/resume feature. Completed/abandoned sessions are terminal.
The five-second drain is bounded: queue overflow, process/network loss, unobserved
session starts, and traffic beyond the deadline have no durable raw-event fallback.
The SQLite backup preserves only events the server accepted. The full result ZIP
includes all sessions in that database, even active/draft sessions, despite exporting
separate JSON/CSVs only for finished sessions.

Input echo redaction is not universal anonymization: webpage titles, accessible
names/descriptions and ordinary speech can contain site-reflected input. Use only
synthetic values for privacy QA and evaluate data minimization before collection.
Engineering fixtures, DOM counts and queued speech text are not participant behavior,
audio recordings, or official research scores.

Benchmark deployment URLs are owned by the parent task and were not inserted as
speculative reachable links. No claim about public availability is made here.
