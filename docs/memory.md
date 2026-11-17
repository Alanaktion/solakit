# Memory operation and migration

Use a stable OpenViking version **>=0.5.5,<0.6.0**, including four-part hotfixes.
Version 0.5.5 remains the live-tested baseline; prereleases, local labels and new
minor versions are rejected. Run the isolated live probe after upgrading before
using production memory. Configure its account/user and managed user root explicitly:

```toml
[memory]
enabled = true
base_url = "http://127.0.0.1:1933"
account = "default"
user = "default"
roots = ["viking://user/default/memories"]
# api_key_env = "OPENVIKING_API_KEY"
timeout_seconds = 15
backlog_cap = 128
```

Enable `memory.recall` / `memory.remember` in both `loaded_tools` and profile `tools`
when explicit model access is wanted. Automatic recall/extraction only needs enabled
memory and a shared profile with `memory_mode = "optional"` or `"required"`.
Operator config, identity and production roots are never changed by developer probes.

## Outages and uncertain work

The normal reply can finish while memory is pending, accepted, or failed.
`runtime.status` exposes observer health and commit counts; durable `MemoryRecall`
and `MemoryCommit` records distinguish recall from extraction failures. Inspect
`state.db` through the operator service/evidence while running, or stop Sola and use:

```sh
uv run python -m scripts.memory_work --config /path/to/config.toml
uv run python -m scripts.memory_work --config /path/to/config.toml --commit-id COMMIT_ID
```

The developer script acquires the data-directory lock and prints private evidence.
It does not start a worker. Back up state/evidence together before manual recovery;
see [backup/restore](backup-restore.md). Do not delete or edit JSONL/state rows to
force retries. A failed observer is visible in status; fix its cause and restart.

For ambiguous work, inspect OpenViking session `sola-COMMIT_ID` and its task list in
the **same account/user**. Never resend its messages/commit merely because a client
timed out. If a task exists, attach its verified reference explicitly:

```sh
uv run python -m scripts.memory_work --config /path/to/config.toml \
  --commit-id COMMIT_ID --provider-task-id TASK_ID
```

The script only performs remote reads, checks the supported version range and task type/session
ownership, and appends a local accepted transition. Restart Sola to resume polling.
The same action can resume capped observation of known accepted work. A completed
task proves extraction completion, not successful retrieval. If no task can be
identified, leave the ambiguous record visible; no safe automatic replay is claimed.
Failed/cancelled run notes require operator inspection and a new intentional request.

## Live gate

Use an explicitly isolated **test account**. The probe refuses the default account,
uses user `default`, records the live schema, and retains all disposable local evidence:

```sh
uv run python -m scripts.probe_memory --account sola-test \
  --url http://127.0.0.1:1933 --output /tmp/sola-memory-probe
```

For authenticated services add `--api-key-env OPENVIKING_API_KEY`. The probe states
a distinctive generated fact, persists/submits a batch, waits for extraction, then
recalls from a fresh local session with a question that omits the fact. Reports
separate extraction from retrieval. Test records stay in the selected test account;
the probe never deletes provider data or lists production memory. Inspect/remove
test data separately through OpenViking when desired.

## One-shot configuration migration

Work from a copied source tree so relative legacy paths still resolve correctly:

```sh
uv run python -m scripts.migrate_config --source /copy/config.toml \
  --profile interactive --data-dir /new/sola-data --output /private/migration-1
```

Outputs are `legacy.source.toml` (exact private copy, possibly containing secrets),
`report.json` (no credential values), and `config.v2.toml` when authority checks pass.
No production file is replaced. Other legacy profiles and unported options are listed
as unsupported; repeat with another selected profile if needed. Existing output
folders are refused to keep the operation one-shot and preserve prior reports.

To revise roots/grants/fragments/budgets, prepare a candidate TOML and rerun into a
new output folder with `--candidate /private/candidate.toml`. Inspect unresolved
authority changes, then rerun with only the intended named resolutions, for example
`--resolve tools --resolve write_roots`. Resolution choices appear in the report.
Memory enabling/scope needs `--resolve memory`. Keep the generated draft private,
review the unsupported settings, and select it explicitly when starting v2. This
phase does not import history, migrate production memory, or perform cutover.
