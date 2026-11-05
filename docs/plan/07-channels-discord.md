# Phase 7: Channel interface and Discord pilot readiness

Status: partial — implementation complete; live gateway/pilot verification remains.

## Audit checklist

- [x] Adapter/routing/delivery implementation and mock regression checks below.
- [x] Prerequisite live OpenViking round-trip confirmed by the operator.
- [ ] Complete the remaining [acceptance checks](09-domain-tools-verification.md).
- [ ] Deliberately validate a real gateway: author/mention gating, reconnect,
  independent channel/thread sessions, stored delivery and oversized attachments.
- [ ] Complete the one-week low-stakes pilot required by SPEC §15.

The operator's general functionality report does not establish a Discord pilot.

Prerequisite: phase 6 implementation and live round-trip (complete).

- [x] Implement the adapter protocol and shared inbound/outbound delivery path.
- [x] Add Discord channel/author/mention gating, parent-session routing for threads, typing indicators, reconnect behavior, and liveness reporting.
- [x] Deliver final messages from the stored transcript without appending announcement duplicates.
- [x] Support final, artifact, and silent delivery; surface `tool_error` failures in their originating channel.
- [x] Define and test handling for oversized code blocks under Discord’s message limit.

**Exit check:** Mock Discord tests prove pinning, continued context, ignored authors/bots, delivery identity, and adapter lifecycle. Live pilot starts only after the complete §14 acceptance suite passes in phase 9.

## Completion record

Implemented the shared adapter interface, trigger gating, parent-thread session
routing with replies in the originating thread, typing lifecycle, discord.py
reconnect, connected/failed liveness, stored-message final/artifact/silent delivery,
whole fenced-block handling, mention suppression, and durable send evidence.
Tests use mock adapters and gateway lifecycle, including continued context,
independent sessions, artifact scope checks, silent runs and duplicate-send refusal.
Discord is disabled by default and requires explicit channel enablement plus a
bot-token reference. The operator confirmed working live DM and server replies
on 2026-11-04; reconnect, oversized delivery and the full pilot remain unverified.
The real gateway pilot remains gated by phase 9 acceptance; the memory round-trip
prerequisite is now operator-confirmed.

## Revised release gates

The automated compaction/recovery prerequisite is complete. The live pilot
requires phase 9 acceptance. The deliberate OpenViking round-trip is
operator-confirmed; do not repeat it merely to update documentation. Adapter activation remains explicit and disabled by default.
Keep platform details out of prompt assembly. Use stored final transcript text,
persist delivery outcomes, and test liveness, gating, parent routing, oversized
code blocks, and job-to-conversation continuity before connecting a real bot.
