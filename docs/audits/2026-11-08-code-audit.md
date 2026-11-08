# Whole-app code audit — 2026-11-08

Scope: core execution (`loop`, `model`, `model_stream`, `store`, `memory`,
`memory_ops`, `compaction`, `archival`, `session_lifecycle`, `config`, `media`,
`search`, `recall`), `main.py`, channels (API, Discord), jobs scheduler, services
(`fs`, `git`, `process`), built-in tools (`files`, `shell`, `delegate`,
`sessions`, `memory`, `_remote`, `jobs`, `disclosure` dispatch path), UI auth,
Markdown sanitizer and route helpers, and the side-effecting email pack plus the
shared pack receipt/lease support.

Baseline before changes: 736 core/pack tests passed, Ruff clean.

## Fixed in this audit

Each fix has a regression in `tests/test_audit_regressions.py`; all six checks
fail against the pre-audit source and pass after it.

| Area | Defect | Fix |
| --- | --- | --- |
| `core/loop.py` | When a tool finished despite a cancellation request (a tool that completes accepted work, or a cancel that lands just after dispatch returns), the loop raised before appending the result, dropping a completed tool result from session history. SPEC requires completed tool results to stay in history. | Append the result first, then honor the cancellation; no later calls in the group are dispatched. |
| `core/model.py` | Streamed completions with neither text nor calls assemble to `""`, which slipped past the `Empty completion` check (it only rejected `None`). Runs were marked `completed` with a blank reply; Discord delivery then failed. | Empty/whitespace text with no calls is `model_unavailable` unless `finish_reason` is `length` (still kept as partial evidence). |
| `services/process.py` | After a timeout/cancel kill, the final stdout/stderr drain waited for EOF without a bound. A descendant that left the process group (`setsid`, daemons) kept the pipes open, hanging `shell.*` until the registry timeout and losing the captured output. | Bounded five-second drain grace; on expiry report the kept prefix as incomplete. |
| `core/store.py` (`pending_memory`) | The memory observer stops at the first `MemoryUnavailable` to avoid retry storms, but always visited runs in rowid order. One permanently unobservable task (e.g. expired after an OpenViking restart) starved every later pending extraction forever. | Visit previously `status_unavailable` runs last. |
| `ui/auth.py` | Every authenticated request (middleware and dependency, i.e. twice) rewrote and fsynced `ui-sessions.json` plus its directory on the event loop for sliding renewal. | Persist renewals at most once a minute; login/logout/expiry still write immediately. |
| `channels/__init__.py` | `delivery_locks` kept one `asyncio.Lock` per delivered run forever (unbounded growth over uptime). | `WeakValueDictionary`: locks vanish when no delivery holds or awaits them. |
| `tools/files.py` | `files.write` was summarized as "Replace a local file" with keyword `overwrite`, but it is create-only; deferred discovery steered the model toward a call that always fails on existing files. | Summary/keywords now say create/new. |

SPEC (§8 shell, streaming/cancellation, memory observer, auth) and
`docs/setup.md` were updated to match.

## Improvements worth implementing later

Not bugs against the current contract, or too invasive for an audit pass.
Roughly ordered by value.

1. **Memory observer backoff/terminal state.** A task OpenViking no longer knows
   is still polled every 5 s forever (now last, but forever). Add per-run backoff
   and a terminal `status_lost` after N consecutive failures, surfaced in the UI
   with the existing manual refresh as the recovery path.
2. **Git service hygiene.** `services/git.py` passes the full process
   environment (including any env-supplied model/Discord/pack secrets) to `git`,
   and its post-kill drain has the same unbounded-EOF hang just fixed in
   `process.py`. Pass an allowlist (PATH, HOME, LANG/LC_*, SSH_AUTH_SOCK,
   GIT_SSH_COMMAND, proxy vars) and reuse the bounded drain.
3. **DNS-rebinding window in remote reads.** `_remote.public_url` resolves and
   checks addresses, then httpx resolves again when connecting. Pin the vetted
   address (custom transport / `local_address`-style resolver) so a rebinding
   host cannot redirect `web.check_feed`/`get_page` to private addresses.
4. **Artifact verification inside dispatch.** `Execution._loop` calls
   `ctx.path(artifact)` after dispatch; a tool returning an out-of-scope artifact
   raises outside the tool error contract and fails the whole run. Validate in
   `Registry.dispatch` and convert to a tool error result.
5. **Transcript append cost.** `Store.append_message` re-reads the entire active
   transcript to count lines for the FTS address on every message (O(n) per
   append, O(n²) per long session). Cache the line count per session and
   invalidate on rotation.
6. **Synchronous tools outlive timeouts.** Sync tools run via `asyncio.to_thread`;
   a timeout returns to the model while the thread keeps running and may still
   cause effects. Prefer async tools for effects, or document per-tool that
   timeout is not cancellation.
7. **Discord feedback for non-completed runs.** Only `completed`/`tool_error`
   runs deliver anything. `model_unavailable`, `context_limit`,
   `budget_exceeded`, a shutdown race or an archived binding leave the Discord
   user with silence (inbound exceptions are only logged). A short, clearly
   labelled status notice (not an invented reply) would match the UI's honesty.
8. **`POST /api/sessions` with an existing ID** returns the existing session
   with 201 and silently ignores requested fields. Return 200 for an identical
   request and 409 for a conflicting one.
9. **UI `api()` helper** assumes JSON error bodies; a plain-text 500 becomes a
   second, opaque 500. Fall back to the response text.
10. **`/health/model` is unauthenticated** and performs an outbound preflight per
    request. Cache the result for a few seconds or require auth for the probe.
11. **Email rejection classification.** `SMTPSenderRefused` raised by
    `send_message` is recorded `uncertain`, although the server refused MAIL FROM
    before any DATA. Classify it as `rejected` so the operator can retry with the
    same operation ID.
12. **Job `{time}` placeholder** renders the scheduler tick time with
    microseconds rather than the scheduled fire time. Consider rendering the due
    instant at second precision (keep tick time for manual runs).
13. **SQLite connection lifetime.** `with sqlite3.connect(...)` commits but does
    not close; closure relies on CPython refcounting. Wrap in
    `contextlib.closing` for portability and deterministic file-handle release.
14. **Corrupt metadata classification.** `Store.set_session_archived` parses
    metadata JSON before validation, so damaged metadata surfaces as a 404/422
    instead of `StorageCorruption` (503 + storage health issue).

## Reviewed without findings

Store recovery/rotation (prepare/finish idempotence, manifest stages, recovery
markers), incremental index rebuild, compaction/archival hash checks, job
one-time consumption and catch-up, lifecycle lock ordering (jobs → submit),
delegation cancellation propagation, deferred-disclosure dispatch, Markdown
sanitizer allowlist, login throttling, redaction filter shapes, pack operation
receipts/leases and email replay refusal, Discord attachment URL validation,
media normalization limits.

Not performed: live backend probes, browser visual QA, pack-by-pack review of
letters/blog/image/printer/calendar/journal beyond the shared support module.

## Implementation follow-up — 2026-11-08

The recommendations above are retained as the original audit record. Implementation
commits, with focused regression coverage:

| Item | Result | Primary commit |
| --- | --- | --- |
| 1 | Persisted observation failure counts and exponential backoff (5–300 s); eight consecutive failures stop polling as `status_lost`. Successful manual refresh resets retry state; the run page offers an explicit recovery action. | `e11367c`, `03bee67` |
| 2 | Git uses an environment allowlist and the shared bounded process runner. Cleanup also bounds process waiting when an escaped child retains pipes. | `6cc1bbe`, `0e02f4e` |
| 3 | Every feed hop connects to a vetted public IP, preserving Host and TLS SNI. Connections close between hosts and environment proxies are disabled. Cloudflare page conversion remains provider-controlled: local checks cannot pin Cloudflare DNS or its redirects. | `bcec46f` |
| 4 | Artifact scope validation occurs inside dispatch and becomes a tool error. | `4cb5259` |
| 5 | Per-session line counts reuse inode/size/mtime fingerprints; rotation or other file changes invalidate the cache. | `dcb027a` |
| 6 | Every synchronous tool schema states that timeout cannot stop its worker thread and effects may continue. | `c411854` |
| 7 | Interactive terminal failures produce labelled status notices; admission exceptions produce a generic notice. No assistant evidence is invented. Scheduled failures retain their existing gap contract. | `dac74aa`, `7fd19ea` |
| 8 | New sessions return 201; identical existing sessions return 200; conflicting fields return 409. Omitted creation timestamps are ignored on retries. | `4d4e787` |
| 9 | UI API errors fall back to response text, preserving status and escaped presentation. | `ea6b894` |
| 10 | Active model probes require shared operator authentication; basic health remains public. | `eb39751` |
| 11 | SMTP sender refusal is `rejected`, allowing an explicit retry with the same operation ID. | `cfd5cc0` |
| 12 | Scheduled/catch-up tasks use the due instant; manual tasks use the current instant. Time placeholders use seconds. | `5e91a49` |
| 13 | Core storage, search, and job definitions close SQLite connections deterministically while preserving transaction contexts. | `4604362` |
| 14 | Archival validates session metadata before parsing importer fields, preserving storage corruption reporting. | `ed3e8a1` |

Validation: full core/pack pytest suite, focused regressions, Ruff lint/format,
all-package source/wheel builds, and packaged recovery-template checks. Offline
`sola check` passes with operator environment loaded; the existing front-page
size warning remains. No live integration probes or browser visual QA performed.
