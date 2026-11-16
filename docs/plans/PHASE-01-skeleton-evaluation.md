# Phase 1 — Development skeleton and evaluation baseline

Status: implemented with a reproducible model-only baseline. Local toolchain, wheel, deterministic corpus, and replay gates passed; the measured baseline does not meet the future Phase 6 reliability thresholds. See [Phase 1 evidence](../validation/phase-01.md). Depends on [Phase 0](PHASE-00-product-contracts.md).

## Outcome

An installable, pack-free core and an evaluation runner that distinguishes completing work from merely explaining why it failed. Preserve raw evidence so model and harness changes can be compared fairly.

## Implementation slices

1. **Make the bootstrap usable.** Confirm the Phase 0 interpreter, replace placeholder package metadata, expose `sola`, and add a reproducible uv lockfile. Establish only the packages needed for the current slice. Configure pytest, Ruff, and typing checks; CI installs from the lock. Define workspace membership without making packs dependencies of the core wheel.
2. **Build a server-independent evaluator.** Use versioned case records with case ID, input/history fixture, tool schemas, fixture resources, expected behavior, trial count, and scoring rules. A model-only baseline driver can precede the new run loop; later replace it with the same application entry point production uses. Capture model ID, server version, parameters, prompt/schema hashes, raw outputs, receipts, timing, and scorer version.
3. **Collect representative cases.** Include zero-argument runtime tools, multistep file tasks, historical announcement-only and reasoning-only replies, normal conversation, quoted/hypothetical announcements, denied calls, missing tools, malformed arguments, memory outage, and interrupted generation. Use disposable files and fake effect destinations. Exact output prose is not the oracle: inspect task artifacts and receipts.
4. **Capture and compare baselines.** Run the corpus against pinned Qwen. A legacy harness comparison is useful only with matched prompt, tools, sampling, and model where possible. Hermes/Open WebUI comparisons are optional controls, not dependencies. Label unmatched conditions. Keep live-service gates pending if the model host is unavailable.
5. **Freeze scoring and thresholds.** Publish per-case and aggregate results with denominators and multiple trials. Choose thresholds before candidate measurements. Proposal for deferred-schema experiments: no lost paired-case successes, median extra model steps <=1, and >=20% schema-byte reduction on tool-heavy cases; these are new proposed gates, not recovered source requirements. Keep eager mode if evidence is insufficient.

## Metrics and artifacts

Record task completion, first-tool accuracy on applicable cases, silent unattempted tasks, detected narration failures, false continuations on no-tool cases, repair counts, model steps, latency, prompt/schema bytes, tokens when supplied, and incorrect/duplicate effects. Distinguish task-level failure from an expected denial. An honest failed run improves observability but does not count as task completion.

Deliver `evals/cases/`, disposable fixtures, developer runner/comparison scripts, baseline report, CI, and a minimal wheel smoke test. Legacy `docs/completion-check-evaluation.md` is a source of regression hypotheses; its tiny trial counts are not proof of a working continuation policy.

## Verification and exit gate

Install the wheel in a clean environment and verify the CLI entry point and absence of pack modules. Run a deterministic fake-adapter corpus to validate scoring, including an intentionally incorrect success and a duplicate effect. Replay saved raw results through the scorer with identical outcomes.

Exit requires a reproducible real-pinned-model baseline with linked evidence. CI and fake results may pass independently while that live gate remains pending. No baseline trial may send production mail, publish content, print, or commit production memory.
