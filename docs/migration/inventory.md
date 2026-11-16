# Phase 0 migration inventory

Scope: format inventory from copied reference source/tests/docs, not an export of live Sola data. Reference HEAD: `fd2086bea12e247297cf0acef47544fb535e2324`. The reference working tree has untracked agent directories and skills metadata; inspected source copies are individually hashed in [reference-manifest.json](reference-manifest.json). Legacy code supplies format evidence only.

## Inventory and migration mapping

Actual production paths, record counts, byte volumes, credential reference names, and memory roots are **not collected**. Do not mistake the synthetic examples for production completeness. Preserve deferred integrations' data even though their tools will not load in v1.

| Category / observed legacy location | Authority and format | V2 treatment / verification | Owner and backup |
| --- | --- | --- | --- |
| `sessions/*.meta.json` | Session ID/title/mode/profile/binding/archive/pin metadata | Explicit state.db mapping; retain raw JSON and unknown fields; compare IDs/ordering/dates | Runtime; state + source snapshot |
| `sessions/*.jsonl`, `*.jsonl.N` | Active and archived conversation, tool calls/results | Preserve source bytes, normalized JSONL references; archive segment ordering and call/result pairing | Evidence; all segments |
| Session compaction/archive manifests and notes | Context boundaries, recovery stage, operator notes | Preserve raw manifests; verify selected source ranges; separate v2 archival/compaction semantics | Evidence/state; both |
| `runs/*.events.jsonl`, `*.json` | Run journal plus latest snapshot | Keep journal and raw snapshot; map old statuses/reasons; unfinished work becomes interrupted, never completed by inference | Evidence/state; both |
| Run prompt/turn/fragment/catalog/schema files | Exact request inputs and hashes | Preserve bytes and hashes, including CRLF; missing historic snapshots explicitly unavailable | Evidence; all referenced artifacts |
| Run tool/diagnostic/plan JSONL and artifacts | Execution detail, outputs, effect references | Preserve raw evidence; normalized references; bound presentation, not source history | Evidence; receipts + artifacts |
| Run notes/memory JSONL and commit fields | Staged notes, commit intent/provider state | Preserve exact text and ambiguous submissions; no automatic resend | Runtime/OpenViking; state + local evidence |
| `app.db.job_definitions` | Authoritative `(id, payload, deleted)` records | Extract independently of FTS; preserve tombstones/unknown fields; disabled until reviewed after migration | Scheduler; consistent SQLite backup |
| `jobs/*.jsonl` | Pause/consumed/last-run/cursor evidence | Reconcile with definitions; consumed/uncertain attempts do not rearm | Scheduler; state + journals |
| `pack-operations/` JSONL, pack-owned files/repos | Effect intent/result, request fingerprints, domain content | Retain even for deferred packs; image reconciliation remains needed; do not replay intents | Pack/operator; receipts + resource data |
| Operator config, SOUL and prompt fragments | Identity, grants, roots, model pin | Preserve source; generate effective-authority diff; exclude deferred tool grants; no personality rewrite | Operator; private config/identity backup |
| Credential references and login-state file | Secret resolution references; durable legacy browser sessions | Do not put values in Git/reports; revoke old browser sessions at cutover, issue v2 login | Operator/auth; private backup only |
| OpenViking canonical roots | Remote semantic memory and extraction state | Verify installed API/scope; retain existing roots; test writes use separate root | OpenViking/operator; separate service backup |
| Legacy channel bindings/post evidence | Historical provenance, potentially outstanding deliveries | Preserve history; do not activate Discord/Android/channels absent release scope | Operator/runtime; raw provenance |
| Legacy derived indexes/calibration caches | Search/offset/token-estimate acceleration | Do not copy as authority; rebuild search.db solely from JSONL; validate/relearn caches | Disposable; no required backup |

Legacy archival can rotate the active transcript into numbered segments while maintaining the topic binding. V2 archival instead hides/freezes a topic, independently of context compaction. Import must preserve the historical boundary without adopting the old UI operation's semantics.

## Copied and synthetic examples

[reference-manifest.json](reference-manifest.json) records byte count/SHA-256 of fourteen source/test/doc files copied to a temporary directory and compared byte-for-byte. No legacy Store or server was instantiated; no live config, .env, data directory, credentials, or remote service was loaded.

[fixtures/legacy-format/manifest.json](fixtures/legacy-format/manifest.json) inventories eleven deliberately synthetic examples: session metadata with an unknown field; active and archived JSONL; a tool-call/result group; run events; CRLF prompt bytes; tool schemas/receipt; a torn trailing record; authoritative job-row export with a tombstone; a consumed job journal; and an uncertain pack-operation intent. They are minimal format examples, not a full legacy-schema acceptance corpus or real SQLite backup. Phase 3 expands/validates them against full import requirements.

## Production inventory handoff

Before Phase 13 rehearsal, obtain a consistent copied evidence tree and SQLite backup plus independently backed-up pack resources and memory roots. For every category record source root, source format/version, record count, byte count, hashes, ordering key, ID namespace, outgoing references, unknown fields, anomalies, destination, and verification result. Credentials remain private and are inventoried by reference/availability only.

Phase 3 may proceed with synthetic inputs. Actual production completeness and recovery gates remain pending until that copy exists. A source count alone is insufficient: verify hashes, stable IDs, ordering, references, consumed attempts, and uncertain effects. Search rebuild is never a repair strategy for lost state.db.
