# Local integration audit

Verified on 2026-11-04 (America/Denver), against the existing screen-hosted
server and source baseline `513434a`. This audit used the running server without
restarting it or changing configuration.

## Live checks

| Check | Result |
| --- | --- |
| Offline configuration | `sola check` passed |
| Health and fresh model preflight | Healthy; exact pinned `Qwen3.6-35B-A3B-mxfp4` available |
| Storage inspection | Healthy, no reported issues |
| API authentication | Anonymous sessions request returned 401; configured operator credential succeeded |
| Prompt inspection | Both configured profiles returned 13 tool schemas; exact UTF-8 prompt SHA-256 verified |
| Model/tool execution | Ephemeral scratch run completed in two steps, calling only `runtime.status` once; persisted prompt hash verified |
| Manual archival | All four transcript messages preserved in archive 1; active transcript empty and ephemeral scope retained |
| UI HTTP smoke | Authenticated chat, tools, runs and operations returned 200; login redirected the authenticated client |
| Integration state | Discord disabled; no configured jobs |
| Existing memory evidence | Nine of 22 pre-audit runs recorded completed extraction |

The scratch session is retained in private runtime evidence with title
**Basic integration audit**, run `ee6a8e93-dbde-4bbe-b08f-8a61cbfe7e00`.
No transcript, prompt contents or credentials are copied here.

Current local memory configuration uses `http://127.0.0.1:1933`, user `default`
and root `viking://user/default/memories`. The startup memory read check reported
ready at `2026-11-05T02:13:15.320150+00:00`; that timestamp is startup evidence,
not a fresh extraction/recall probe. The earlier operator-confirmed live round-trip
remains the memory acceptance evidence. No new memory write probe was needed.
The installed OpenViking version was not reverified.

## Automated checks and limits

Core tests: **175 passed**, with one existing Starlette/httpx deprecation warning.
Ruff lint and format checks passed; core wheel and source distribution built.
Optional pack tests: **18 passed**, run from their source directories because
packs are not installed in this environment; this does not validate installed
pack integration. Combined core and pack coverage: **193 passed**.

HTTP UI checks do not establish browser rendering, keyboard behavior or viewport
coverage. Live summary/compaction, provider correspondence, configured publishing
profiles, Discord and scheduled-job rollout remain in the
[operator acceptance](../operations/acceptance.md). This basic audit does not approve operator
prompt content or the complete SPEC acceptance matrix.

## Workspace sync follow-up (2026-11-05)

The earlier source-only pack-test limitation is superseded by the shared uv
workspace lock. `uv sync --frozen --all-packages` installs core plus all five
editable pack distributions, including common dependencies. The full suite runs
through those installed packages with ordinary `uv run pytest tests packs/*/tests`.
Startup, offline checks and runtime status expose installed module distribution
versions. These checks identify environment/installation drift; they do not
replace a live integration acceptance result or prove a deployed checkout's SHA.
