# Operator diagnostics and configuration review

Put setup guidance, health, and authority review in the authenticated API and
the existing Settings tabs. This covers the everyday jobs of v1 `init`,
`doctor`, `config explain`, `config diff` and `tools residents`. Use the
[longer probe plan](NEXT-OPERATOR-EVALUATIONS.md) for model capability tests,
memory extraction tests and evaluations.

## Diagnostics and setup

- Reuse `GET /api/v1/health` for runtime, queue, scheduler and memory observer
  state. Show a compact Diagnostics view with the source and age of each
  observation, and distinct states for unavailable, disabled and unchecked.
- Add authenticated `POST /api/v1/diagnostics/checks` for an explicit bounded
  availability check against the configured text model, optional vision model,
  and memory service. Reuse the checks behind
  [`sola check --live`](../cli.md) and the app's clients. Return per-service
  reachability, pinned API identity or provider version where available, and
  a safe error code. Label listed model IDs as availability checks. Pixel and
  tool competence come from their respective probes. Allow one check at a time
  and reuse its result in the UI until the operator refreshes it.
- Link setup help from Diagnostics. Package the existing configuration example
  as a downloadable, secret-free starter. Show required configuration and
  missing credential references by name or purpose, as appropriate. The browser
  applies to running instances. Use the existing offline `sola check --explain`
  for failures that prevent startup.

## Effective authority and evidence

- Extend **Profiles & tools** using the current `/profiles` and `/tools`
  views. Show profile inheritance, workspace roots, effective grants, tool
  schema sizes, memory mode, and budgets in the same units used at dispatch.
  Keep missing or disabled integrations visible with a short reason.
- Add `GET /api/v1/diagnostics/authority` using the existing resolver and
  frozen admission snapshots. Compare either two configured profiles or a
  recorded run with its current profile. Return added and removed tools and
  roots, changed limits, and provenance. Show the comparison beside Profiles
  & tools and link to it from run evidence. Fetch the recorded model parameters
  and context accounting through existing prompt evidence. Keep configuration
  editing in the operator's local workflow.
- Add `GET /api/v1/diagnostics/tool-usage` for tool attempts, successes, and
  last use over a small bounded period for a selected profile. Derive the
  counts from receipts in recent runs, label sample size and limits, and show
  results beside eager schema sizes. Add a projection if measured cost warrants
  one. The report is advisory and helps the operator narrow grants.

## Acceptance

Test authentication and CSRF, a service timeout, model mismatch, disabled
services, provenance, changed grants since admission, and redaction of a test
secret. Check that available results survive another service's failure. Verify
sample bounds and that rejected attempts aren't counted as successful usage.
Exercise Settings in the browser with a fixture and inspect the same responses
headlessly through the authenticated API.
