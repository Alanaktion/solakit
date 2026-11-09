# Architecture

One page: the run lifecycle, the evidence layout and a module map. `SPEC.md`
holds behavioural contracts; `docs/operations*` hold operator how-to. A test
checks that every module path named here exists.

## Run lifecycle

```
channel/API/job ──► Execution.enqueue (admission: durable run, session FIFO)
                      │  per-session lock ─► inference semaphore
                      ▼
                 _perform_locked: refresh grants → model preflight → recall
                      → system prompt (stable) + turn block (time/origin/recall/
                        notes/job playbook) → budget check / compaction
                      ▼
                 _loop, per step:
                   request = aged(messages)            (optional tool-result aging)
                   ModelClient.complete(request, schemas, sampling/thinking params)
                     ├─ reasoning → runs/<id>.reasoning.jsonl (never resent)
                     └─ <tool_call> content fallback (opt-in, fail-closed)
                   append assistant message (verbatim) → dispatch each call
                     Registry.dispatch: name resolution → access → argument
                     preparation (lenient/strict) → tool → truncation → log
                   render results (envelope|text) → append tool messages
                   runtime notices (step/no-progress/plan/structured/wrap-up)
                      ▼
                 status + hint → memory commit (shared) → delivery (channel/job)
```

## Evidence layout (data directory)

| Path | Contents |
|---|---|
| `sessions/<id>.jsonl`, `.jsonl.N` | verbatim transcript, numbered archives |
| `sessions/<id>.meta.json`, `.compaction.json`, `.archive.json` | metadata, rotation manifests |
| `sessions/<id>.notes.jsonl` | session notes (append-only) |
| `runs/<id>.events.jsonl` | run record history |
| `runs/<id>.prompt.txt`, `.prompt-fragments.json`, `.turn.txt` | exact system prompt, fragments, turn block |
| `runs/<id>.tool-schemas.json`, `.tool-catalog.json` | schema evidence |
| `runs/<id>.tools.jsonl` | redacted tool log (normalizations, resolved names) |
| `runs/<id>.reasoning.jsonl`, `.plan.jsonl`, `.injections.jsonl` | reasoning, checklist, runtime notices |
| `app.db` | rebuildable indexes + authoritative job definitions |
| `calibration.json`, `doctor.json` | bytes/token calibration, last doctor run |

## Module map

| Module | Responsibility |
|---|---|
| `src/solakit/main.py` | app factory, API routes, startup diagnostics |
| `src/solakit/cli.py` | `sola` commands (check, doctor, eval, init, config, tools, search) |
| `src/solakit/core/loop.py` | admission, nesting, step loop, notices, wrap-up |
| `src/solakit/core/steps.py` | pure step helpers: repair budget, thinking schedule, history validity |
| `src/solakit/core/runtime_ops.py` | archive/compact operations and memory note staging (mixin) |
| `src/solakit/core/model.py` | pinned model client, validation, fallback hook |
| `src/solakit/core/model_stream.py` | SSE assembly, reasoning split |
| `src/solakit/core/toolcall_fallback.py` | `<tool_call>` content parser |
| `src/solakit/core/prompt.py` | system prompt and turn block assembly |
| `src/solakit/core/playbooks.py` | playbook parsing and index |
| `src/solakit/core/aging.py` | request-time tool-result aging |
| `src/solakit/core/compaction.py` | rotation, OpenViking and local summaries |
| `src/solakit/core/structured.py` | structured job output validation/rendering |
| `src/solakit/core/hints.py` | operator failure hints |
| `src/solakit/core/stats.py` | model health aggregates |
| `src/solakit/core/doctor.py` | live probes |
| `src/solakit/core/eval.py` | evaluation harness |
| `src/solakit/core/explain.py` | config explain/diff |
| `src/solakit/core/residents.py` | resident suggestions |
| `src/solakit/core/scaffold.py` | `sola init` |
| `src/solakit/core/store.py` | evidence storage and indexes |
| `src/solakit/core/config.py` | configuration models |
| `src/solakit/tools/__init__.py` | registry, dispatch, rendering, `tool_json` |
| `src/solakit/tools/arguments.py` | argument preparation and repair messages |
| `src/solakit/tools/disclosure.py` | eager/deferred/auto disclosure |
| `src/solakit/tools/ports.py` | typed context seams |
| `src/solakit/sdk.py` | supported pack surface (`SDK_VERSION`) |
| `src/solakit/services/fs.py` | scope-checked filesystem (windows, grep, find) |
| `src/solakit/jobs/__init__.py` | scheduler, chaining |
| `src/solakit/channels/__init__.py` | delivery and channel posts |
| `src/solakit/ui/routes.py` | operator UI routes |
| `packs/email/src/sola_packs/email.py` | example external pack (uses only `solakit.sdk`) |
