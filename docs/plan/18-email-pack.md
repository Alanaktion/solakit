# Email pack

Status: locally implemented in `packs/email`; live provider/scratch check pending.

Search/read/reply by stable mailbox/message ID. Credentials remain secret
references. Apply external-content markers to subject/body/attachments and enforce
existing-thread reply semantics without accepting arbitrary SMTP envelopes.

Use mocked IMAP/SMTP or a scratch mailbox to verify threading, limits, uncertain
send/retry handling and credential redaction. No filesystem writes in the inbox
profile. Add reusable generic resource/client seams only if needed; no email
branches in core execution and no live mail sent by default.

## Implementation record

`sola_packs.email` supplies literal search, bounded BODY.PEEK reads and sender-only
replies. IDs include UIDVALIDITY; source-derived thread/envelope headers cannot
be replaced by arbitrary model SMTP arguments. TLS/STARTTLS is mandatory. Secrets
register with the runtime redactor; the inbox profile has no filesystem write
grants. Private append-only receipts are internal evidence, not model filesystem
permission. Identical source/body deliveries deduplicate across operation IDs;
ID reuse with changed content is refused. Known pre-send/rejected attempts can
retry; pending, uncertain or torn evidence cannot silently replay.

Mock checks cover TLS/STARTTLS, stable IDs, marker injection, thread headers,
recipient derivation, oversized messages, credential file modes/redaction,
permission denials, duplicate and failed sends, and cancellation through receipt
completion. SMTP success means acceptance, not confirmed recipient delivery. No
live mailbox has been contacted and no live message has been sent.
