# Chat and Settings

Start `sola serve` with your normal configuration and the environment variable
named by `http.password_env` set. Open `http://127.0.0.1:8765/` (or your explicitly
configured origin) and sign in. The server serves the HTML, CSS and Lit components
from the core wheel. Components import the bundled `lit-core.min.js` locally;
there is no frontend build, package installation or external CDN. The app shell,
conversation turns, job cards and artifact viewer use light DOM to share the
stylesheet and preserve native labels, keyboard navigation and fragment links.
See [HTTP authentication and deployment](contracts/api.md) for LAN HTTP and remote HTTPS setup.

The interface follows your system light or dark appearance through
`prefers-color-scheme`, including native controls and dialogs. Changes apply
immediately; there is no separate theme setting. On a fresh setup, **Start a
topic** opens topic creation. Empty/whitespace drafts cannot be submitted.

Create a topic, choose its memory mode, and send a message. Enter sends;
Shift+Enter inserts a newline. The composer remains editable while Sola works,
and additional submissions appear as queued runs. Each topic keeps its draft,
profile and selected navigation in browser session storage, across reloads in
that tab. Closing the tab clears these drafts. No credentials enter browser
storage; sign-in uses the existing HttpOnly session cookie and CSRF protocol.

A lost submission receipt stays in the outbox. **Retry same submission** reuses
its original content, topic, profile and admission key. A new draft gets a new
key. Reconnect can also discover the original admission and remove the outbox
entry. A disconnection does not cancel accepted work. Avoid creating a fresh
copy of an uncertain submission if it could repeat a consequential action.

The initial conversation loads recent messages and runs, rather than walking
the topic from its beginning. **Load older conversation** prepends bounded pages
while retaining the visible turn. New tokens update a provisional preview;
settled messages and open disclosures keep their DOM nodes. Scrolling up stops
automatic following; **Jump to latest** resumes it. Elapsed time comes from the
server's start/end timestamps and survives reconnect and navigation.

Use **Cancel run** to request cancellation and **Inspect evidence** for records,
exact model-request references, receipts and verified artifact chunks. The chat
shows recent run activity; the evidence dialog pages through all records. Tool
results are associated with their call IDs. Tool errors, uncertain effects,
interrupted work, memory extraction state and staged notes have separate labels.
A staged note is not a claim that memory extraction succeeded. Context summary
markers retain original turns; topic details can rename, pin or archive a topic.
The Archived topics checkbox makes archived topics available again.
Archived and already-used ephemeral topics explain why sending is unavailable;
their drafts remain editable. Single-use topics require a new topic for another
run. Error notices can be dismissed without removing an uncertain submission
from the outbox. The selected topic stays visible in narrow-screen navigation.

Settings contains:

- Job creation/editing, explicit enablement, pause/resume, run-now, deletion and
  occurrence inspection. New jobs are disabled by default. Schedules use
  `cron:<five fields>` or `once:<aware ISO timestamp>`. Run-now has its own durable
  retry key. Resume a paused job before running it. Scheduled/proactive messages
  carry their attribution; no-action decisions are visible in occurrences.
- Read-only profile authority, tools and workspace roots. Configure permissions
  and credential references in operator-owned configuration outside the browser.
  Credential values never enter page source or profile diagnostics.
- Memory status refresh; failed/ambiguous extraction cards expose reconciliation
  with a verified provider task ID. This checks existing work without resubmitting
  its content.
- Conversation-evidence search, health and an explicit polling switch. Streaming
  failure falls back to five-second polling and retries SSE. Both use the same
  record maps and stable IDs. Authentication expiry asks for login and keeps the
  draft and pending submissions.

Markdown supports paragraphs, headings, emphasis, lists, quotations, code and
explicit HTTP/HTTPS/mailto links. Raw HTML is text; images do not fetch remote
resources. Tool output and evidence use text nodes. Artifact inspection checks
normal API authorization and downloads chunks of at most 64 KiB. Details are
collapsed and scrollable. The shell has a restrictive Content Security Policy,
labelled controls, native keyboard dialogs, polite progress announcements,
compact 36-pixel desktop controls and 44-pixel targets when a touch pointer is
available. Conversation text stays at 16 pixels; supporting controls use relative
font sizes. Form boundaries and focus rings remain visible in both themes,
including a selected-topic outline in forced-colors mode. A skip link reaches
the composer, dialogs retain native keyboard behavior, and jumping to the latest
message keeps keyboard focus in the conversation. New settled replies have a
dedicated polite announcement; previews, timers and loaded history do not repeat
that announcement. Narrow screens reflow, and very short portrait windows allow
page scrolling so the composer stays reachable.

## Web source layout

- `app.js`, `turn.js`, `job.js`, `artifact.js`: Lit components and their rendering.
- `settings.js`: job forms/catalog, profiles/tools, memory status and evidence search.
- `evidence.js`: paged run records and artifact panels.
- `client.js`, `ui.js`: authenticated requests, tab storage, shared DOM/error helpers and the light-DOM base.
- `chat.js`: conversation state, topics, drafts/outbox, SSE/polling and startup wiring.

Components use explicit callbacks for conversation refresh/cancellation and job
editing/refresh. Shared requests signal authentication expiry to the conversation
controller. Imports flow toward helpers and components; components do not import
`chat.js`. Every module is packaged in the wheel and explicitly allowlisted by the
HTTP boundary. No bundler or external module source is required.

## Reproducible browser acceptance

The fixture starts a real loopback HTTP server with the real runtime/services,
a synthetic streaming model and a four-second synthetic tool. It creates 500
settled turns, uncertain tool evidence, staged/ambiguous memory, a summary marker
and an interrupted run. It never uses production configuration, model or memory.
The output directory must not already exist.

```sh
uv run --locked python scripts/chat_fixture.py --output /tmp/sola-chat-fixture
```

In a second terminal, install browser-only developer tooling outside this project
and run the script. Use the fixture-only password printed by the server for manual
inspection. Never use this fixture password or configuration in production.

```sh
npm install --prefix /tmp/sola-chat-browser --no-audit --no-fund playwright @axe-core/playwright
/tmp/sola-chat-browser/node_modules/.bin/playwright install chromium
SOLA_PLAYWRIGHT_PATH=/tmp/sola-chat-browser/node_modules/playwright \
  node scripts/check_chat.mjs --fixture /tmp/sola-chat-fixture/fixture.json \
  --output docs/validation/phase-11/browser
```

`SOLA_BROWSER` can select an already-installed Chromium executable. The script
creates its own conversation through the UI, so it can be rerun against the same
fixture. It records payload/node measurements, results and desktop/mobile PNGs.
It checks live appearance switching, text and form-boundary contrast in both
themes, 320-pixel and 390-pixel portrait layouts, short landscape layouts,
archive/restore, single-use topics, keyboard submission and composition input.
The same script runs axe WCAG A/AA scans on login, chat, topic, Settings and
evidence surfaces, plus keyboard focus, touch target, 200% text, forced-colors
and short-window checks. The report retains axe rules needing manual review;
passing automated checks is not a claim of full WCAG conformance.
Stop the fixture with Ctrl+C when finished. Browser tooling is not a runtime or
Python dependency.

Automated acceptance is separate from operator acceptance. The operator still
needs to complete the conversation, permitted tool task, scheduled message and
recovery journeys against the intended deployment. Safari/touch devices, a real
screen reader and remote TLS are not established by a headless Chromium check.
Configured-server Qwen conversation, cancellation, recovery, Settings and manual
scheduled delivery have also been browser-verified; see the
[Phase 11 follow-up evidence](validation/phase-11.md#appearance-and-ux-follow-up).
