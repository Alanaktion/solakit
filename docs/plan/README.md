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
  separately and must be retried with a stable idempotency key.
- Model identity matches the configured ID exactly; no fallback.
- Reserved isolated memory must fail closed until scope support exists.

## Rollout

Implementation is separate from production cutover. Complete SPEC §14 before
starting the Discord pilot. Then follow §15: operator prompt audit, one-week
pilot, shadow blog publishing, jobs individually, and a month rollback window.
Hermes remains untouched. No production deployment is part of this plan run.


## Current checkpoint

Phases 1–5 are implemented and pushed to main. Phase 6 has opt-in recall/commit
and memory tools with mock verification; compaction, extraction completion
tracking, and validation against the existing instance remain open. Phases 7–9
remain planned. The full acceptance suite and production rollout have not passed.
Resume at phase 6's remaining-work checklist. All integrations remain opt-in.


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
