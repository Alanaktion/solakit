# D1–D8 refactor validation

November 10, 2026. Baseline: `2902a5395bc095a39db106615f4a02c118e09a35`.

## Implementation

- D1: resource API routers use a typed FastAPI runtime dependency; UI route
  modules receive an explicit UIContext. StepState owns mutable per-run state,
  request preparation, model completion, response evidence, tool groups, reply
  validation and wrap-up. The step-loop complexity exemption is removed.
- D2: public Store.tool_log and Store.archive replace external private-reader
  calls in API, runtime and statistics code.
- D3: StoreServices and ExecutionServices declare shared mixin contracts.
  Compactor receives memory per operation; RuntimeOps no longer changes its
  shared memory attribute.
- D4: jobs, services and UI helpers have additional annotations. Mypy checks 33
  files in CI, including mixin implementations, with unannotated-body checking.
  Third-party missing stubs are allowed; imported modules outside the initial
  boundary are followed silently.
- D5: fingerprinted byte offsets select exact run messages across active and
  archived files. Startup/rebuild and rotation reconcile addresses. Missed writes
  reconcile on lookup; unavailable SQLite falls back to validated evidence scans.
- D6: run listings remain arrays with limit/offset and server-side filters.
  Expression indexes cover session, status, memory status and creation time;
  pending-memory polling also has a partial index. UI pages show 100 runs.
- D7: request size is shared by budget checks and calibration. Store reuses a
  lock-serialized SQLite connection and commits run/activity cache updates in one
  transaction. Every JSONL save and atomic snapshot keeps its durable checkpoint.
- D8: one lifespan-owned internal ASGI client serves UI helpers. Cookie and Bearer
  headers remain request-local, response cookies are rejected, and shutdown closes
  the client.

New tests cover run reads across rotation/restart, avoiding unrelated history
reads, missed index writes, damaged evidence, missing active rotation files, LF/CRLF/CR line boundaries,
unavailable indexes, pagination past
100 runs, filter ordering, SQL index use, concurrent authentication isolation and
client shutdown. Existing route snapshots, cancellation, compaction, delegation,
job preservation and transcript tests remain in the full suite.

## Automated and browser checks

- Frozen all-package workspace sync passed.
- Full core/pack suite: **982 passed**, on the checkout's CPython 3.15 environment.
- Mypy: **33 files**, no errors. Ruff lint and format checks passed.
- Operator `sola check` passed with existing env/file secret references.
- All nine workspace distributions built; core wheel remains pack-free and
  includes the new routers, contracts, offset reader and UI assets.
- Browser phase 11 passed light/dark at 1440×1000 and 390×844: prompt API parity,
  schema expansion, stored evidence and cancellation. Existing selectors were
  narrowed to their panels to accommodate multiple hashes, prompts and tools.
- New browser `runs` phase passed the same four combinations with 500-run
  fixtures: session/status filtering, 100-row pages, next/previous navigation,
  empty results, no horizontal page overflow and no browser script errors.
  Screenshots were inspected separately from HTTP tests.

One earlier full-suite run hit the shell cancellation timing assertion while
live model and browser work overlapped. The shell tests passed in isolation and
subsequent complete runs passed. Starlette's existing TestClient deprecation
warning remains; it is outside D1–D8.

## Synthetic performance comparison

Local timings are illustrative, not service latency guarantees. Both versions
used synthetic private temporary evidence and the same host/interpreter.

| Check | Baseline | Candidate |
| --- | ---: | ---: |
| Full run read, median of 10; 5,000 historical messages / 10 archives | 22.91 ms | 0.39 ms |
| 25 durable saves, total | 28.10 ms | 21.42 ms |
| Request preparation, median of 30; 1,000 messages × 512 characters | 1.71 ms | 0.86 ms |
| Request-size serializations per ordinary step | 2 | 1 |

## Real-model comparison

Two isolated trials each of `disclosure-discover`, `find-by-name` and
`grep-optional-args`, using the configured pinned local model and configuration
hash `f57882e3e94f`. Memory and external integrations remained disabled. The
baseline imported an unmodified source archive of the commit above; the candidate
used the refactored checkout. Only temporary harness workspaces were written.

```sh
sola eval --only find-by-name,grep-optional-args,disclosure-discover --trials 2
sola eval compare <baseline.json> <candidate.json>
```

| Scenario | Baseline | Candidate | Pass-rate delta; 95% bootstrap interval |
| --- | ---: | ---: | --- |
| disclosure-discover | 2/2 | 2/2 | 0.00; [0.00, 0.00] |
| find-by-name | 2/2 | 1/2 | -0.50; [-1.00, 0.00] |
| grep-optional-args | 2/2 | 2/2 | 0.00; [0.00, 0.00] |
| Overall | 6/6 | 5/6 | -0.17; [-0.50, 0.00] |

The failed candidate completed using only files.list after loading that tool;
its trace omitted the scenario's required files.find call. It had no runtime
error or malformed-call repair. All intervals include zero; this small sample
neither establishes a regression nor supports a quality improvement claim.
Raw local results remain in `/private/tmp/solakit-d-refactor-eval-before/` and
`/private/tmp/solakit-d-refactor-eval-after/`; model/prompt evidence is not copied
into this report.

## Compatibility

The first start with derived-index version 2 revalidates historical evidence and
preserves authoritative managed job definitions. Back up app.db along with the
evidence tree. Run-list clients relying on an unlimited array must now paginate;
default ordering and array shape remain compatible. Offset pagination observes
live state rather than a database snapshot across requests. D9–D12 and unrelated
CI projects remain deferred. No deployment, integration activation or Hermes
changes were performed.
