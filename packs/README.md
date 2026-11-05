# Optional tool packs

Core distributions contain no pack code. Each directory is an independently
installable distribution using the `sola_packs` namespace. These directories can
move to a separate repository without changes to core execution.

- `journal`: dated, configured collections (posts, dreams, poems).
- `letters`: sequential immutable correspondence in a named Git repository.
- `email`: named TLS mailboxes, read/search and sender-only threaded replies.
- `shell`: maintenance-only host-user command execution.
- `common`: pack-owned config, grants, secret registration, operation evidence,
  bounded process execution and optional contract-test fixtures.

From the existing core development checkout:

```sh
uv sync --frozen
uv pip install --no-deps -e packs/common -e packs/letters -e packs/email -e packs/shell
uv pip install --no-deps -e packs/journal
uv run --no-sync pytest tests packs/journal/tests packs/letters/tests packs/email/tests packs/shell/tests -q
```

Core sync does not manage these optional dependencies. Production/application
environments should declare desired pack wheels alongside the core in their own
requirements, including `solakit-packs-common` for letters/email/shell. Use
`uv run --no-sync sola serve` for the manually installed development environment.

Set `SOLA_PACKS_CONFIG` to an operator-owned TOML file based on
[config.example.toml](config.example.toml) for letters/email. Shell needs no
resource file. Nothing reads or sends mail, publishes letters, enables jobs or
connects production resources simply because a pack is installed. It must also
be explicitly loaded in core `enabled_tools`, and tools/capabilities granted to
profiles. These packs are trusted Python, not a sandbox for hostile package code.

Operation receipts live in the private data tree under `pack-operations/`.
They are append-only JSONL with request fingerprints, cross-process locks and
fsynced intent/result records. A torn/ambiguous record fails closed. Never delete
receipts to force a resend: inspect the actual remote/Message-ID and use an
operator-reviewed reconciliation or deliberate new request. Mock/scratch checks
do not satisfy live rollout gates.
