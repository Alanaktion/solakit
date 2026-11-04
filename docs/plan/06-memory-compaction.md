# Phase 6: OpenViking integration and context compaction

Status: in progress — recall/commit implemented; compaction and live verification pending

Depends on: phase 5

- Add pre-turn recall and post-turn session commits through the existing OpenViking API.
- Route all memory access through a scope-aware client in `ToolContext`; MVP sessions use `shared`.
- Construct and persist the final prompt after recall, before inference.
- Add between-run compaction with archived transcript rotation, boundary records, and summary references.
- Make memory failures and commit failures visible; prevent retries from duplicating committed messages.

**Exit check:** A committed fact is recalled in a later run. Compaction preserves archived history and valid tool-call groups. Dry-run inspection remains free of external calls.

## Completion record

Implemented opt-in scope-aware OpenViking REST client, pre-turn recall before
prompt construction, post-turn session commits, memory tools, durable commit
intent journals, task/archive references, visible recall/commit failures, and
conservative duplicate prevention. Ephemeral sessions make no memory calls;
isolated scopes fail closed. The API remains disabled by default.

Validated with mock recall round-trip, denied URI/scope access, failed recall,
ambiguous-write replay refusal, retained conversation on failed commit, and CLI
byte-output tests. Full suite: 29 passing tests; Ruff lint and format pass.

## Remaining work before completing this phase

- Confirm the existing OpenViking instance URL, installed API version, canonical
  roots and secret reference. No such configuration is present in this checkout.
- Poll accepted extraction task IDs and surface terminal extraction failures.
  Upstream explicitly distinguishes archive acceptance from completed extraction.
- Reconcile ambiguous remote writes before any operator-triggered recovery;
  upstream message append has no documented idempotency key. Do not blindly retry.
- Implement between-run compaction: completed OpenViking summary, immutable
  transcript archive rotation, boundary/summary references, crash recovery, and
  preservation of complete assistant/tool groups. Do not substitute a local
  summary or silently discard history.
- Test archive/index recovery, repeated compaction, pending/failed summary tasks,
  and live committed-fact recall before marking the exit check complete.

API contract references reviewed during implementation:
[Sessions](https://github.com/volcengine/OpenViking/blob/main/docs/en/api/05-sessions.md),
[Retrieval](https://github.com/volcengine/OpenViking/blob/main/docs/en/api/06-retrieval.md),
[Content](https://github.com/volcengine/OpenViking/blob/main/docs/en/api/12-content.md).

## Revised implementation gates

Use completed, OpenViking-generated summaries only. Summary-only remote sessions
must disable long-term extraction, so compaction does not re-extract the entire
history. Preserve every original transcript in numbered archives, journal the
rotation before it starts, and recover interrupted rotations at startup. Include
whole assistant/tool groups. Refuse oversized requests when compaction cannot
complete; never silently send unbounded context or estimate usage as real tokens.

Provide explicit extraction refresh and scratch round-trip operations. The live
probe must verify recalled content in the next run's persisted prompt, not merely
trust a model's answer. Pending/failed/ambiguous extraction is visible. Do not
blindly replay memory writes. Live instance configuration is still required.
