# Remaining implementation and verification plans

[SPEC.md](../../SPEC.md) defines behavior. The [implementation audit](../audits/implementation-status.md)
records shipped features, supporting tests, acceptance gaps and retired plans.
The [local integration audit](../audits/local-integration.md) records verified
API, model/tool, prompt evidence and archival checks against the running server.
Completed implementation plans have been removed; Git history retains their original
scope and completion records. Plan numbers stay stable.

The operator has confirmed the live OpenViking round-trip and reports that general
functionality looks good. This closes the original phase 6 live memory gate.
Run IDs/results were not supplied in this conversation; retain them in private
operator evidence. This does not establish live compaction, provider/device checks,
or the Discord/job rollout results.

## Active plans

| Plan | Implemented | Remaining |
| --- | --- | --- |
| [07: Discord](07-channels-discord.md) | Adapter, routing, gating, delivery, mock lifecycle checks and operator-confirmed live DM/server replies | Remaining acceptance scenarios, reconnect/oversized delivery and pilot validation |
| [08: scheduling](08-scheduling.md) | Persistent cursors, timezone schedules, controls, misfire/non-overlap checks | Live job and channel delivery validation; staged rollout |
| [09: acceptance](09-domain-tools-verification.md) | Core, services, journal/letters/email, shell, runner-driven journal and profile-isolation acceptance | Operator resource/prompt acceptance and rollout |
| [17: letters](17-letters-pack.md) | Independent pack, immutable numbering and durable receipts | Deliberate live correspondence check; actual repository protocol inspected |
| [18: email](18-email-pack.md) | TLS mailbox search/read/reply and send evidence | Scratch provider/threading/send verification |
| [19: printer and later extensions](19-printer-and-seam-provers.md) | USB/serial 58mm ESC/POS pack, grants, bounded text and durable receipts | Physical printer check; image/calendar remain later priorities |
| [21: compaction controls](21-compaction-controls.md) | Opt-in mid-run and explicit API/UI compaction, summary ordering | Live summary/continuation and browser checks passed; investigate remote tool-argument omission |
| [22: Markdown/streaming](22-ui-markdown-streaming.md) | Sanitized assistant Markdown, bounded model SSE and live UI snapshots | Targeted streaming browser verification; live oMLX text/tool SSE verified |

Partial plans list completed work separately from unchecked remaining work. General
operator approval is recorded without treating it as evidence for every named check.
No live service, mailbox, printer, job or Discord bot is activated by this audit.

## Working agreement

Synchronize main, implement the bounded scope, run its relevant exit checks plus
pytest, Ruff lint and format checks, update status, and push directly to main
without force. Keep integrations opt-in. Never mark a live gate complete from mocks.
Update the audit when removing a completed plan and repair all references.

## Architecture and rollout rules

- Keep domain behavior in separately installed packs; no domain branches in core.
- Load modules explicitly; config grants tools, filesystem scopes and capabilities.
- Preserve exact prompts and append-only evidence; redact diagnostics/UI secrets.
- Recall precedes prompt persistence; ambiguous writes/sends never auto-replay.
- Keep declarative schedules in TOML and durable cursor state outside the index.
- Complete SPEC §14 acceptance before the Discord pilot, then follow §15's prompt
  audit, one-week pilot, shadow publishing, individual jobs and rollback stages.
- Hermes and its schedules remain untouched by this work.
