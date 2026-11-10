# November 10 code review fixes

Implemented B1–B6 and S1–S6 from the local November 10 code review.
Architecture, performance and CI expansion remain separate work.

## Behavior changes

- Filesystem tools refuse Git metadata writes, including aliases and differently
  cased `.git` components. Empty workspace defaults include repositories only.
  Runtime data needs an applicable explicit path grant naming the directory or a
  descendant, even with parent-directory or anywhere grants. Case-insensitive
  filesystem aliases cannot bypass that exclusion.
- Unsafe cookie-authenticated requests reject cross-origin browser metadata,
  including another port on the same host. Bearer API clients remain independent.
  Proxy setup documents trusted forwarding for Secure cookies and client-IP limits.
- Idle session workers, queues and locks retire after normal, cancelled, delegated
  and scheduled work, under the admission lock.
- Read-only filesystem operations release cancellation without waiting for worker
  threads; traversals stop cooperatively. Writes retain durable completion. Grep
  uses the timeout-capable `regex` engine in compatibility mode, skips lines over
  4,096 characters and limits cumulative matching to 100 ms. Incomplete search
  coverage is visible even with no matches, separately from byte truncation.
- SQLite lock contention retries once. Query failures remain recoverable; missed
  index writes are reconciled from session evidence before search resumes.
  Structural failures remain visible until repaired.
- Discord skips whitespace-only chunks and ignores download proxy environment
  variables. All secret-file references resolve relative to configuration.
  Damaged saved logins require fresh authentication. Redaction matches longer
  secrets first; encoded secret representations remain outside exact matching.

## Real-model comparison

Two isolated trials each of `find-by-name` and `grep-optional-args`, using the
operator's pinned local model and the real file tools. Memory and integrations
were disabled by the harness; only temporary seeded workspaces were written.
The baseline imported unmodified source from commit `86d8f49`; the candidate used
the working changes. Both used configuration hash `f57882e3e94f`.

```sh
sola eval --only grep-optional-args,find-by-name --trials 2
sola eval compare <before.json> <after.json>
```

| scenario | A | B | Δ | 95% CI |
|---|---:|---:|---:|---|
| find-by-name | 0.50 | 1.00 | +0.50 | [+0.00, +1.00] |
| grep-optional-args | 1.00 | 1.00 | +0.00 | [+0.00, +0.00] |
| **overall** | 0.75 | 1.00 | +0.25 | [+0.00, +0.75] |

* marks intervals excluding zero. Small trial counts make only large deltas trustworthy.

This small sample establishes successful candidate tool use, not a statistically
supported improvement. Baseline `find-by-name` missed the required tool call in
one trial despite completing; both grep trials passed before and after.
Raw local result files: `20261110T162528-unknown-f57882e3e94f.json` (baseline,
archive import has no Git metadata) and
`20261110T162734-86d8f49-f57882e3e94f.json` (candidate). Raw prompt/run artifacts
remain outside the repository.

## Validation

- Frozen workspace sync and offline `sola check` passed. The existing curated
  memory front-page size warning remains.
- Ruff lint/format checks and core plus all pack distribution builds passed.
- HTTP checks cover origin enforcement and trusted-proxy authentication behavior.
  Browser visual QA was not performed; no visual assets or templates changed.
- Full core/pack suite: **972 passed**, with the existing Starlette TestClient
  deprecation warning. Final targeted checks after the last filesystem refinements:
  **128 passed**. Tests ran on the checkout's CPython 3.15 environment; expanding
  the Python-version CI matrix is deferred.
