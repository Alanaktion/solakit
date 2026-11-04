# Email pack — after letters

Status: planned. Separate package and pack-owned named mailbox configuration.

Search/read/reply by stable mailbox/message ID. Credentials remain secret
references. Apply external-content markers to subject/body/attachments and enforce
existing-thread reply semantics without accepting arbitrary SMTP envelopes.

Use mocked IMAP/SMTP or a scratch mailbox to verify threading, limits, uncertain
send/retry handling and credential redaction. No filesystem writes in the inbox
profile. Add reusable generic resource/client seams only if needed; no email
branches in core execution and no live mail sent by default.
