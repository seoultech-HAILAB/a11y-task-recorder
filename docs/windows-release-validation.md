# Windows / NVDA release validation

Status: NOT RUN for the 2026-10-07 source changes. Linux unit tests and ZIP
inspection do not validate NVDA extension points, speech timing, Windows launcher
behavior, or participant usability. This is a release gate before distribution.

## Environment and evidence

Use a clean Windows 10/11 x64 user account, NVDA 2024.1 or a separately recorded
supported version, and the bundled Chrome for Testing. Record Windows build,
NVDA/add-on/Chrome/extension versions, synthesizer, speech rate, source commit or
working-tree identity, kit SHA-256, tester, and date. Keep only synthetic inputs.
Run each supported NVDA version separately; do not infer older-version behavior.

1. Extract the kit to a path with spaces and launch `평가시작.bat`. Confirm the
   server, dedicated Chrome profile, extension, and NVDA heartbeat. Relaunch once.
   Confirm an existing personal Chrome window stays open.
2. With the kit running and no participant session active, double-click
   `검수실행.bat`. It reads actual NVDA input and timed, non-redacted speech plus
   browser records on Example Domain, completes its own diagnostic session, and
   compares JSON/CSV event counts. It never injects NVDA events. Existing active
   participant sessions cause an immediate failure without Chrome/key/session changes.
   Results are written to `diagnostic-results/<timestamp>/diagnostic-result.json`
   on PASS and FAIL, with available exports. If the kit is not writable, it uses
   the displayed TEMP path. Keep that folder. PASS is an engineering collection
   check, not participant validation or complete release-checklist approval.
3. Create a synthetic session for a researcher-confirmed benchmark start URL.
   Record that exact URL and scenario ID in the evidence. Confirm no events are
   added before Start; observe Tab, Shift+Tab, heading navigation, focus changes,
   page transitions, DOM counts, and the NVDA issue marker after Start.
4. Compare speech text with what NVDA actually says. Record complete speech and
   speech interrupted by a key. Check `speech_end_ts`, `interrupted`, and
   `end_reason`; a DOM change is never proof of spoken output.
5. Type synthetic unique strings into plain text, password and contenteditable
   controls, including browse-mode reading, pasting, selecting, and returning to
   the field. Search JSON, both CSVs and the backup DB for those strings. Do not
   use personal data. Input echo should be redacted. Page titles, accessible names,
   descriptions and ordinary webpage speech can contain site-reflected input;
   this tool does not guarantee arbitrary-page personal-data anonymization.
6. Stop mid-speech, then immediately create/start a different synthetic participant
   session. Keep navigating for at least five seconds. Check that completed-session
   events captured after Stop are rejected and old queued speech is not assigned
   to the new participant. Stop/export waits for a five-second delivery window:
   pre-stop events may arrive in that window under their original session ID.
   A crossing speech episode is clipped to Stop, with observed end/interruption
   metadata retained separately; DOM batches retain only pre-stop counts. After
   the window, confirm event counts stay fixed. No pause/resume feature exists: `abandoned` and
   `completed` are terminal, and a new round requires a new session.
7. Disconnect/restart the server, verify disconnected status, reconnect and check
   fresh events. This recorder does not provide durable offline buffering; document
   any collection gap and repeat the affected engineering scenario. Queue overflow,
   network loss and arrivals beyond the five-second drain have no durable raw-event
   fallback; the SQLite backup can only preserve events accepted by the server.
8. Export a completed session and result package. Match event counts between
   JSON, CSV and DB; match action count to interaction CSV. Check step/hint/issue
   references, UTF-8 Korean text, and spreadsheet import. The ZIP's SQLite backup
   includes ALL sessions, including active/draft sessions; review it before sharing.
9. Record PASS/FAIL per step with artifact paths and remaining limitations. A
   connected badge or synthetic API event alone cannot pass the NVDA speech gate.

No results from this checklist are human-subject efficacy or official study scores.
