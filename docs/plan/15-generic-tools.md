# Generic tools and runtime grounding

Status: locally implemented and scratch-tested.

- Files: bounded read, create-only write, exact unique-match edit (atomic replace),
  append to existing regular files, bounded directory listing. Shared Python
  services enforce each actual path, preserve edit permissions, reject special
  files/symlink escapes and finish durable filesystem mutations before cancellation.
- Git: named-repo status/diff/log, clean fast-forward pull, named-file commit/push.
  Internal services expose status/diff/log/pull/commit/push/commit_push. Mutations
  use repository locks; generic writes require the exact `repo:<alias>` grant.
  Collection services may commit only their scoped directory. Publish additionally
  requires `git.push`, including when called from pack code.
- Publishing refuses an existing index or unpublished commits; push checks that
  exactly the identified new commit is being sent and rechecks all changed paths.
  Paths are literal, repository-relative file names; no directory/glob staging,
  force push or implicit rebase. Failed push reports the local commit and refuses
  automatic retries. Existing upstream configuration is required. Operator Git
  config/filters are trusted; this is not an OS sandbox for hostile repositories.
- Git output is bounded and subprocess groups are killed/drained on cancellation
  or timeout. Runtime-owned mutations serialize; independent operator Git/file
  writers must coordinate with active tools.
- `runtime.status`: live pinned model/availability, channel status, job next-fire
  state and tools granted to the calling profile. No guessed capabilities.

Verification uses local bare remotes, unrelated worktree/index changes, path and
capability denials, failed push state, fast-forward pulls, bounded output, and live
state changes. No production repositories, network publishing or live jobs used.
