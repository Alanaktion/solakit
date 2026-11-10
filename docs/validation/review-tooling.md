# Code review tooling completion

November 10, 2026. Addresses T1–T3 and T5 from the code review.

## Changes

Regression CI explicitly selects Python 3.12 and 3.13, retains scoped mypy,
and publishes per-version coverage XML even after test failures. Coverage includes
unexecuted core and pack modules and is reporting-only. Ruff now enables B and
ASYNC, with narrow documented exceptions for immutable schema defaults, explicit
operation timeouts and polling that cannot use an asyncio event. Tool schema
inputs and defaults remain unchanged.

Existing review regressions cover Git metadata write refusal, empty workspace
roots, cross-origin requests, worker retirement, bounded regex grep and blank
Discord chunks. A new scheduler-level test fires an ephemeral job three times,
checks distinct completed sessions, and verifies execution queues, workers and
locks return to empty after each dispatch.

All workflow actions are pinned to full commits verified against upstream release
tags. Dependabot checks uv and GitHub Actions weekly. A separate audit matrix
exports locked production dependencies from every workspace member, excluding
local workspace entries, and audits them without resolution or installation.
Both Python versions are audited so version-specific dependency markers are
covered. Vulnerabilities and audit errors fail CI; no advisories are excluded.
Audits run on main pushes, pull requests, manual dispatch and weekly scheduling.

The uv configuration follows the [official integration guide](https://docs.astral.sh/uv/guides/integration/dependabot/).
Audit flags follow the [pip-audit documentation](https://github.com/pypa/pip-audit#usage).

## Local validation

| Check | Result |
| --- | --- |
| Frozen all-package sync | Passed in fresh Python 3.12.16 and 3.13.18 environments |
| Core and pack tests, Python 3.12 | 993 passed, 3 warnings, 100.43 seconds |
| Core and pack tests, Python 3.13 | 993 passed, 537 warnings, 88.35 seconds |
| Coverage, both versions | 91% rounded; 11,225 statements, 1,053 missed |
| Scoped mypy, both versions | Passed, 33 source files |
| Ruff lint and format | Passed with B and ASYNC enabled |
| Production dependency audit, both versions | No known vulnerabilities found |
| Offline sola check | Passed after loading operator environment |
| All distribution builds | Passed; core wheel remains pack-free |
| Workflow and Dependabot YAML | Parsed; both matrices and full action SHA shapes checked |

Coverage XML includes all eight pack source roots (including common support) and
core. Raw local reports are `/tmp/solakit-coverage-312.xml` and
`/tmp/solakit-coverage-313.xml`; test logs are `/tmp/solakit-tests-312.log` and
`/tmp/solakit-tests-313.log`.

Warnings include Starlette's existing TestClient deprecation; Python 3.12 also
reported the audioop deprecation and an unraisable subprocess transport cleanup
exception. Python 3.13 surfaced unclosed SQLite connections during test cleanup.
These warnings are not suppressed.
Offline validation also warns that the operator's curated memory fragment is
approaching its configured cap. Pip-audit emitted advisory notices about unhashed
input and ignored incompatible cached responses; both audits completed cleanly.

These are local macOS checks, not a claim that hosted Linux CI or Dependabot has
already run. Browser visual QA and live model/integration checks were not needed
for these tooling changes. No prompt or tool schema behavior changed, so no model
evaluation comparison was run. T4 and T6 remain outside this change. The operator-local review document was
updated in place; `docs/plans/` is excluded locally and is not added to Git.
