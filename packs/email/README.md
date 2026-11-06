# Email pack

`solakit-packs-email`, loaded as `sola_packs.email`; install alongside core and
`solakit-packs-common`. See [pack setup](../README.md) and the sidecar example.

Tools:

- `email.list_mailboxes()`: discover only granted aliases, addresses, folders and
  actions, without connecting or resolving credentials. Grant this tool explicitly.
- `email.search(mailbox, query="", limit=20, unseen_only=false)`: literal IMAP text
  search, newest UID first. Set `unseen_only=true` for unread checks; combines
  `UNSEEN` with literal `TEXT` when a query is supplied. Returns marked external
  headers, `seen`, `total_matching`, `has_more`, mailbox alias, address and folder.
  The query is quoted, not raw IMAP syntax. Search and reads preserve unread state.
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

For report-only scheduled inbox checks, use a dedicated profile instead of a broad
interactive profile:

```toml
[profiles.inbox-check]
tools = ["email.list_mailboxes", "email.search", "email.read"]
capabilities = ["mailbox:personal:read"]
write_scopes = []
max_steps = 25
```

Name the alias in the task and operator-owned profile fragment. For example:
“Check unread mail in `personal`. Start with email.search(mailbox="personal",
unseen_only=true, limit=20). Read relevant bodies with each result's `id`, never
the Message-ID header. Summarize briefly; if more results remain, say the report
is partial. If no matches, report no unread mail. On failure report it and stop.”
For a job with channel delivery, add “Your final response is the announcement;
the runtime delivers it.” Do not grant reply, shell or filesystem tools to this
report-only profile. Reads preserve unread state, so subsequent checks can report
the same unread messages until an external mail client marks them seen.
