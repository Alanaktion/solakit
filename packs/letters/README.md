# Letters pack

`solakit-packs-letters`, loaded as `sola_packs.letters`; install alongside core
and `solakit-packs-common`. See [pack setup](../README.md) and the sidecar example.

Tools:

- `letters.pull_new(resource_name="default", after=-1, limit=20)`: clean fast-forward pull,
  then peer letters after the given global sequence number, earliest first.
  The default includes letter 000 if the repository starts at zero.
- `letters.read(number, resource_name="default")`: read an existing letter with explicit
  external-content markers. Source files are not marked, edited or renamed.
- `letters.reply(number, body, resource_name="default")`: pull current state, create the
  next `NNN-YYYY-MM-DD-author.md`, commit that file and push. `number` is required
  and identifies the existing peer letter being replied to. One immutable reply
  per peer letter. Dates use the core configured timezone; numbering is global
  across authors, with a minimum width of three digits.
- `letters.send(body, resource_name="default")`: publish a new letter without a
  reply target, automatically choosing the next global number. No peer letter
  needs to exist. The result has `reply_to: null`; `{reply_to}` in a custom layout
  expands to an empty string. Both send and reply require the `send` and `push`
  effects, repository write access, and the named letters resource grant.

Omitting `resource_name` selects the `letters.default` sidecar resource. The
profile must still grant `letters:default`; other aliases require an explicit
`resource_name` (including `correspondence` in the example below).

Verify the actual repository uses these conventions before live wiring. Peer and
local author identifiers are configurable lowercase tokens; layout is pack data
(default `{body}\n`). Replies never overwrite. Duplicate existing sequence numbers
are an error. A resource lock serializes numbering across simultaneous calls.
Repeated identical requests return the published receipt, including new letters
with the same exact body and resource settings. A retry never allocates another
number, even after later letters. New-letter receipts use a separate namespace
from existing reply receipts, which remain valid. Changing an existing
reply request is refused. Interrupted/failed publication preserves the local file
and commit if created, and blocks automatic recreation/replay until inspected.
No implicit force-push, rebase, clone, or changes to past letters.

Example core configuration (the sidecar defines `letters.correspondence`):

```toml
enabled_tools = ["sola_packs.letters"]

[repositories]
letters = "/absolute/path/to/letters"

[profiles.letters]
tools = ["letters.pull_new", "letters.read", "letters.reply", "letters.send"]
workspace = ["repo:letters"]
write = "workspace"
effects = ["send", "push"]
resources = ["letters:correspondence"]
```

Pull needs repository write access because it changes the checkout. A read-only
profile can grant only `letters.read`, `workspace=["repo:letters"]`, `read="workspace"`, `write="none"`, and the
`letters:correspondence` resource. Do not grant generic file edit/write or shell tools to
the letters profile if its never-edit rule is to hold. All actual writes still
pass the shared filesystem/Git services, with the existing upstream and empty
index requirements.
