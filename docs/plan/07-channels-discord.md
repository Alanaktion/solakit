# Phase 7: Channel interface and Discord pilot readiness

Status: planned

Depends on: phase 6

- Implement the adapter protocol and shared inbound/outbound delivery path.
- Add Discord channel/author/mention gating, parent-session routing for threads, typing indicators, reconnect behavior, and liveness reporting.
- Deliver final messages from the stored transcript without appending announcement duplicates.
- Support final, artifact, and silent delivery; surface `tool_error` failures in their originating channel.
- Define and test handling for oversized code blocks under Discord’s message limit.

**Exit check:** Mock Discord tests prove pinning, continued context, ignored authors/bots, delivery identity, and adapter lifecycle. Live pilot starts only after the complete §14 acceptance suite passes in phase 9.

## Completion record

Record implementation, validation, and remaining limitations here before marking complete.
