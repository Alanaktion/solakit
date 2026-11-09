# Optional tool packs

Core distributions contain no pack code. Each directory is an independently
installable distribution using the `sola_packs` namespace. These directories can
move to a separate repository without changes to core execution.

- `blog`: Astro posts and dreams, optional generated covers, one-call publication.
- `journal`: dated, configured collections (posts, dreams, poems).
- `letters`: sequential immutable correspondence in a named Git repository.
- `email`: named TLS mailboxes, read/search, sender-only threaded replies, and opt-in
  allowlisted new mail, mark-read and move-to-trash.
- `printer`: named USB/serial 58mm ESC/POS text receipts with feed and send evidence.
- `calendar`: read-only named ICS agendas with bounded recurrence expansion.
- `image`: named ComfyUI API workflows, explicit seeds and non-replaying receipts.
- `common`: pack-owned config, grants, secret registration, operation evidence,
  optional contract-test fixtures.

Packs import core only through `solakit.sdk` (`tool`, `ToolContext`, `ToolError`,
`ToolResult`, `tool_json`, `durable`, `SecretRef`, filesystem/process/git helpers
and typed `ports`) and declare `SOLA_SDK = ">=1,<2"`; the registry refuses a pack
whose requirement the running `SDK_VERSION` does not satisfy, and a contract test
fails if a pack imports anything else from `solakit`. `docs/tools.md` is the
generated reference of every tool.

From the existing core development checkout:

```sh
uv sync --frozen --all-packages
uv run pytest tests packs/*/tests -q
```

The root and all eight packs share one uv workspace and lockfile. Sync installs
them together, including inter-pack dependencies, without manual ordering.
`uv run` keeps the synced members available; no `--no-sync` is needed.
Workspace membership does not add pack dependencies or code to the core wheel.
Plain `uv sync` selects only the root; use `--all-packages` for this checkout.
Standalone deployments outside the workspace still declare their desired pack
distributions alongside core. Installed packs load only through `enabled_tools`.

Set `SOLA_PACKS_CONFIG` to an operator-owned TOML file based on
[config.example.toml](config.example.toml) for letters/email/printer/calendar/image. Nothing reads or sends
mail, publishes letters, enables jobs or connects production resources simply because a pack is installed. It must also
be explicitly loaded in core `enabled_tools`, and tools/effects/resources granted to
profiles. These packs are trusted Python, not a sandbox for hostile package code.

Operation receipts live in the private data tree under `pack-operations/`.
They are append-only JSONL with request fingerprints, cross-process locks and
fsynced intent/result records. A torn/ambiguous record fails closed. Never delete
receipts to force a resend: inspect the actual remote/Message-ID and use an
operator-reviewed reconciliation or deliberate new request.

Shell has moved into core: enable `shell` or `solakit.tools.shell` without an
optional package. See [built-in shell and migration](../docs/operations/shell.md).

`effects` is a closed list: `push`, `send`, `print`, `shell`, `operate`.
`operate` authorizes job mutations/triggers and cross-profile/cross-session runtime
access. `resources` lists named pack aliases, for example `mailbox:personal`.
Resource read needs the alias; reply/send also needs `send`, print needs `print`, and
image generation needs a non-none write tier. Mailbox mark-read/delete need only the
alias plus the mailbox's sidecar opt-in. Core `channel:<id>` grants with `send` allow
`messages.send` to that configured channel. Mixed read/reply authority across
mailboxes requires separate profiles. Shell runs with host-user privileges; its
cwd check is not OS containment.
