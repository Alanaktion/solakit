# Git tools for configured checkouts

Let Sola inspect a trusted checkout, fast-forward it, and publish selected
changes. Use the [shared tool plan](README.md#follow-up-tool-plans). The
[letters plan](NEXT-TOOLS-LETTERS.md) uses the same checkout operations under
its own grant and publication rules.

## Tool calls

| Tool | Arguments | Result |
| --- | --- | --- |
| `git.status` | `repository` | Current branch, staged and unstaged paths, untracked paths, and whether status is truncated. |
| `git.diff` | `repository`, optional `path` | Bounded tracked changes against HEAD, with truncation indicated. |
| `git.pull` | `repository` | Fast-forwards a clean checkout and returns old and new commit IDs. |
| `git.publish` | `repository`, `paths`, `message` | Commits exactly those paths and pushes that commit. Returns the commit ID and push result. |

`repository` is a configured name such as `project`. Put aliases in the
operator-owned profile fragment so the model can select one directly. `paths`
contains explicit repository-relative filenames from status or the user's
request. `git.diff` can inspect one path or all tracked changes. Reserve branch
management, arbitrary refs, reset, and a separate log tool for later use cases.

## Implementation

- `[git.repositories.<name>]` defines a canonical checkout and expected
  branch. Use the checkout's trusted `origin`. Verify the directory is the
  checkout root and in the admitted and current workspace roots. Start with
  ordinary non-bare checkouts with their own `.git` directory. Reject linked
  worktrees, nested repositories and aliased metadata paths. Protect runtime
  data, source/pack code, and prompt files under the existing guard rules.
- Use fixed Git argv with literal pathspecs. Refuse directories, globs, absolute
  paths and metadata paths. Support tracked deletions by verifying the parent
  and the index entry. Reject symlink and hardlink aliases for supplied paths.
  Disable external diff drivers and hooks. Keep status/diff read-only by
  disabling incidental index updates. Use operator-trusted repository config
  and credentials, with prompts disabled and errors scrubbed of remote URLs or
  raw credential output.
- Use one async lock per checkout for mutations. Recheck authority after
  waiting for the lock. Pull only on the configured branch with a clean index
  and worktree. Publish requires an empty index and local HEAD matching the
  freshly verified upstream. It leaves unrelated unstaged/untracked files out
  of the commit. Use fast-forward-only synchronization and a normal push.
  The push must fail if the branch moved incompatibly. Keep push authority
  interactive-only in the first slice, along with pull.
- Mark each mutation through the existing dispatch receipt. After a failure,
  expose the last verified phase and commit if available. A failed push leaves
  the local commit for operator inspection. A repeated `git.publish` cannot
  publish arbitrary pending commits because the local HEAD check still applies.
  Avoid retries, automatic resets, rebase and force-push.
- Share the checkout validation, lock, and fixed Git calls with letters. Keep
  the generic publish tool denied on the letters checkout. If a Git alias uses
  the blog checkout, use the same lock without changing the blog's existing
  staging and publication policy. Extract subprocess cleanup from the blog
  runner only where its behavior can be preserved. Keep Git command and
  publication policy out of any generic process helper.

## Acceptance

Use real disposable repositories and a local bare remote. Test status and diff,
clean fast-forward, dirty/diverged refusal, path aliases and metadata denial,
publishing two named paths alongside unrelated work, grant revocation while
waiting, and a failing push with a retained local commit. Assert what the
remote actually received. Run a model task that inspects changes and publishes
one named file. Rerun blog tests if its process or lock code changes.
