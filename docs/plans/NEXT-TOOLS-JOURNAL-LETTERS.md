# Next tools: journal + letter writing on main-v2

The [current letters plan](NEXT-TOOLS-LETTERS.md) replaces the letters proposal
in this historical note. The [blog and dream tools](../blog.md) landed after
this note was written. The v1 journal reference below still describes its
general-purpose collection workflow.

Alan 2026-11-18 (06:31 UTC, side chat "Solakit main-v2 needs more tools"): main-v2 is
basically done, he'll use it as-is for a bit, and "mostly just needs journal and
letter writing". This is the porting surface for those two, mapped from the v1 packs
to the v2 code as it stands at HEAD 4c97f37. It is a map, not a build: Alan writes
v2 himself, and the open decisions at the bottom are his.

## Where the v1 behaviour lives

- Journal pack: `~/workspace/solakit-review/repo/packs/journal/` — README + 
  `src/sola_packs/journal.py`, tests in `packs/journal/tests/`.
- Letters pack: `~/workspace/solakit-review/repo/packs/letters/` — README +
  `src/sola_packs/letters.py`.
- Both are independently installable packages outside `src/solakit`; core never
  imports them. v1 AGENTS.md invariant: domain behaviour belongs in packs, no
  blog/letters/email/printer modules under `src/solakit`, no domain imports or
  branches in core execution.

## Journal — 3 tools

`journal.create`, `journal.list_recent`, `journal.publish`
(`packs/journal/src/sola_packs/journal.py`, @tool at lines 26, 103, 142).

- Collections are configured per kind (dreams, poems, posts differ only by
  config): a repository + directory + filename pattern `{date}-{slug}.md`, with a
  front-matter layout template. `create` takes slug (lowercase hyphenated),
  title, body, optional `YYYY-MM-DD` date; it never overwrites. Listing is by
  file modification time.
- `publish` takes collection-relative filenames + a commit message, stages only
  those entries, one commit, then pushes. It refuses a staged index,
  pre-existing unpublished commits, or an upstream known to be ahead. No
  force-push, implicit rebase, auto-retry, or rollback; on push failure the
  local commit remains and the tool reports its identity.
- Config shape (v1): `enabled_tools` includes `sola_packs.journal`;
  `[collections.dreams]` with repository/directory/filename; profile grants
  `tools = ["journal.create", "journal.list_recent", "journal.publish"]`,
  `workspace = ["collection:dreams"]`, `write = "workspace"`, `effects = ["push"]`.

## Letters — 4 tools

`letters.pull_new`, `letters.read`, `letters.reply`, `letters.send`
(`packs/letters/src/sola_packs/letters.py`, @tool at lines 114, 165, 204, 245).

- `pull_new(after, limit)`: clean fast-forward pull, then peer letters after a
  global sequence number, earliest first. `read(number)`: read one letter, with
  explicit external-content markers; source files are never marked, edited, or
  renamed. `reply(number, body)`: pull, create the next `NNN-YYYY-MM-DD-author.md`,
  commit, push — one immutable reply per peer letter. `send(body)`: new letter
  with no reply target, next global number.
- Numbering is global across authors (min width 3 digits); a resource lock
  serializes numbering; repeated identical requests return the published receipt
  and never allocate a second number. Changing an existing reply request is
  refused; interrupted publication preserves the local file/commit and blocks
  automatic replay until inspected. No force-push, rebase, clone, or edits to
  past letters.
- Profile grants (v1): `tools` = the four verbs, `workspace = ["repo:letters"]`,
  `write = "workspace"`, `effects = ["send", "push"]`, `resources =
  ["letters:correspondence"]`. The README's warning matters: do NOT grant
  generic file edit/write or shell to the letters profile, or the never-edit
  rule for past letters can't hold — read-only profiles grant only
  `letters.read` with `write = "none"`.

## What v2 already has, and the gaps

- v2 tool pattern (Phase 5, `repo-main-v2/src/solakit/tools.py`): one Pydantic
  args class, one async function, one `Tool(name, description, Args, fn)` line.
  Current v2 tools: `files.read/write/list/search`, `transcripts.search`,
  `runtime.status`, `memory.recall`, `memory.remember`, `jobs.*`,
  `generate_image`. The resident eager set is 5 tools.
- `journal.create` / `list_recent` are nearly covered by the existing file
  tools plus a collection-scoped wrapper: create is a guarded write into one
  directory with a fixed filename pattern and a never-overwrite rule; listing
  is files-by-mtime. The new behaviour is the collection scoping and the
  front-matter layout, not new I/O.
- The real gap is git. Neither pack's publish/send path works without it, and
  v2 has no git service or git tool yet — no `repo_git`, no push effect in the
  v2 tool set. `journal.publish`, `letters.reply`, `letters.send`, and
  `letters.pull_new` (pull changes the checkout, so it needs repo write too)
  all wait on that. Phase 12's precedent (V2-36) is one optional built-in
  adapter/service for the fixed image workflow, with external packs out of the
  core wheel — the same shape would fit a fixed git adapter, if that's the
  direction.
- V2-10 / `docs/product.md`: domain packs for letters, blog, email, journal,
  calendar, and printer are deferred, and "no domain logic belongs in the run
  loop". So these land as tools/adapters, not as pack reactivation, unless
  Alan reopens the packs question.

## Decisions for Alan (not made here)

1. Git first, or file-only first? `journal.create` + `list_recent` and
   `letters.read` need no git; everything that publishes does. A file-only
   first cut would already cover Sola drafting dreams/letters, with publish
   as the follow-up.
2. Resident or deferred? Seven new verbs would grow the eager schema set well
   past the 5-tool resident set that replaced v1's ~24k characters of schemas
   (Phase 5). Journal/letters are daily-use for the interactive profile, so
   resident is plausible — but that reopens the prompt-size question Phase 6
   measured, and the deferred-disclosure gate stays failed/disabled.
3. Letters profile scoping: in v2 the guard is the workspace root + write tier,
   not v1's `collection:`/`repo:` grants. The never-edit rule needs the letters
   profile to reach only the letters repo, with no generic files.write outside
   it — worth confirming the v2 grant model can express that before landing
   `letters.reply`/`send`.

## Sources

- v1 packs: `repo/packs/journal/README.md`, `repo/packs/letters/README.md`,
  `repo/packs/journal/src/sola_packs/journal.py`,
  `repo/packs/letters/src/sola_packs/letters.py`
- v2: `repo-main-v2/src/solakit/tools.py`, `repo-main-v2/docs/product.md`,
  `repo-main-v2/docs/decisions.md` (V2-10, V2-36), `repo-main-v2/docs/plans/PHASE-12-packs-workflows.md:14`
- Alan's priority: 2026-11-18T06:31:52Z, "I'll use it as-is for a bit, mostly
  just needs journal and letter writing."
