# Chat and Settings

Start `sola serve` with your normal configuration and the environment variable
named by `http.password_env` set. Open `http://127.0.0.1:8765/` (or your explicitly
configured origin) and sign in. The server serves the HTML, CSS and Lit components
from the core wheel. Components import the bundled `lit-core.min.js` locally;
icons come from the bundled Lucide `lucide.min.js` (ISC) through a light-DOM
`<sola-icon name="…">` element, always decorative beside visible text;
there is no frontend build, package installation or external CDN. The app shell,
conversation turns, job cards and artifact viewer use light DOM to share the
stylesheet and preserve native labels, keyboard navigation and fragment links.
See [HTTP authentication and deployment](contracts/api.md) for LAN HTTP and remote HTTPS setup.

The interface follows your system light or dark appearance through
`prefers-color-scheme`, including native controls and dialogs. Changes apply
immediately; there is no separate theme setting. On a fresh setup, **Start a
topic** opens topic creation. Empty/whitespace drafts cannot be submitted.

Set the top-level `agent_name = "Sola"` in `config.toml` to change the name shown
throughout the UI. Restart the server and reload the page after changing it.
The name is presentation only; operator-owned identity prompts remain verbatim.

To customize appearance, create `user.css` in the configured `data_dir`. It loads
after the packaged CSS and applies to all components through their shared light
DOM. Reload the page after adding, editing or removing it; no server restart is
needed. An absent file leaves the default appearance. The file must be a regular
file, not a symbolic or hard link. Like the login shell, this stylesheet is served
before sign-in, so keep its contents free of private data. The existing Content
Security Policy still applies.

Create a topic, choose its memory mode, and send a message. Enter sends;
Shift+Enter inserts a newline. The composer remains editable while Sola works,
and additional submissions appear as queued runs. Each topic keeps its draft,
profile and selected navigation in browser session storage, across reloads in
that tab. Closing the tab clears these drafts. No credentials enter browser
storage; sign-in uses the existing HttpOnly session cookie and CSRF protocol.

When an image route is configured, **Attach image** accepts one PNG or JPEG.
The draft shows the thumbnail and **Remove image** alongside the question.
**Use image in next message** adds an image already in the topic. On the separate
vision route the run shows **Analyzing image…** while the vision model works.
Later questions can use the saved observation. Attach the image again when Sola
needs to look at the pixels. See [image workflows](image-workflows.md).

A lost submission receipt stays in the outbox. **Retry same submission** reuses
its original content, topic, profile, attachment and admission key. A new draft gets a new
key. Reconnect can also discover the original admission and remove the outbox
entry. A disconnection does not cancel accepted work. Avoid creating a fresh
copy of an uncertain submission if it could repeat a consequential action.

The initial conversation loads recent messages and runs, rather than walking
the topic from its beginning. **Load older conversation** prepends bounded pages
while retaining the visible turn. New tokens update a provisional preview;
pulsing dots show a run that is working before any text or tool appears.
Settled messages and open disclosures keep their DOM nodes. While at the bottom,
the view stays pinned as content, images or opened disclosures grow. Scrolling
up stops automatic following; the floating **Jump to latest** button resumes it
and gains a dot when new content arrives below. Elapsed time comes from the
server's start/end timestamps and survives reconnect and navigation.

Each turn is one timeline in record order: your message, any text the model
wrote while working, its tool calls, then the reply. In-between text is muted to
distinguish it from the final reply. Tool calls, memory work and summary markers
are compact one-line disclosures; open one to see its arguments, receipt and
artifact. A status icon marks each disclosure and the run line: spinner for
working, check for succeeded/completed, cross for failed/denied, `!` for
uncertain/ambiguous, dashed circle for queued/pending and dash for cancelled.
The state is always written in words beside the icon.

Use **Cancel run** to request cancellation and **Inspect evidence** for records,
exact model-request references, receipts and verified artifact chunks. The chat
loads each run's records back to its start, then refreshes recent activity; the
evidence dialog pages through all records. Tool
results are associated with their call IDs. Tool errors, uncertain effects,
interrupted work, memory extraction state and staged notes have separate labels.
A staged note is not a claim that memory extraction succeeded. Context summary
markers retain original turns; topic details can rename, pin or archive a topic.
Use **Search topics** above the topic list to find a title/name by a partial,
case-insensitive match, including topics beyond the first page. Pinned topics
always appear first, including pins beyond the first page and pins whose titles
do not match the search. Pins follow the Archived topics filter. **More topics**
continues the filtered results. Clearing the field restores the list; searching
keeps the current conversation and draft open. A message explains an empty result.
The Archived topics checkbox searches/lists archived topics instead of active ones.
Archived and already-used ephemeral topics explain why sending is unavailable;
their drafts remain editable. Single-use topics require a new topic for another
run. Error notices can be dismissed without removing an uncertain submission
from the outbox. The selected topic stays visible in narrow-screen navigation.

Settings uses six keyboard-accessible tabs, with a separate dialog for job
creation and editing. Use the X or Escape to close Settings. Arrow keys move
between tabs. Home and End select the first or last tab. Navigation stays visible
while section content scrolls. On small screens, the tabs form a compact grid.

- **Schedules:** create/edit, enable/disable, run now, delete and
  inspect occurrences. New jobs start disabled. Schedules use
  `cron:<five fields>` or `once:<aware ISO timestamp>`. Each job chooses its own
  profile, a shared/isolated topic or a fresh one-run target, and whether to
  publish results or retain only execution evidence. The Topic dropdown lists
  active shared and isolated topics. If a job's bound topic is archived, choose
  a new target before saving. One-run topics are archived when the run ends.
  **Permissions & limits** shows tool checkboxes for the selected profile when
  **Limit tools for this job** is checked. Unchecked tools are excluded. Leaving
  the limit off inherits the profile's tools. The path field appears when
  **Limit workspace roots for this job** is checked. An empty path list allows
  no roots. This section can also narrow all six budget fields.
  Blank limits inherit scheduled-work limits. The server enforces authority.
  **Run now** works with the job's schedule off and has its own durable retry
  key. **Pause all schedules** also suspends manual runs.
- **Memory:** readable observer/commit counts and manual reconciliation of
  failed/ambiguous extraction using the memory commit ID and a verified provider
  task ID. Run memory cards also offer reconciliation. This checks existing work
  without resubmitting content.
- **Profiles & tools:** read-only model, memory policy, workspace roots and
  available tool descriptions/schemas. Effective configuration and provenance
  remain inspectable in a disclosure. Permissions and credentials stay in
  operator-owned configuration; credential values never enter this page.
- **Search:** evidence-backed phrase search in the current topic or all topics,
  with a bounded result limit, readable snippets, topic navigation and direct
  run-evidence inspection. An empty result or failed search has explicit feedback.
- **Diagnostics:** readable runtime health, streaming/polling controls and draft
  prompt inspection, including optional hashes. Draft inspection assembles a
  request without model inference, topic history or semantic recall; it can
  include private operator prompt content.
- **Security:** this login’s expiry and an explicit, confirmed action to revoke
  every browser/API login, including the current one.

The run-evidence dialog has **All records** and **Model requests** tabs, each with
independent pagination and authorized artifact inspection. Close it with the X,
Escape, or a click or tap outside the dialog. Settings loads each section on
demand, so an unavailable diagnostic service does not block the other sections.
Streaming failure still falls back to five-second polling and retries
SSE; both transports use the same record maps and stable IDs. Sign-in expiry
retains drafts and pending submissions.

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
- `settings.js`: Settings/job-dialog templates and their API actions.
- `evidence.js`: independently paged run records/model requests and artifact panels.
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
