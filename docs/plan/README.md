# Implementation plan

Source of truth: [SPEC.md](../../SPEC.md), draft v2. Keep the existing
`src/solakit` package and place spec modules beneath it.

- [01: configuration-lifecycle](./01-configuration-lifecycle.md)
- [02: storage-routing](./02-storage-routing.md)
- [03: prompt-inspection](./03-prompt-inspection.md)
- [04: tool-capabilities](./04-tool-capabilities.md)
- [05: model-execution](./05-model-execution.md)
- [06: memory-compaction](./06-memory-compaction.md)
- [07: channels-discord](./07-channels-discord.md)
- [08: scheduling](./08-scheduling.md)
- [09: domain-tools-verification](./09-domain-tools-verification.md)

## Working agreement

For each phase, synchronize main, implement the bounded scope, run its exit
checks plus pytest, Ruff lint and format checks, update the completion record,
and push directly to main without force. Keep integrations opt-in. Never mark a
phase complete on the strength of a mock alone when its exit requires live work.

## Resolved implementation defaults

- Recall precedes final prompt construction and persistence.
- Shell is maintenance-only until an explicit profile change.
- Filesystem scopes and non-filesystem capabilities are separate grants.
- Job tasks use `{date}` with a bounded allowlist of substitution keys.
- Recall failure aborts before transcript mutation; commit failures are recorded
  separately. Poll acknowledged extraction; never replay ambiguous writes without
  verified idempotency support or explicit operator reconciliation.
- Model identity matches the configured ID exactly; no fallback.
- Reserved isolated memory must fail closed until scope support exists.

## Rollout

Implementation is separate from production cutover. Complete SPEC §14 before
starting the Discord pilot. Then follow §15: operator prompt audit, one-week
pilot, shadow blog publishing, jobs individually, and a month rollback window.
Hermes remains untouched. No production deployment is part of this plan run.


## Current checkpoint

Phases 1–5 are implemented. Phases 6–8 are locally implemented and covered by
mock/controlled-clock tests. Phase 6's deliberate live round-trip is pending on
the Sola host; phase 7's live pilot and phase 8's live jobs remain gated by that
check and phase 9 acceptance. The generic services and journal pack are now locally implemented; remaining
packs and complete live acceptance are unfinished (see plans 14–19).

The first-contact guide records the supplied server settings and the exact
remaining verification. No pilot/deployment, production configuration, or Hermes
changes have been made. Local completion is not a production release claim.

## Review hardening checkpoint

The follow-up code review's six findings are addressed: special-file read refusal,
atomic session metadata with isolated damage reporting, explicit corruption/torn-tail
recovery, serialized shutdown/admission, idempotent cancellation, and redacted
execution diagnostics. Regression tests cover the reproduced failures. These fixes
do not advance phase 6 or enable any integrations.


The subsequent [adversarial review resolution](adversarial-review-resolution.md)
records counted truncation, startup prompt validation and offline checks, explicit
proxy/CA environment trust, and the clarified verbatim archive policy.


## Direction review sequencing

The review of 81aa667 confirms the small-core/plugin direction. Next work is:

1. Reject unknown session/message request fields and improve safe config-load
   diagnostics before extending the API.
2. Finish phase 6 locally: extraction status, safe summary-only compaction,
   archive recovery, bounded context, and an explicit scratch-memory probe.
3. Build phase 7/8 behind disabled-by-default integration settings, using mocks.
   Local implementation may proceed after phase 6's automated checks; a live
   Discord pilot and live jobs must wait for the deliberate OpenViking probe
   and the complete phase 9 acceptance suite. Do not label mock checks live.
4. Keep SPEC, behavior, tests, and operator documentation in the same commits.

Retain existing documented MVP decisions: no Discord replay after downtime,
ignore non-trigger authors entirely, shell maintenance-only, blog publishing
capability separated from filesystem writes. Revisit these explicitly before
production configuration. Hermes and its schedules stay untouched.

## Operator UI

The supplied UI brief authorizes [P1 sessions/chat/status](10-ui-p1.md), now locally
implemented. [P2 evidence](11-ui-p2-evidence.md) and
[P3 operations](12-ui-p3-operations.md) have separate deferred plans. They do not
advance existing live-integration gates. Markdown/profile selection and shared
UI/Discord session archival remain separate v2 decisions.

[13: session archival](13-session-archival.md) adds operator API/UI archival and
read-only numbered archive browsing, preserving bindings and existing evidence.
This supersedes the earlier UI deferral for these surfaces only; Discord archival
exposure, prompt/run inspectors, markdown and profile selection remain deferred.

## Tool boundary direction

The user-directed replacement for phase 9 is now recorded in
[14: external tool packs and services](14-tool-pack-boundary.md),
[15: generic tools/runtime](15-generic-tools.md), and
[16: journal pack](16-journal-pack.md), locally implemented. Remaining work follows
[17: letters](17-letters-pack.md), [18: email](18-email-pack.md), then
[19: printer and later seam proofs](19-printer-and-seam-provers.md).
Domain behavior is independently packaged; core loop changes are not part of
adding a pack. Live acceptance and pilot gates remain unchanged.
