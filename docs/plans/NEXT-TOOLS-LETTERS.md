# Git-backed letters

Add correspondence for one existing checkout. Sola can check for peer letters,
read a numbered letter, start a new letter, and reply to a specific peer letter.
Reuse the checkout checks, lock, and command runner in
[`src/solakit/git.py`](../../src/solakit/git.py) and the existing
[tool contract](../contracts/tools.md). The letters service owns the workflow
below and calls the Git helpers directly under its own authority and lock.

## Tool calls

| Tool | Arguments | Result |
| --- | --- | --- |
| `letters.inbox` | `after=-1`, `limit=20` | Fast-forwards the checkout, then returns `letters`, `next_after`, and `has_more`. Each row includes number, date, author, Markdown body, and `complete`. |
| `letters.read` | `number` | Reads one letter of either author from the local checkout. Returns number, date, author, Markdown body, and `complete`. |
| `letters.send` | `body` | Starts a new letter. Returns its number, repository-relative path, and commit. |
| `letters.reply` | required `reply_to`, `body` | Answers the specified peer letter. Returns its number, repository-relative path, commit, and `reply_to`. |

Describe `inbox` as a checkout update and `read` as a local read. Describe
`send` as starting a new letter, and `reply` as answering a peer letter using
its number from `inbox` or `read`. Reject missing or null `reply_to` on
`reply` and unknown arguments on both publication tools. Checking the inbox
leaves read tracking to the caller. Treat all returned Markdown as external
content.

`after` is an exclusive cursor in the shared numbering sequence. Validate it
as an integer ≥ −1 and `limit` as an integer from 1 through 100, retaining
v1's default of 20. Page in increasing number order across both authors,
skipping local letters. Advance `next_after` to the highest number consumed,
including skipped local letters and trailing local letters at the end of the
scan. Keep `after` when no numbers were consumed. Stop before the first peer
letter that would exceed the page limit or remaining byte budget. That
unreturned letter determines `has_more` and stays beyond the cursor.

Bound the whole inbox JSON result to the smaller of 64 KiB and the effective
`tool_result_bytes`. Measure the canonical redacted output, including JSON
escaping, external-content framing, and pagination fields. Include complete
bodies when they fit. When the first peer letter alone exceeds the budget,
include its metadata and a UTF-8-safe body
prefix with `complete: false`, then advance past it. If even that row's metadata
cannot fit, return a budget error without advancing. `read` uses the same
budget-aware body handling for one letter, with the profile's result budget
available to it. Read bodies with bounded UTF-8 I/O and keep the result
structured when a body is shortened.

## Configuration and authority

Extend the existing v2 `[letters]` checkout reservation. Use the same checkout
path resolution as `[blog]`, with one optional integration:

```toml
[letters]
repository = "~/workspace/letters"
branch = "main"
directory = "."
author = "sola"
peer = "dingus"
```

`branch` is both the required checked-out local branch and the branch fetched
from and pushed to `origin`. A detached HEAD or different local branch fails
before I/O with the remote. The tool leaves branch selection to the operator.
`directory` defaults to the checkout root and must be repository-relative.
Reject parent traversal, Git metadata components, and symlinked directory
components. Require the repository, branch, and both authors to enable tools.
An existing reservation with only `repository` still reserves the checkout.

Use `[defaults]` and `[profiles.<name>]` workspace roots and named tool grants.
A dedicated interactive profile can use all four tools. An operator-created
scheduled job can use `letters.inbox` and `letters.read`, with explicit job
`workspace_roots` covering the checkout for the inbox update. `inbox` is
consequential and follows normal job effect and quiet-hour checks.
`letters.send` and `letters.reply` are interactive-only. Omit generic writes,
shell, and generic Git mutations from the letters profiles. Letters tools
create no wakes.

Gate eager disclosure and dispatch on configured letters, the named grant,
origin, and the checkout being inside both admitted and current workspace
roots. Freeze the resolved letters configuration with admission, default older
admissions to disabled, and recheck after taking the checkout lock and at
guarded I/O. Require one operator-trusted checkout with private Git metadata,
using the existing adapter's checkout validation and noninteractive Git
environment.

Reserve the entire repository by canonical checkout identity, including path
aliases. Use a separate repository for blog and dream content. Blog publish
sweeps pending content in its repository, so a different directory in the
same checkout is insufficient. Deny generic Git and blog/dream mutations for
the reserved checkout, and deny generic guarded writes into it. Git
inspection may still be granted. The shared checkout lock serializes operations
that are permitted on that checkout.

## File format and validation

- Scan only the configured directory, with a hard cap of 10,000 directory
  entries, including unrelated entries. Ignore names outside
  `[0-9]{3,}-[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z][a-z0-9_-]*\.md`.
  For matching names, validate the ISO calendar date and require a regular
  file. Refuse symlinks and other unsafe aliases. Reject duplicate numeric
  values across the entire scan, including with different zero padding,
  dates, or authors.
- Require distinct author and peer identifiers matching
  `[a-z][a-z0-9_-]{0,63}`. Require nonnegative integer read and reply
  numbers. Require publication bodies of 1–64,000 characters with at least
  one non-whitespace character, subject to the dispatcher's existing 64 KiB
  canonical JSON argument ceiling.
- Allocate one plus the largest scanned number, using 1 for an empty
  directory. Pad to at least three digits and date the filename in
  `[background].timezone`. Write the exact submitted UTF-8 body followed by
  one added LF, the v1 default `{body}\n`. Preserve existing files verbatim.
  The `# Letter 001 — …` heading in `001-2026-11-03-dingus.md` is part of
  that letter's body. The writer adds no heading or frontmatter.

## Synchronization and publication

- Take the shared checkout lock for inbox and publication. Keep it through
  synchronization, scan, numbering, exclusive create, commit, and push.
  Local read also takes the lock for a consistent scan and file read. Before
  synchronizing, verify branch, clean worktree (including ordinary untracked
  files), clean index, and no in-progress Git operation. Fetch only
  `origin refs/heads/<branch>`, verify fast-forward ancestry, and require
  HEAD to equal the fetched commit after the update. Refuse local-ahead or
  diverged histories. For publication, verify that origin has a single
  matching fetch and push destination. Confirm the inbox effect once HEAD
  and the checkout update are verified.
- Under the lock, check the durable publication key before synchronization.
  An identical confirmed request returns its saved result. A changed reply
  or a matching unresolved request stops with an inspection message. For a
  fresh reply, verify after synchronization that the target exists and was
  authored by the peer. Each target accepts at most one published reply.
- Persist a prepared record with the exact request identity and allocation
  before exclusive creation. Stage and commit exactly the new file. Keep
  the v1 commit subjects `Send letter NNN as AUTHOR` and
  `Reply to letter NNN as AUTHOR`, using the outgoing number for sends and
  the target number for replies. Push the resulting commit to the configured
  branch with a normal push. A competing push can reject it. Keep any local
  file or commit and report the last verified phase, path, and commit when
  known. Use the existing effect markers and normal `tool.intent` and
  `tool.receipt` evidence. Mark publication confirmed after Git verifies the
  push and persist the confirmed result before acknowledging success. No
  automatic retry, rollback, rebase, or force-push.

## State, recovery, and v1 cutover

Use a `letter_publication` record in the existing `state.db` entity ledger.
Persist transitions through the [storage outbox](../contracts/storage.md) to
immutable JSONL evidence. Key replies by stable checkout identity and target
number. Key new letters by checkout identity and a canonical SHA-256 request
fingerprint. Include the exact body, intent and target, canonical repository
identity (checkout, branch and origin), directory, author, peer, and writer
layout in the fingerprint. Store the lineage of the originating tool intent,
allocated number and path, rendered file hash, synchronized base commit,
last verified phase (`prepared`, `created`, `committed`, or `confirmed`),
local commit when known, and confirmed result. Keep raw request text in the
private intent evidence. A record below `confirmed` blocks that key on restart
and preserves its original allocation even if the interrupted file is absent.

Add `scripts/letters_work.py` as an operator-only inspection and reconciliation
entry point, following the data-directory ownership pattern in
[`scripts/image_work.py`](../../scripts/image_work.py). After a failed push, the
operator inspects Git and publishes the existing commit. Reconciliation verifies
the original file bytes and commit against the prepared record, including the
parent, expected subject, and single-file change. Then it verifies that the
same commit is reachable from the freshly fetched configured remote branch.
When the commit ID was lost between Git commit and state persistence, require
a unique commit matching the recorded evidence. Only then append a confirmed
transition with the recovered result. The script performs no publication or
worktree repair. Leave the record blocked if verification fails.

Rehearse enabling publication on a disposable copy of the v1 checkout and its
available evidence. Confirm the directory and body-only writer format against
the copy. The reported live checkout contains only
`001-2026-11-03-dingus.md` and has no reply yet. For any existing replies, start
with the configured branch's Git history. Parse v1 subjects such as
`Reply to letter 003 as sola`, and verify the target, author, added letter,
and publication on the remote branch before seeding the reply identity.
The default v1 layout stores the body and LF. Reply linkage lives in Git
history and legacy receipts.

Use verified completed v1 receipts as supplementary evidence. Look in
`imports/legacy/` and consult the original legacy copy if needed. Phase 13
preserves raw files but does not create active publication records. An
unresolved receipt keeps its target blocked unless the original publication
can be verified. A history-verified reply with insufficient request evidence
still reserves its target. Return an old result only when the exact original
request and publication can be verified. Seed through the operator script
before enabling publication on the existing checkout.

## Acceptance

Use a bare test remote and two disposable clones. Record deterministic checks,
the separate model scenario, and the v1 cutover rehearsal under
`docs/validation/`.

- List and read letters, including full body returns. Page past 20 peer
  letters with interleaved and trailing local letters. Check `next_after`
  and `has_more` at the entry and byte limits, an own-only page, and a
  body too large for one page. Verify no returned peer letter repeats and
  no unreturned peer letter is skipped.
- Start a new letter and reply once. Reject a non-existent target, a local
  letter as target, and a second reply to an already-replied target. Cover
  both a changed body for the same target and a reply seeded from Git
  history. An identical confirmed reply reuses its result.
- Call `letters.reply` with only `body` and verify schema rejection with no
  allocation, file, commit, or push. Also reject `reply_to` on
  `letters.send`, empty and whitespace-only bodies, oversized bodies, and
  negative read or reply numbers.
- Send the exact same body twice, including across restart. Return the
  saved publication result and allocate no new number. Check that the
  generated filename uses the runtime timezone near a date boundary and
  that the file contains the caller's heading once.
- Refuse a dirty worktree, a staged index, the wrong or detached branch,
  duplicate numbers, a matching symlink, an invalid ISO date, and a scan
  above the entry cap. Verify denial when the checkout is outside admitted
  or current workspace roots. Verify the repo reservation blocks generic
  writes and generic Git and blog/dream mutations, including aliases.
- Run a scheduled job that discovers and reads a new peer letter. Deny
  inbox without explicit job roots and deny publication from that job.
  Run an interactive model scenario that finds a new peer letter and
  replies with its number.
- Race a competing push. Interrupt publication after file creation and
  after commit. After restart, verify that a retry preserves the original
  file, commit where present, and evidence without allocating a new
  number. Reconcile only after the operator publishes the verified commit,
  and verify that an absent remote commit or changed file leaves the
  record blocked.
- On the v1 copy, verify the existing layout without rewriting any letter.
  Exercise Git-log reply recovery, a verified completed legacy receipt,
  and an unresolved legacy target before enabling production publication.
