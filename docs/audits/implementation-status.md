# UI/UX audit implementation — 2026-11-05

The user requested the audit feedback, including streaming improvements. The
attached streaming plan's narrower deferrals were reference scope, not a request
to exclude the rest of the audit. This change addresses all 19 findings. The
existing moonlight palette, evidence semantics, authentication and opt-in
integrations are preserved.

## Coverage

| Audit | Implementation |
| --- | --- |
| 1–2 | Active-run SSE deltas; recursive keyed reconciliation; 20-turn HTML windows and prepend for current, lifecycle-archived, and numbered history. |
| 3 | One chat progress bar; unfinished trailing list/fence/table/quote blocks withheld; exact final saved content replaces the preview. |
| 4, 9 | Send alone disables; drafting/selection stay available; acceptance preserves concurrent edits; elapsed seeds from run creation across reconnect. |
| 5 | UI Run now returns the durable queued run receipt and redirects before generation/delivery. Existing blocking API and job-state queue responses remain available. |
| 6–7 | Dead composer HTMX handlers/response branch removed. All ordinary POST forms mark busy and prevent duplicate submissions; native serialization runs before button disabling. |
| 8 | Per-session, same-tab sessionStorage draft recovery across expiry/login/restart; accepted text clears, unsent edits survive. UI auth still deliberately expires on restart. No credentials are persisted. |
| 10 | Search links include run/window and physical message anchor, with target outline; fragment-only links fetch missing windows; default search copy explains Full text. |
| 11 | Per-session lifecycle archive has a confirmation; list bulk archive opens a dry-run review with activity checks and restore copy. Conversation rotation keeps its existing dedicated confirmation. |
| 12 | Visible-state digest yields 204 for unchanged status polls; stable grid/min-height reserves layout. Existing model success already sets availability; a regression verifies failed state recovers immediately on successful completion. |
| 13, 15 | Mobile chat removes global nav, folds health into toolbar disclosure, and scrolls a single archive-action row. Tables have a sideways-scroll hint and refresh remains together. |
| 14 | Extraneous tool-summary glyph removed; arguments/results wrap within scrollable bounded panels. Stable tool nodes survive inserted calls. |
| 16–18 | UTC server-formatted time bodies, localized by browser enhancement; image width/height attributes reserve aspect ratio; 44px primary/secondary/send/new-chat controls. |
| 19 | No row arrow; root favicon resolves; native fallback accepts/redirects without waiting; textarea grows up to its viewport cap. |

## Measurements

The supplied audit baseline reported a 41-turn page of **48 KiB**, and
**7 × approximately 46 KiB** full-history snapshots (about **322 KiB**) in four
seconds. Its original `~/workspace/ui-audit-fixture` directory is not present in
this environment, so these are the supplied audit's numbers, not a claimed rerun.

Reproduction uses this repository's isolated QA server with synthetic evidence,
a deterministic OpenAI-compatible SSE stub and disabled external integrations.
`node tests/browser/measure.cjs` recorded:

| Synthetic history | Initial HTML | Stream duration | Snapshots | Payload range | Total snapshot payload |
| --- | ---: | ---: | ---: | ---: | ---: |
| 44 turns | 25,885 B | 4.117 s | 8 | 713–1,307 B | 8,005 B |
| 500 turns | 26,187 B | 3.972 s | 8 | 713–1,307 B | 8,005 B |

Both pages render exactly 20 turns. Page totals include the session sidebar,
whose session count grew when the second fixture was added. Both stream totals
are identical: no historical run ID appears in a delta. This is about **97.6% less
snapshot traffic** than the supplied short-reply baseline. A separate two-tool
stream on 500-turn history carried only one active turn per delta, with a largest
payload around 5.7 KiB, including its redacted tool results.

Initial page and earlier-window requests still fetch the full API transcript and
run catalog once to group/select turns. They hydrate tool logs only for the
selected window. This is bounded HTML, not a storage pagination claim. Streaming
reads run metadata/tool logs and the current turn backwards in 8 KiB blocks;
it never fetches the session transcript or settled runs' evidence per tick.
Run discovery happens on connection and when advancing accepted queued work.
No transcript, prompt, model wire assembly, or preview persistence is changed.

## Validation

- Full Python suite: **455 passed** (core plus all pack tests).
- Ruff check and format check passed; core and all seven pack wheels/sdists built.
- Chromium audit at 1440×1000 and 390×844, light/dark: selection and link focus
  remain; reading scroll stays fixed; the first tool node stays open when the
  second appears; no other card opens; drafts survive completion and expiry/login;
  reconnect timer retains run age; plain forms reject repeated submit events.
- Separate Chromium streaming/disconnect/cancellation, vision, job management,
  session lifecycle, and search matrices passed in all four theme/viewport combinations.
- Packaged assets/templates checked in built distributions. Browser screenshots
  reviewed separately from HTTP/unit checks. No live model, memory extraction,
  Discord, or external sending was exercised.

## Operational limits

Draft recovery depends on same-tab sessionStorage availability. Authentication
continues to require login after restart. The default no-JavaScript send accepts
and redirects to the run-in-progress conversation, with a manual reload link.
No public deployment or live-data migration is part of this change.

## Conditional tool disclosure — 2026-11-05

Implemented opt-in deferred schemas with eager defaults. A frozen per-run ToolView
keeps the static granted-only index, initial resident set and schema catalog;
search/load activate only effective grants. Direct inactive calls load their
schema and return without execution. Pure discovery has an eight-step cap,
separate from profile work and repair budgets. Scope/capability enforcement stays
on the existing dispatch path; activation does not grant authority.

Profile and job modes/residents, API/chat/form editing, job/profile inspection,
offline diagnostics, context checks and historical activation/step inspection are
implemented. Initial schemas retain their existing immutable evidence; deferred
runs additionally snapshot a hash-verified full catalog. Job inherited residents
intersect the job ceiling; explicit replacement residents must belong to that
ceiling. This resolves the plan's inherited-resident ambiguity without widening
access. Stored profile drift safely shrinks exposure and remains visible. Workers
use their own profile defaults; sub-session jobs carry their job overrides.

Validation: **519 core/pack tests passed**, including **39 disclosure regressions**
covering eager bytes/order, granted-only indexes, lexical search and exact aliases,
partial loads, same-group activation, inactive-call interception, repair/work/
discovery budgets, streaming, context/compaction rechecks, capability checks,
job modes/validation/profile drift, sub-sessions, missed records, worker isolation,
readonly offline diagnostics and immutable schema evidence. Ruff and formatting
checks passed; core and all seven pack wheels/sdists built. Chromium form,
validation, dry-run/recorded inspector and catalog checks passed at 320/390/768/1440
pixels in both light and dark themes; screenshots were reviewed separately.

No live pinned-model evaluation was run. **Interactive remains eager**; mechanism
verification with mocks does not satisfy the live adoption gate. Before enabling
interactive deferred mode, run the golden tool-use battery eager versus deferred:
no eager-successful task may regress, median additional tool-task model steps must
be at most one, and initial interactive request bytes must fall at least 50%.
