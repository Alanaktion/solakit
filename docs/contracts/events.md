# Session event transport

`GET /api/v1/sessions/{session_id}/events` is authenticated SSE. Cookie clients use
the normal login cookie; bearer clients use a streaming fetch with Authorization.
No URL token path exists. A connection revalidates expiry/revocation every poll;
`auth.expired` ends a stale connection. The same operator can inspect every topic,
including private execution evidence; model-facing session isolation is unchanged.

Every connection begins with a bounded `snapshot`, or a `reset` if its cursor is
outside the replay window. Its data contains the topic, newest 50 visible message
ID/sequence references, newest 50 run ID/request/state references, the current
high-water cursor, and `replace_previews=true`. Fetch message/record pages for
content; a snapshot is not a history page. Snapshot construction has no await and
runs on the one owner loop, so admission cannot interleave its state reads.

Durable SSE IDs are `session_id:sequence`; they are scoped to one topic. A foreign,
negative or malformed cursor fails with 422. Use Last-Event-ID or the `cursor`
query; if both are present they must agree. Without a cursor, start at the current
snapshot high-water mark. With a valid cursor, the initial snapshot's SSE ID is
the requested cursor, then replay events strictly after it. The snapshot data
still describes current state. A future cursor resets to the current high-water.

Durable frames use their existing type (run.admitted, run.started, run.terminal,
message.appended, model.requested/recorded, tool.intent/receipt, memory.changed,
delivery.changed, compaction.recorded, recovery.noted, image.changed) and carry a DurableEvent:

```text
id: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:8
event: run.terminal
data: {"event_id":"…","session_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sequence":8,"type":"run.terminal","payload":{"kind":"run","record_id":"…"},"…":"…"}

```

Payloads invalidate/reference current records; they do not inline full artifacts
or append duplicate messages. Render records by stable ID. Deduplicate replay by
session sequence/event_id. A terminal run event is immutable and never recreated
by reconnecting; an old cursor can intentionally replay the same event. A snapshot
may already reflect a replayed event, so event handling must refresh/replace
current state instead of appending historical copies.

JSONL currently retains all durable events. Network replay is deliberately bounded
to 1000 sequences behind current state, in batches of 100. An older cursor, a
missing sequence (including an internal batch gap), or retention loss emits reset
with a fresh snapshot/high-water cursor. Replace visible state, discard previews,
and page history as needed; do not infer lost event transitions. This same reset
bounds catch-up for a slow client. Future pruning must retain this behavior.

`preview` has **no SSE ID**, no durable sequence and no replay guarantee. It carries
session_id, run_id, model-step index, a process-local version, replaceable text and
a truncation flag. The text is the newest <=8192 UTF-8 bytes of the current step.
Only interactive conversation content is previewed; reasoning, compaction and
background work are excluded. Replace the prior preview for that run/step; clear
it on model.recorded, terminal state, snapshot or reset. A preview is provisional,
including before eventual model validation/failure. Reconnection recovers durable
records, not unrecorded tokens. Current topic state always takes precedence.

One shared preview map keeps at most eight entries; consumers sample it, with no
per-client token queue and no await in the model callback. SSE polls at 100 ms
when caught up. Idle connections send `: heartbeat` every 15 seconds. At most 16
streams are active, admitted before response headers. Each blocked ASGI send has
a 30-second deadline; timeout/disconnect explicitly closes the iterator and
releases its slot. One response holds one bounded replay batch/snapshot and at
most eight bounded previews. Slow readers never participate in the inference
permit or cancel durable runs. Process restart loses previews and connections;
reconnect using the last durable ID.
