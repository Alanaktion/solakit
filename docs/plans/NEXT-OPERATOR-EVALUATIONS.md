# Operator probes and evaluations

Let the operator run small, reproducible tests and compare the outcomes in
Settings or through the authenticated API. Reuse the repo's
[corpus and scorers](../../evals/README.md),
[memory probe](../memory.md#live-gate), and
[vision fixture design](NEXT-VISION-MODEL.md#acceptance). Availability and
effective configuration belong in [Diagnostics](NEXT-OPERATOR-DIAGNOSTICS.md).

## Operator actions

| Action | Checks and result |
| --- | --- |
| **Test text model** | Run a short synthetic completion and a dummy tool round trip. Report pin, argument fidelity, terminal status, latency, and usage where available. |
| **Test vision model** | Ask for a fresh token rendered only in fixture pixels. Report whether the configured VLM read it and record the model pin and image hash. |
| **Test memory round trip** | In an explicitly configured test account, extract a generated fact and recall it in a fresh session. Report extraction and retrieval separately. |
| **Run smoke evaluation** | Run a fixed small subset of the existing corpus and show per-case results plus the aggregate denominators. Compare two completed reports in the UI. |

Ship the text probe and smoke evaluation first. Enable the vision or memory
action when its adapter and test settings are available. Offer one entry point
under **Settings → Diagnostics** with action, progress, result, and evidence.
Keep the full developer corpus available through the existing developer runner.

## API and execution

- Add `POST /api/v1/diagnostics/probes`, `GET /api/v1/diagnostics/probes`,
  `GET /api/v1/diagnostics/probes/{id}`, and cancellation for an active probe.
  Accept a fixed probe kind and an idempotency key. Use bounded pagination and
  ordinary status polling in the browser. Each result links to its private
  evidence and records whether it was simulated or live. The API accepts no
  user-defined scripts, commands, prompts, filesystem paths, or provider URLs.
- Run at most one explicit operator probe at a time in the existing process.
  Reuse the single shared inference semaphore, releasing it between cases.
  Run synthetic fixtures with disposable files, fake tools and no production
  transcript, jobs, or effects. Package the fixed fixture and scoring logic
  used by both entry points. Use bounded active time and normal shutdown
  cancellation. Persist status and reports through the existing state/evidence
  path. Interrupted work is inspectable and requires an explicit new launch.
- Require a configured test account and root distinct from the live scope
  before offering a write test. Verify the test identity before submission,
  extract a generated fact, then query without repeating it. Track the provider
  task and preserve ambiguous submissions for inspection. Keep probe data in
  the test scope and show results only in Diagnostics.
- Store comparable metadata with each report, including fixture and scorer
  hashes, model pin, server identity where available, effective request settings,
  execution mode, and thresholds. Show per-case differences and aggregate
  change when conditions match apart from one declared comparison variable,
  such as a model pin or request setting. Report all other mismatches in the
  API and UI. Use the recorded settings to show which change was tested.

## Acceptance

Exercise idempotent start, concurrent start refusal, cancellation, completion
and restart using fake services. Confirm probes share the inference permit and
cannot read live topics or dispatch live tools. Verify that evaluation comparison
flags unmatched metadata, failures retain evidence, and the operator can use
the complete flow through Settings or the API. Then verify the supported live
probes with disposable fixtures and record their actual scope and outcomes.
