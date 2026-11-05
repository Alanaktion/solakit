# Session archival — API and UI

Status: partial — implementation complete; targeted browser verification remains.

## Audit checklist

- [x] API/UI archival and read-only browsing implemented; durability, concurrency,
  byte-preservation and authenticated HTTP checks pass.
- [x] Operator reports general functionality looks good.
- [ ] Check archive confirmation/history navigation, read-only composer absence and
  light/dark/narrow-screen rendering in a real browser.

General approval is not recorded as completion of the specific browser matrix.

Authorized follow-up scope: operator archival and archive browsing in API/UI.
Discord slash-command exposure remains deferred; it can call the same execution
operation later. No new model/job tool grants are introduced.

- `POST /api/sessions/{id}/archive` requires existing cookie/Bearer auth.
- The operation rejects queued/running session turns or runtime shutdown with
  HTTP 409. It holds the shared submission/admission lock across durable rotation,
  including request cancellation, so a new turn cannot race an unfinished write.
- Active messages rotate byte-for-byte to the next numbered immutable archive.
  Manual archival and compaction share numbering and rotation/recovery machinery.
  Prepared hashes/manifests recover on startup; unresolved manual rotation blocks
  active reads/writes until recovery. No history or existing archive is deleted.
- Session metadata, ID, profile, memory scope and channel binding are unchanged.
  Empty active transcripts are a no-op. A manual archive manifest records the
  new context boundary without synthesizing a message for the model.
- The next turn sees an empty transcript plus the normal prompt and scoped memory
  recall. Old memory remains available. Archival itself invokes neither model nor
  memory and does not wait for or retry extraction from previous turns.
- Chat offers an explanatory confirmation, POST archive action and history picker.
  Numbered archives are read-only and reuse escaped/credential-masked rendering
  and tool details from redacted run logs. Live chat remains available in the
  same session. No rename, delete, undo, or archive editing operations are added.
- Regression coverage: byte preservation, binding/metadata continuity, fresh model
  context, empty retry, numbering with compaction, no memory writes, busy rejection,
  cancelled HTTP-operation safety, crash recovery, damage refusal, API auth and
  read-only UI/secret masking/tool evidence.

Browser visual QA is separate from automated HTTP checks and remains pending in
this workspace. Existing live-integration acceptance gates are unchanged.
