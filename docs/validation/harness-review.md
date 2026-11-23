# Harness review

Reviewed against base commit `0759314` for a single operator running one local process.
The review covered the packaged runtime and web client, CLI, SDK example, maintenance
commands, evaluation harness, tests and wheel build. The deepest traces followed admission,
prompt assembly, tool authority, evidence recovery and browser reconnects.

## Findings and changes

| Finding | Change |
| --- | --- |
| An ordinary conversation selected the same history and assembled the same prompt several times before its first inference. | Reuse the selected history and prepared prompt. A summary call returns control to the run loop to recheck grants and budgets. A change to the tool-result byte limit rebuilds history at the new limit. |
| The operator's tool catalog and draft prompt could advertise tools that execution hid, including memory tools on a profile with memory off. | Share the origin and memory filters across inspection, disclosure and dispatch. Job-specific and delegated-run restrictions still use the active run. |
| Blog, dream and job tool declarations repeated action names in handlers or inferred the action from the argument type. | Bind each action in its declaration. Keep job update semantics for omitted fields and the interactive restriction on mutations. |
| Recovering the store held the outbox rows in memory and parsed delivered records twice. Reading a history page also queried each entity a second time. | Stream the outbox, reuse the verified evidence entry and validate page rows directly. Fetch frozen admission evidence through the existing session-sequence index. Keep the original admission JSON for idempotent retries. |
| Image reconciliation and compaction each depended on a second artifact reader. Backup and retention read entire files into memory to hash them. | Use the evidence reader for complete and ranged reads. Stream backup and retention checksums. Require disjoint retention directories so a retained tree cannot include the runtime's own data. |
| A cached chat run fetched only its newest page on reconnect. More than one page of new records could leave a gap. | Page back to a known record and use run rows already present in the history response. The browser regression forces single-row pages during reconnect. |

## Verification

- Ran five consecutive short shared-session submissions through the runtime with a fixture model
  on the base commit and the revised tree. Each submission used one model call in both.
  History selections fell from three to one per submission, and prompt assemblies from three to one.
- Compared all 35 built-in tool declarations with the base commit. Names, JSON schemas,
  descriptions, allowed origins and consequential flags matched. SQLite's query plan used
  both columns of the existing `event_pages` index for the admission event lookup.
- All 706 deterministic pytest tests passed. Ruff lint, format checks, strict mypy and the clean
  wheel smoke passed, including installation and dispatch of the separate example pack.
- The 42 fake runtime evaluation trials produced the same behavioral scores as the base commit.
  The same 12 negative-control trials failed their expectations. Replay reproduced the saved
  scores and report.
- All 29 Chromium fixture checks passed, including multi-page reconnect, scheduled delivery,
  PNG preview and download, draft restoration and delegation. An initial browser run crossed
  into the fixture's quiet hour. The fixture now disables quiet hours using the supported
  configuration, and deterministic scheduler tests exercise quiet-hour behavior.

## Limits

The largest remaining costs depend on operator data. Trying successively larger compaction
prefixes reprojects prior turns. Guarded writes inspect workspace Git metadata synchronously.
Latency on a large archive or workspace remains unmeasured. Validation used disposable storage and fixture transports. Live Qwen, oMLX,
OpenViking, ComfyUI and production migration were outside this run. Vendored browser bundles
were exercised through Chromium and checked for packaging.
