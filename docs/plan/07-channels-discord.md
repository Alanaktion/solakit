# Phase 7: Channel interface and Discord pilot readiness

Status: local implementation complete — live pilot gated

Depends on: phase 6

- Implement the adapter protocol and shared inbound/outbound delivery path.
- Add Discord channel/author/mention gating, parent-session routing for threads, typing indicators, reconnect behavior, and liveness reporting.
- Deliver final messages from the stored transcript without appending announcement duplicates.
- Support final, artifact, and silent delivery; surface `tool_error` failures in their originating channel.
- Define and test handling for oversized code blocks under Discord’s message limit.

**Exit check:** Mock Discord tests prove pinning, continued context, ignored authors/bots, delivery identity, and adapter lifecycle. Live pilot starts only after the complete §14 acceptance suite passes in phase 9.

## Completion record

Implemented the shared adapter interface, trigger gating, parent-thread session
routing with replies in the originating thread, typing lifecycle, discord.py
reconnect, connected/failed liveness, stored-message final/artifact/silent delivery,
whole fenced-block handling, mention suppression, and durable send evidence.
Tests use mock adapters and gateway lifecycle, including continued context,
independent sessions, artifact scope checks, silent runs and duplicate-send refusal.
Live Discord is disabled and requires live_ready plus a bot-token reference.
The real gateway pilot remains gated by live memory and phase 9 acceptance.

## Revised release gates

Phase 6's automated compaction/recovery suite precedes implementation. The live
pilot additionally requires a successful deliberate OpenViking round-trip and
phase 9 acceptance. Adapter activation remains explicit and disabled by default.
Keep platform details out of prompt assembly. Use stored final transcript text,
persist delivery outcomes, and test liveness, gating, parent routing, oversized
code blocks, and job-to-conversation continuity before connecting a real bot.
