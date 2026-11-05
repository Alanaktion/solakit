# Phase 6: OpenViking integration and context compaction

Status: local implementation complete; live round-trip pending

Depends on: phase 5

- Opt-in scoped recall before final prompt construction and persistence.
- Post-turn commits with intent journals and refusal of ambiguous write replays.
- Extraction task polling and explicit terminal failure/status-unavailable states.
- Completed OpenViking summaries with self/peer long-term extraction disabled for
  compaction, preserving whole tool groups and previously recorded boundaries.
- Numbered immutable transcript archives, prepared rotation manifests, and
  recovery before startup serves requests; archives survive index deletion.
- Conservative serialized UTF-8 context guard, never reported as endpoint tokens;
  context_limit instead of silently sending oversized context or dropping history.
- Authenticated archive/run inspection and explicit scratch verification API/CLI.

Automated checks cover repeat compaction, failed/pending summaries, rotation crash
recovery, tool groups, index rebuilds, version 0.4.17.1 archive shapes, extraction
failure visibility, and a probe that cannot pass from a model's claim alone.

## Live exit gate

The operator supplied `http://127.0.0.1:1933`, server `0.4.17.1`, root
`viking://user/sola/memories`, and secret reference `OPENVIKING_API_KEY`.
This workspace has no listener or credential binding for those settings. The
source contract at the exact upstream tag was checked; the live check was not run.
Follow [first contact](../operations/first-contact.md) on the Sola host and retain
the probe's run IDs/results. This gate precedes any Discord pilot or live job.

Mocks establish implementation behavior, not live compatibility. Ambiguous prior
writes still require operator reconciliation; polling never replays them.

## Compaction controls follow-up

Profile `mid_run_compaction` is opt-in (default false). At a context overflow after
a complete tool group, the runner summarizes and resumes with the original task
reminder, preserving counters and checking the new request size. Full run messages
are read across archives for normal memory commits. Explicit idle compaction is
available through API/UI, independent of that flag. Rotation preparation/commit
is one durable operation and failed prepared state blocks writes until recovery.
See [21: compaction controls](21-compaction-controls.md). Live verification is still
pending; these additions are tested against the existing mocked server contract.
