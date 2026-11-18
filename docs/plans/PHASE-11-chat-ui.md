# Phase 11 — Chat-first web UI

Status: implemented; automated verification is recorded in [Phase 11 evidence](../validation/phase-11.md). Operator acceptance remains pending. Depends on [Phase 10](PHASE-10-api-cli-events.md). Unlocks browser/operator acceptance of the daily-use candidate.

The appearance/UX follow-up adds system light/dark appearance, clearer empty and
disabled states, compact mobile controls and expanded browser verification.
Configured-server Qwen conversation, cancellation, recovery, Settings and manual
scheduled delivery pass browser checks; synthetic tool/automatic-scheduler
journeys remain separately identified in the evidence.
The density/accessibility follow-up tightens desktop spacing, preserves touch
targets, improves control contrast and reply announcements, and extends browser
checks with axe, keyboard, enlarged-text and forced-colors coverage. See
[follow-up evidence](../validation/phase-11-accessibility.md).

The API-coverage follow-up replaces the Settings accordion with six focused
tabs and a separate job editor. It exposes job authority/budget/delivery/target
controls, readable profile/tool and health views, search navigation, draft prompt
inspection, a model-request evidence view and login revocation. See
[coverage and verification](../validation/phase-11-settings.md).

## Outcome

Sola's web interface centers the conversation, stays usable during long work, and exposes evidence without turning every turn into a diagnostic dashboard.

## Implementation slices

1. **Build the shell and topic navigation.** Use a packaged HTML shell and Lit components importing the local `lit-core.min.js`, with no frontend build or CDN. Provide Topics, readable conversation, composer, and restrained run progress. Fetch runtime data through the public API. Preserve selected topic and drafts across appropriate navigation. Settings contains profiles, job management, tools/workspace roots, credential references, search, and diagnostics.
2. **Render stable conversation records.** Key messages, runs, and tool cards by server IDs. Append/update only affected nodes; settled messages remain stable while previews change. Associate results with tool-call IDs. Keep drafts editable during execution and mark queued submissions. Retry a lost submission with the same idempotency key; rotate the key for a new draft.
3. **Handle history and streaming independently.** Load bounded recent history with cursor-based older pages. SSE snapshots do not contain the full topic. Preserve scroll anchor while prepending history; autoscroll only when the reader is following the bottom. Seed elapsed time from server start time, so reconnect/navigation never resets it. Polling fallback uses the same state/reducer and IDs.
4. **Make incomplete work intelligible.** Show queued/running/failed/interrupted/cancelled states, tool failure, uncertain effect, memory pending/failed/ambiguous, and proactive-message attribution. Distinguish staged notes from extracted memories. Keep compaction/archive boundaries readable without moving original turns. Reconnect replaces transient previews with authoritative state rather than duplicating text.
5. **Support controls and safe content.** Expose cancellation, job pause/run-now, memory refresh/reconciliation entry points, and evidence inspection. Confirm consequential configuration through clear user actions where needed, not model-generated controls. Safely render Markdown, disable raw model HTML, escape tool outputs, restrict link schemes, and authorize artifact downloads. Do not put credential values into page source.
6. **Verify interaction and layout.** Follow system appearance with `prefers-color-scheme`; test light/dark text contrast and native controls. Test keyboard navigation, labelled composer/actions, focus retention, screen-reader progress, reduced motion, sufficient contrast, touch targets, and narrow/short screens. Long tool output is collapsed/bounded with inspectable detail. Use product-facing language; framework and storage internals belong in diagnostics.

## Verification and exit gate

Deliver chat/Settings UI, browser acceptance scripts, representative screenshots, and remaining UX issues. Exercise two submitted drafts during a tool run, failure/cancellation, topic switch/reload, auth expiry, offline/reconnect, preview loss, polling fallback, and autonomous no-action/message states.

Use a 500-turn fixture to measure initial payload and node growth; older history is paginated and snapshot size remains bounded. Select settled text while tokens stream, type while progress changes, and verify selection/focus survive. Reconnect must not duplicate cards or reset timers. Test hostile Markdown/tool strings and narrow/mobile layouts in a real browser.

Exit requires operator completion of conversation, tool task, scheduled message, and recovery journeys through the UI. HTTP-only tests cannot substitute for browser acceptance. The operator-requested Lit migration supersedes the original vanilla-JavaScript restriction. Keep the bundled local import and avoid adding a build pipeline or deeper integration.
