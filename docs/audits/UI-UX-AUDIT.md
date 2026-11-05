> Implementation update (2026-11-05): findings **1–19 addressed** in this change.
> See [implementation status](implementation-status.md) for per-item coverage,
> measurements, browser QA, and remaining limits. Finding 8 uses draft recovery
> rather than persistent authentication. Finding 12's availability recovery was
> already present and now has a regression check. The text below preserves the
> original audit baseline; its fixture paths are operator-side references.

# Solakit Web UI — UX Audit (2026-11-05)

Method: three passes over current `main`. (1) Full static read of `ui/` (routes,
view, auth, all templates, app.css, app.js). (2) Live instance on a scratch
fixture (stub OpenAI-compatible model, operator token fixture) driven over HTTP.
(3) Real Chromium (Chrome-for-Testing via Playwright) screenshots at 1440×1000
and 390×844, dark and light, including mid-stream and tool-card states —
`~/workspace/ui-audit-fixture/shots/`. The moonlight aesthetic itself was out of
scope per the brief; everything below is function/polish. Items marked
*(observed)* were seen in the live browser; *(code)* are code-evidenced;
*(measured)* have numbers.

## Verdict

The pages are individually handsome and the light theme is at parity with dark.
The jank is structural, and it has one engine: **the chat renders and re-renders
its entire history as a single DOM block, forever** — plus a set of
interaction-state gaps (locked composer, blocking actions, no in-flight states)
that make the app feel heavy and slightly untrustworthy even when it's working.

## Tier 1 — the jank engine

**1. Streaming replaces the whole conversation DOM several times a second**
*(code + measured)*
The events endpoint re-renders `message_tail.html` — every turn, full Markdown —
every 200 ms, and the client does `conversation.innerHTML = snapshot.html`.
Measured on a seeded 41-turn session with short replies: the chat page is
**48 KiB of HTML**, and during one short streaming reply the server shipped
**7 snapshots of ~46 KiB each in 4 seconds**. Cost grows linearly with history;
on the real multi-year sessions every snapshot is a full re-render server-side
(including one API call per tool-call run) and a full DOM teardown client-side.
Consequences, all inherent to the mechanism: text selection destroyed mid-read,
focus inside the conversation destroyed, `<details>` open-state rescued by a
fragile index-based restore (a tool call inserted mid-stream reopens the wrong
card), images re-decoded per swap, and `aria-live="polite"` announcing a
full-DOM replacement. Fix direction: keyed per-turn deltas — snapshots carry
only the active turn's node; history renders once. (`routes.py:760-781`,
`app.js` snapshot handler.)

**2. History is never windowed** *(code)*
`conversation()` loads all messages on every page load and every snapshot; no
limit/offset anywhere. Same fix as #1: render the last N turns, "load earlier"
for the rest, snapshots touch only the tail. #1+#2 together are the single
highest-leverage change in this audit — they are the flicker, the scroll
jumps, and the heavy feel.

**3. Mid-stream presentation artifacts** *(observed, shots 04/17)*
The streaming preview renders partial Markdown literally — a dangling lone
bullet sits under the text while a list streams in. Simultaneously there are
**two** progress signals: a "Thinking — this turn is still in progress" card
inside the conversation *and* the "Waiting for Sola · 0:01" bar above the
composer. At completion the whole block swaps to final content (the Tier-1
swap), so every turn ends with a visible layout settle. One progress
affordance, and a preview that suppresses incomplete trailing Markdown
blocks, would calm most of it.

## Tier 2 — interaction state

**4. Composer is fully locked for the whole run** *(code + observed)*
`waiting(true)` disables the textarea itself. On multi-minute real runs the
user cannot draft, edit, or select their next message. Keep the textarea
enabled; disable only send. (`app.js` `waiting()`.)

**5. Operations "Run now" blocks the request until the job completes**
*(code)* The template copy admits it; `job_action` awaits the run, the button
has no pending state, and navigating away loses the redirect to the evidence.
Start async, redirect to the run page immediately — it already renders
queued/running states. (`routes.py:1109-1129`.)

**6. Dead second submission path** *(code)*
The composer has no `hx-*` attributes; the only HTMX use in the app is the
15 s status poll. The `htmx:beforeRequest/afterRequest/beforeSwap` handlers,
the `HX-Trigger: chatCompleted` branch, and its listener can never fire.
HTMX ships on every page for one poll. Delete the dead path or wire the
composer to it — the divergence is a standing source of "behaves differently
than the code suggests" bugs. (`chat.html:47`, `app.js`, `routes.py:703-712`.)

**7. No in-flight state on any plain form** *(code)*
Only the composer gets busy handling. Archive, compact, job delete/run/pause,
and bulk cleanup can all be double-clicked into duplicate POSTs; the second
lands on a 409-style error page that reads as if the (successful) action
failed. A five-line delegated submit listener fixes the class.

**8. Restart logs the user out mid-chat and eats the draft** *(code)*
UI sessions are in-memory despite a 7-day cookie. On restart the SSE stream
emits `expired`, JS hard-navigates to /login, and the composer's unsent text
(DOM-only) is lost. Persist UI sessions with the data tree, or preserve the
draft across the bounce (a draft is not a credential; the no-storage rule is
about credentials). (`ui/auth.py`, `app.js` expired handler.)

**9. Elapsed timer measures your arrival, not her work** *(code)*
Reconnecting to an in-flight run resets `started = Date.now()`. Seed it from
the pending run's `created_at`, which is already in the DOM.

**10. Search results don't take you to the match** *(code)*
Text results link to the bare session; the chat page then auto-scrolls to the
bottom and nothing highlights the hit. In a long session the result is
effectively unfindable. Deep-link to a message anchor with a flash highlight;
suppress scroll-to-bottom when an anchor is present. Related small thing
*(observed, shot 09)*: the default search mode is "Session name or ID" — a
content query returns "No matching sessions or messages" with copy that
doesn't mention the Full text mode sitting in the dropdown.

**11. Session-list archive bypasses the confirm the chat flow built**
*(code)* Chat archives via a dedicated confirm page; the sessions list
archives in one click (per-row button and bulk bar). An undo exists on the
result page but isn't announced beforehand. Same action, two safety levels.

**12. Status chips: stale bad news + reflow** *(observed + code)*
After two failed runs (fixture stub fault), the header read "App degraded ·
Model unavailable" on every page — and kept reading that way in screenshots
taken *after* subsequent runs had completed successfully. Whether the model
probe is interval-latched or failure-latched, the displayed state contradicted
live behaviour for minutes: the status line cries wolf. Separately, the chips
are `outerHTML`-swapped every 15 s whether or not anything changed, and
value-length changes can rewrap the row and shift page content (on chat, that
resizes the conversation mid-read). Return 204 when unchanged; give the row a
fixed min-height; make model health recover on the next successful completion.

## Tier 3 — layout and polish (screenshot-verified unless noted)

**13. Chrome stack / mobile chrome tax** *(observed, shots 03/15)*
Chat stacks global header + status row + chat toolbar + archive toolbar
above the conversation. At 390×844 the header wraps (brand row, nav row,
two chip rows), the archive toolbar wraps, and the session badges squeeze
into a ~90 px stacked column — roughly 45% of the viewport is chrome before
the composer. On `chat-app`, fold status into the chat toolbar and drop the
global nav row on small screens.

**14. Tool cards** *(observed, shots 17/18)*
Mostly good: slim collapsed bar, state word at right. Three nits: the summary
renders a stray "–" (`▸ – Tool call: runtime.status`) like an empty slot;
the expanded Result is a single-line JSON in a `pre` that runs into the
card's right edge with no scroll cue or fade; and expanding pushes the
conversation (inherent to `<details>`, acceptable — but combined with Tier 1,
any snapshot during streaming collapses/reopens by index).

**15. Tables on mobile truncate mid-word with no scroll hint** *(observed,
shot 16)* `.data-table { min-width: 640px }` in `.table-scroll`: headers cut
("Co…", "Next"), the Operations Actions column starts off-canvas, nothing
indicates sideways scroll. Drop the min-width under 640 px or add an edge
fade. The Operations "Refresh ↻" link also wraps so the ↻ orphans onto its
own line.

**16. Timestamps flash as raw ISO** *(code + verified in served HTML)*
`<time>` bodies are server-rendered as `2026-11-05T22:38:13.357657+00:00`
and rewritten by JS on load — a visible flicker on every page, permanent
raw ISO with JS off. Format server-side; let JS adjust only the timezone.

**17. Images carry no dimensions** *(code)*
`message_tail.html` prints the image's width×height as *text* beside an
`<img>` with no dimension attributes, while startup scrolls to bottom before
lazy images lay out — the view lands short, then jumps. Emit
`width`/`height`/`aspect-ratio` from data already in the template.

**18. Touch targets ~34 px** *(code)*
Send button 34×34, sidebar new-chat 34×34, secondary buttons ~34 px tall —
the most-tapped controls are the smallest. 40–44 px costs nothing visually.

**19. Small stuff** *(observed/code)*
Sessions-list row arrow renders as a stray "→" floating between date and
button. `/favicon.ico` 404s (the moon icon exists as a static asset but the
root favicon request isn't satisfied). The no-EventSource fallback path
blocks on the full run server-side then reloads — make it accept-then-
redirect like the main path. Composer textarea never auto-grows (fixed
2 rows + manual drag handle; the overlaid send button sits where long text
ends up).

## What's genuinely good (don't regress it)

- Light theme is at full parity with dark (shot 13) — same hierarchy, error
  cards readable in both.
- Failure presentation is honest and calm: the amber run-failure card states
  the status, never a fake reply, and the composer recovers enabled+focused.
- Empty states (sessions, operations, search) are clear and unpanicked.
- `:focus-visible` outlines, skip link, `prefers-reduced-motion`,
  `prefers-contrast: more`, and 16 px mobile input font (no iOS zoom) are
  all already in the CSS.
- Confirm pages for archive-conversation, compact, and job delete are well
  built (job delete even disables itself while the job is active).

## Suggested fix order for Codex

1. **Delta streaming + tail windowing** (#1, #2, #3) — one change, most of
   the jank. Acceptance: snapshot payload during a stream contains only the
   active turn; a 500-turn session's chat page ships bounded HTML; text
   selection survives a stream.
2. **Composer stays typable; elapsed seeds from run start** (#4, #9).
3. **Async Run now + generic form in-flight disabling** (#5, #7).
4. **Status row: 204-when-unchanged, fixed height, health recovers on
   success** (#12).
5. **Mobile chrome diet + table scroll cues + timestamp server-formatting**
   (#13, #15, #16). Then Tier-3 nits as filler work.

Fixture + screenshots: `~/workspace/ui-audit-fixture/` (stub model, config,
`shots/`). Seeded session `audit-long` (44 turns) still in the fixture data
tree for regression comparisons.
