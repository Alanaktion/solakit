# Git-backed letters

Add correspondence for one existing checkout. Sola can check for letters from
its peer, read a letter by number, and publish a new letter or a reply. Preserve
the existing numbered Markdown format. Use the [shared tool plan](README.md#follow-up-tool-plans)
and the checkout operations from the [Git plan](NEXT-TOOLS-GIT.md).

## Tool calls

| Tool | Arguments | Result |
| --- | --- | --- |
| `letters.inbox` | `after=-1` | Fast-forwards the checkout, then returns up to 20 peer letters in number order, with dates, `next_after` and `has_more`. |
| `letters.read` | `number` | Returns the number, date, author and Markdown body from the local checkout. |
| `letters.send` | `body`, optional `reply_to` | Publishes a new letter, optionally replying to a peer letter. Returns its number, path and commit. |

Describe `inbox` as a checkout update and `read` as a local read. `after` is the
last letter seen by the caller. Checking the inbox does not mark anything read.
`reply_to` comes from an inbox or read result. Use one send tool because the only
decision the model needs to make is whether this is a reply.

## Implementation

- Configure `[letters]` with the checkout, branch, local author and peer author.
  Use the runtime timezone and the existing `NNN-YYYY-MM-DD-author.md` naming
  convention, with numbering across both authors. Confirm the existing file
  layout with a disposable copy before selecting the one writer format. Scan
  only that directory. Reject duplicate numbers and unsafe files. Preserve
  existing letters verbatim.
- Require the checkout to be inside admitted and current workspace roots.
  `letters.inbox` and `letters.send` are consequential and interactive-only in
  the first slice. `letters.read` can be granted to a read-only scheduled profile.
  Reserve the correspondence checkout for letters mutations, including when Git
  or blog services are configured. Give the letters profile the named letters
  tools and keep generic writes and shell out of it.
- Hold the checkout lock across fast-forward, numbering, exclusive create,
  commit and push. Require a clean checkout and index before synchronization.
  Verify the reply target is a peer letter and accept at most one reply per
  target. Commit exactly the created file. A concurrent remote update can reject
  the push. Retain local work and report the last verified phase and commit.
- Index a small durable publication record by checkout identity and reply
  target, or by checkout identity and exact body fingerprint for a new letter.
  Record the request and allocated number before creating the file. Identical
  confirmed sends return the saved result. A changed reply or unresolved send
  stops with an inspection message. Use the existing state/evidence path and
  receipts. For a failed push, have the operator inspect and publish the
  existing commit. Reconcile a blocked record with a small operator-only script
  that verifies the original file and remote commit before marking publication
  confirmed. Retain the blocked record when the outcome cannot be verified.
- Before enabling send on an existing v1 checkout, check whether existing
  replies can be identified from their Markdown. Where reply links live only
  in v1 receipts, seed reply identities from verified completed receipts and
  preserve unresolved targets as blocked. Do this explicitly against a copy.

## Acceptance

Use a bare test remote and two disposable clones. Exercise listing and reading,
starting a letter, replying once, retry after restart, duplicate numbering,
checkout denial, and a competing push. Interrupt publication after file create
or commit and verify a retry leaves the original evidence and allocates no new
number. Run a model scenario that finds a new peer letter and replies using its
number. Confirm the existing v1 layout and receipt handling before production use.
