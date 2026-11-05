# Letters pack

`solakit-packs-letters`, loaded as `sola_packs.letters`; install alongside core
and `solakit-packs-common`. See [pack setup](../README.md) and the sidecar example.

Tools:

- `letters.pull_new(resource_name, after=-1, limit=20)`: clean fast-forward pull,
  then peer letters after the given global sequence number, earliest first.
  The default includes letter 000 if the repository starts at zero.
- `letters.read(resource_name, number)`: read an existing letter with explicit
  external-content markers. Source files are not marked, edited or renamed.
- `letters.reply(resource_name, number, body)`: pull current state, create the
  next `NNN-YYYY-MM-DD-author.md`, commit that file and push. One immutable reply
  per peer letter. Dates use the core configured timezone; numbering is global
  across authors, with a minimum width of three digits.

Verify the actual repository uses these conventions before live wiring. Peer and
local author identifiers are configurable lowercase tokens; layout is pack data
(default `{body}\n`). Replies never overwrite. Duplicate existing sequence numbers
are an error. A resource lock serializes numbering across simultaneous calls.
Repeated identical requests return the published receipt; changing an existing
reply request is refused. Interrupted/failed publication preserves the local file
and commit if created, and blocks automatic recreation/replay until inspected.
No implicit force-push, rebase, clone, or changes to past letters.

Example core configuration (the sidecar defines `letters.correspondence`):

```toml
enabled_tools = ["sola_packs.letters"]

[repositories]
letters = "/absolute/path/to/letters"

[profiles.letters]
tools = ["letters.pull_new", "letters.read", "letters.reply"]
write_scopes = ["repo:letters"]
capabilities = ["letters:correspondence:read", "letters:correspondence:reply", "letters.reply", "git.push"]
```

Pull needs repository write access because it changes the checkout. A read-only
profile can grant only `letters.read`, `read_scopes=["repo:letters"]`, and the
resource read capability. Do not grant generic file edit/write or shell tools to
the letters profile if its never-edit rule is to hold. All actual writes still
pass the shared filesystem/Git services, with the existing upstream and empty
index requirements. Production jobs remain disabled pending live acceptance.

Tests use a local bare remote: content preservation, markers, numbering,
concurrent replies, idempotency, conflicting request denial, duplicate sequences,
failed push and torn evidence. No actual correspondence was published.
