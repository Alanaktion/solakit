# Chat Markdown and streaming

Status: partial — implementation and automated verification complete; live backend
and targeted browser checks remain.

## Implemented

- [x] Assistant chat messages use server-side CommonMark with tables, sanitized by
  an explicit HTML/attribute/URL allowlist. Raw HTML and images are disabled;
  user text, tool evidence, summaries and prompt/run evidence remain literal.
- [x] Rendering follows credential masking. Stored/API message text and hashes
  remain unchanged. No external fonts, scripts, images or Node build are added.
- [x] Web chat submits once, accepts a queued run immediately, then observes
  authenticated same-origin SSE snapshots. Reload observes existing queued/running
  work; disconnects do not cancel accepted runs or automatically replay POSTs.
- [x] OpenAI SSE responses assemble UTF-8 content and indexed tool argument deltas.
  Pinned model, complete envelope, terminal reason and `[DONE]` are required before
  durable assistant messages or tool execution. Response bytes and total streaming
  duration are bounded; tool ordering, budgets and cancellation share the existing loop.
- [x] Transient previews are explicitly labeled unsaved and separate from stored
  messages. Full credentials and possible trailing secret prefixes are masked before
  previews leave the API. Only completed tool logs supply visible arguments/results.
- [x] Existing synchronous API and non-JavaScript form behavior remain available.
  Browser observation preserves open tool details and avoids dragging a reader to
  the bottom. Native browser EventSource handles the GET transport only; errors
  close observation and advise checking evidence before sending again.
- [x] Regression checks cover Markdown structure/XSS, split secrets, byte-level SSE
  assembly, tools/usage/order, wrong models, malformed/interrupted/limited streams,
  bounds, pre-completion ASGI delivery, disconnect/reload and client state/keyboard.

## Remaining verification

- [ ] Deliberately verify oMLX's `stream=true`, `stream_options.include_usage`, pinned
  model ID, tool-call deltas and terminal events on the Sola host. No JSON fallback
  or automatic resubmission occurs when a backend rejects streaming.
- [ ] Browser-check code/table overflow and links in light/dark/mobile views.
- [ ] Browser-check partial Markdown, tool-call expansion, queued turns, cancellation,
  reload, sign-out/expiry, connection loss and scroll/focus behavior while streaming.

The browser receives HTML snapshots at most five times per second, built solely
from existing APIs; there is no UI access to Store/Execution. Live previews are
in-memory and disappear on failure/restart; they are not saved assistant messages.
A model transport interruption produces an explicit run failure. On a first-step
failure no session messages are appended; earlier completed steps remain evidence.
Run cancellation remains available through the existing run evidence page/API.
