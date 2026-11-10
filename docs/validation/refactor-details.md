# D9–D12 refactor fixes

November 10, 2026. Baseline: `a61ef2f` (completed D1–D8 refactor).

## Changes

- **D9:** preserve existing model and configuration timezone defaults, as requested.
  Omitted TOML/API job timezones inherit Config.timezone before schedule
  validation. API creation uses a field-only JobCreate request model, then the
  manager supplies the timezone and validates the full Job. This avoids premature
  DST rejection or UTC conversion in the old default zone. Explicit zones win;
  saved definitions and task-only edits retain their resolved timezone. Existing
  definitions require no migration. HTTP field shapes and response fields remain
  the same; OpenAPI now names the creation input JobCreate and describes inheritance
  without a misleading fixed timezone default.
- **D10:** process output limits count decoded Unicode characters per stream.
  Incremental UTF-8 decoding handles split reads; retained output never splits a
  valid multibyte character. Completion flags reflect character limits. The
  service copies caller-provided environment dictionaries before supplying PATH.
  Process-group cancellation and bounded draining retain their existing behavior.
- **D11:** protect the actual installed solakit package. A verified src-layout
  checkout also protects its project root. Installed wheels no longer interpret
  the interpreter's entire lib/pythonX.Y directory as the source tree. Resolved
  paths preserve symlink checks.
- **D12:** root-relative find patterns match directory components independently.
  `docs/*.md` selects direct children; `docs/**/*.md` includes nested files.
  Basename-only patterns still search recursively. A complete ** component spans
  zero or more directories; other wildcards cannot cross directory separators.
  Matching uses iterative state transitions, without recursive call depth.
  Tool help, generated reference and operator documentation explain the behavior.

## Validation

- Frozen all-package workspace sync passed.
- Full core and pack suite: **992 passed**, using the checkout's CPython 3.15.
- Mypy: 33 checked files, no errors. Ruff lint/format checks passed.
- Operator `sola check` passed using existing secret references.
- All nine workspace distributions built. Core wheel packaging stays pack-free.
  A subprocess imported the extracted wheel and verified that sibling operator
  data is allowed while data inside the installed solakit package is refused.
- The tool-catalog snapshot retains 67 tools and 172 parameters; schema size
  changes from 53,010 to 53,205 bytes for the expanded find help.

The ten added tests cover inherited and explicit zones, ambiguous one-time dates
under different configured zones, persistence across restart and task edits,
OpenAPI timezone metadata, multibyte truncation and read boundaries, unchanged
caller environments, source/wheel layout discrimination and directory-aware glob
matching. Existing shell cancellation, scheduled-job, service, API and UI form
regressions also pass. No new browser visual QA was run for these service/API
changes. The existing Starlette TestClient deprecation warning remains.

## Real-model comparison

Two isolated find-by-name trials per version, using the same pinned local model
and configuration hash f57882e3e94f. Baseline source came from an unmodified
archive of a61ef2f; the candidate used the working refactor. Memory and external
integrations were disabled; real file operations stayed inside seeded temporary
workspaces.

```sh
sola eval --only find-by-name --trials 2
sola eval compare <baseline.json> <candidate.json>
```

| Scenario | Baseline | Candidate | Pass-rate delta; 95% bootstrap interval |
| --- | ---: | ---: | --- |
| find-by-name | 1/2 | 2/2 | +0.50; [0.00, 1.00] |

The failed baseline exhausted its step budget and omitted the required files.find
call. Both candidate trials completed and satisfied the scenario. With only two
trials and an interval including zero, this is not evidence of a reliable quality
improvement. Raw model/prompt evidence remains in the local private temporary
solakit-d9-d12-eval-before and solakit-d9-d12-eval-after directories.

## Compatibility

No configuration defaults, existing job instants, saved evidence, integration
activation or Hermes schedules were changed. Clients omitting a job timezone now
receive the configured zone. Clients that relied on docs/*.md traversing nested
directories must use docs/**/*.md. ASCII output limits retain their prior meaning;
non-ASCII output now uses the advertised character budget.
