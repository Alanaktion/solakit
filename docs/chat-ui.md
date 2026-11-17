# Chat and Settings

Start `sola serve` with your normal configuration and the environment variable
named by `http.password_env` set. Open `http://127.0.0.1:8765/` (or your explicitly
configured origin) and sign in. The server serves the HTML, CSS and vanilla
JavaScript from the core wheel; there is no frontend build or external CDN.
See [HTTP authentication and deployment](contracts/api.md) for remote HTTPS setup.

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
44-pixel controls and a narrow-screen layout. Timers do not announce every second.

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
npm install --prefix /tmp/sola-chat-browser --no-audit --no-fund playwright
/tmp/sola-chat-browser/node_modules/.bin/playwright install chromium
SOLA_PLAYWRIGHT_PATH=/tmp/sola-chat-browser/node_modules/playwright \
  node scripts/check_chat.mjs --fixture /tmp/sola-chat-fixture/fixture.json \
  --output docs/validation/phase-11/browser
```

`SOLA_BROWSER` can select an already-installed Chromium executable. The script
creates its own conversation through the UI, so it can be rerun against the same
fixture. It records payload/node measurements, results and desktop/mobile PNGs.
Stop the fixture with Ctrl+C when finished. Browser tooling is not a runtime or
Python dependency.

Automated acceptance is separate from operator acceptance. The operator still
needs to complete the conversation, permitted tool task, scheduled message and
recovery journeys against the intended deployment. Safari/touch devices, a real
screen reader and remote TLS are not established by a headless Chromium check.
