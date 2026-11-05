# Browser regression tests

These checks use a real Chromium browser against an isolated localhost app with
scratch storage, mocked OpenAI-compatible and OpenViking transports, disabled channels and
scratch jobs. They never read `.env` or operator configuration. Node/Playwright
are QA tools only; the application still has no Node build/runtime requirement.

Install Playwright outside the checkout:

```sh
npm install --prefix /private/tmp/solakit-browser-qa playwright
uv run python tests/browser/server.py
```

In a second terminal:

```sh
NODE_PATH=/private/tmp/solakit-browser-qa/node_modules node tests/browser/check.cjs 12
```

The default executable is macOS Brave. Override `SOLA_BROWSER_EXECUTABLE` to use
another installed Chromium browser. Set `SOLA_BROWSER_ARTIFACTS` to change the
screenshot directory (default `/private/tmp/solakit-browser-artifacts`). Review
screenshots in addition to assertions. Stop the scratch server after validation;
it leaves its isolated `solakit-browser-*` temporary data for inspection.

Phase 12: light/dark × 1440×1000 / 390×844, browser timezone Europe/London versus
America/Denver schedules, pause/resume/run-now feedback, disabled controls and
channel state, table scrolling, page overflow and browser script errors.

Phase 13: run `check.cjs 13` with the same setup for archive confirmation,
numbered history navigation, preserved messages, read-only composer absence and
return to the active conversation, across the same theme/viewport matrix.

Phase 10: run `check.cjs 10` for session creation/navigation, Enter/Shift+Enter,
synthetic browser IME composition/key sequences, keyboard focus, busy duplicate
submission guards and cancellation recovery, with the same theme/viewport matrix.

Phase 11: run `check.cjs 11` for fixed-time prompt API parity, schema/tool/prompt
expansion, narrow-screen evidence and actual UI cancellation feedback. The mock
model calls the built-in read-only runtime.status tool; external integrations
remain disabled.

Phase 21: run `check.cjs 21` for explicit compaction confirmation, ephemeral/busy
errors and expanded summary ordering, across light/dark desktop/mobile. The
OpenViking transport is mocked inside this QA process, including startup/commit
requests, and cannot access a live memory server.

Phase 22: run `check.cjs 22` for delayed partial Markdown/tool SSE, internal code
and table overflow, link navigation, scroll/focus, reload, observer connection
failures and actual mid-body SSE reset, running/queued cancellation, model stream
interruption, sign-out and server-side expiry. The fixture exposes QA-only
disconnect/expiry controls; these never exist in the production application.
The four theme/viewport cases take a few minutes. Accepted runs continue through
observer failures and authentication changes; no model POST is replayed.

Job management: run `check.cjs jobs` for create/error preservation, editing,
manual triggering, pause, delete confirmation and actual deletion across
light/dark desktop/mobile. Checks include page overflow, browser errors and
form/list screenshots. Jobs use the same scratch mock-model server above.

One-time scheduling: run `check.cjs once` for the One time picker, timezone
conversion, manual consumption, disabled repeat controls, task-only edits and
explicit date-based rearming, across light/dark desktop/mobile. Form/completed-row
screenshots, browser errors and overflow are checked with scratch jobs only.
