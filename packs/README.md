# Optional tool packs

Core distributions contain no pack code. Each directory is an independently
installable distribution using the `sola_packs` namespace. These directories can
move to a separate repository without changes to core execution.

- `journal`: dated, configured collections (posts, dreams, poems).
- `letters`: sequential immutable correspondence in a named Git repository.
- `email`: named TLS mailboxes, read/search and sender-only threaded replies.
- `printer`: named USB/serial 58mm ESC/POS text receipts with feed and send evidence.
- `calendar`: read-only named ICS agendas with bounded recurrence expansion.
- `image`: named ComfyUI API workflows, explicit seeds and non-replaying receipts.
- `common`: pack-owned config, grants, secret registration, operation evidence,
  optional contract-test fixtures.

From the existing core development checkout:

```sh
uv sync --frozen --all-packages
uv run pytest tests packs/*/tests -q
```

The root and all seven packs share one uv workspace and lockfile. Sync installs
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
Resource read needs the alias; reply also needs `send`, print needs `print`, and
image generation needs a non-none write tier. Mixed read/reply authority across
mailboxes requires separate profiles. Shell runs with host-user privileges; its
cwd check is not OS containment.
