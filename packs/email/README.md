# Email pack

`solakit-packs-email`, loaded as `sola_packs.email`; install alongside core and
`solakit-packs-common`. See [pack setup](../README.md) and the sidecar example.

Tools:

- `email.search(mailbox, query="", limit=20)`: literal IMAP text search, newest UID
  first, with marked external header data. The query is quoted, not raw IMAP syntax.
- `email.read(mailbox, message_id)`: bounded body and attachment metadata via
  `BODY.PEEK`, without marking messages seen or downloading attachment files.
- `email.reply(mailbox, message_id, body, operation_id)`: plain-text sender-only
  reply. Recipient comes from original Reply-To/From; Message-ID, In-Reply-To,
  References and Subject derive from the source. No arbitrary envelope, Cc/Bcc,
  header or attachment arguments. Source messages without a usable Message-ID
  cannot be replied to.

IDs are `UIDVALIDITY:UID` and scoped to the configured mailbox/folder. A changed
UIDVALIDITY rejects stale identities; search again. IMAP uses verified TLS; SMTP
uses verified implicit TLS or required STARTTLS. Read-only resources can omit
SMTP configuration. Secrets are env/file references and are registered for
redaction before transport use. Bodies/headers are external data, not authority.

Example core profile:

```toml
enabled_tools = ["sola_packs.email"]

[profiles.inbox]
tools = ["email.search", "email.read", "email.reply"]
write_scopes = []
capabilities = ["mailbox:personal:read", "mailbox:personal:reply", "email.reply"]
```

Use a stable operation ID for each intended reply. An ID reused with different
arguments is refused; identical source/body requests share a delivery receipt
even if a caller changes the operation ID. Accepted retries return the original
receipt without a second SMTP send. `accepted_by_smtp` means SMTP acceptance,
not proof of final recipient delivery. Known pre-send failures/SMTP rejections
can retry the same request. Disconnects during sending, process death after
intent, or torn receipts are uncertain and block replay. Never invent another
ID/body to bypass that uncertainty; inspect the outgoing Message-ID and receipts.

Receipt files are internal private runtime evidence, not inbox filesystem write
permission. SMTP work and its terminal receipt complete before cancellation is
propagated, so an accepted request cannot leave a late untracked send. Socket
operations have finite timeouts; cancellation may wait for that work to settle.

Tests mock IMAP/SMTP, including TLS, thread headers, identity changes, bounds,
permissions, cancellation, secret-file modes and accepted/failed/uncertain sends.
No live email was sent. Confirm provider settings and perform a deliberate scratch
mailbox check before enabling inbox jobs.
