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
channel state, stacked mobile job/channel records with reachable actions, page overflow and
browser script errors.

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

Vision thumbnails: run `check.cjs vision` to verify authenticated images load in
chat and archived history, fit desktop/mobile in both themes, and become
inaccessible after logout. The scratch server generates its own image fixture;
these browser checks do not connect a Discord gateway or real vision model.

Session lifecycle: run `check.cjs lifecycle` for a 55-session imported fixture,
pagination, inactivity preview/deselection, bulk archive, retained search/history,
read-only archived chat, restoration and batch undo. It checks theme/viewport
overflow and screenshots with scratch data only.

Session search: run `check.cjs search` for name/ID pagination, archived sessions,
full-text snippets and links to retained numbered archives, in both themes at
desktop/mobile sizes. Uses the scratch imported-history fixture only.

Session rename: run `check.cjs rename` for renaming archived chats, preserved
history and searchable escaped names across light/dark desktop/mobile layouts.

Memory direction: run `check.cjs memory` for staged note receipts/evidence, escaped
run inspection and retained trivial-recall status, in light/dark desktop/mobile.
The model and memory endpoints are mocked; this is not live extraction acceptance.


UX audit: run `check.cjs audit` for synthetic 500-turn history windows, prepend,
query/fragment deep links, selection/focus/scroll preservation, a tool card kept
open while a second call arrives, typable retained drafts, reconnect age, expiry
and login draft recovery, and plain POST duplicate-submit guards. It checks both
themes at desktop/mobile sizes and writes mid-preview/settled screenshots. The
fixture budget is deliberately enlarged for synthetic long-history execution;
production context limits are unchanged. `check.cjs vision` also verifies image
layout and authenticated history after the dimension change.

With the same scratch server running, `node tests/browser/measure.cjs` prints
page and SSE payload bytes for comparable 44/500-turn fixtures and a roughly
four-second stub stream. This is mocked transport measurement, not live oMLX QA.


Layout/alignment: run `node tests/browser/layout.cjs` against the same scratch
server. It visits 36 screens/states in both themes at 320, 390, 600, 601, 640,
641, 768, 850, 851, 1024, 1440, and 2048 CSS pixels (864 screenshots/checks).
Set `SOLA_LAYOUT_WIDTHS=320,1440` for a shorter pass,
`SOLA_LAYOUT_THEMES=dark` for one theme, or `SOLA_LAYOUT_ONLY=chat` for the
focused chat/viewport pass (nine states per width/theme). Set
`SOLA_BROWSER_ARTIFACTS` for screenshots. Assertions check control height,
button/link text centering, filter/action row alignment, session-record bounds,
rename action widths/helper spacing, history-menu anchoring and page overflow.
Coverage includes blank and populated forms, errors, disabled/pending controls,
recurring/one-time schedules, expanded evidence, long titles/drafts, current and
archived chat, cleanup review/result/undo, login, and empty search/session lists.
Fixtures and POSTs use scratch data only. Native checkboxes retain their own
size inside 44px labels. This checks rendered geometry in Chromium, not CSS
class strings; review screenshots alongside the assertions.


Job tool policies: run `node tests/browser/job-tools.cjs` with the scratch server
for light/dark at 320, 390, 768 and 1440px. It checks inherit/selected/none form
round-trips, selection retention on validation errors, profile-change validation,
profile-drift warnings with narrowed execution, and historical prompt/schema
inspection. The fixture's status-only profile and `/qa/tool-grants` control exist
only in the isolated test server. Model/memory transports are mocked; screenshots,
page overflow and script errors are checked separately from Python/API tests.

Session sources: run `node tests/browser/session-sources.cjs` against the scratch
server to verify default cron exclusion, explicit inclusion, and filter layout at
390/1440px in light/dark themes. Optional `SOLA_CHROMIUM_PATH` selects an installed
Chromium executable; `SOLA_BROWSER_ARTIFACTS` selects the screenshot directory.

Run pagination: run `check.cjs runs` for 500-run session fixtures, server-side
session/status filtering, next/previous navigation and empty-result controls in
light/dark desktop/mobile. Evidence phase 11 scopes prompt/hash/schema selectors
to their panels so multiple saved inputs and tools remain inspectable.

Chat-first UI: run `NODE_PATH=/private/tmp/solakit-browser-qa/node_modules node
tests/browser/chat-first.cjs` against the same scratch server. It covers latest-topic
landing, title filtering, mobile drawers and focus restoration, streaming submission,
Details/run evidence, Settings/job execution, topic archive/restore, model health
changes, viewport/composer containment and screenshots in light/dark at 1440×1000
and 390×844. QA-only model availability controls exist solely in the fixture.
