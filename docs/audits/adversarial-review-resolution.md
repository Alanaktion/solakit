# Adversarial review resolution

Review baseline: `214aba4`. This follow-up does not advance phase 6 or enable any
integrations. Prompt whitespace and transcript history remain literal.

- **M1:** Complete tool outputs use counted truncation markers, including the
  characters removed to fit the marker within the output budget. Bounded file
  reads report an explicit lower bound, not a fabricated full character count.
  The tool log distinguishes observed counts, exact totals, and minimum omissions.
- **M2:** Keep session transcripts and prompt files verbatim, consistent with
  archival and prompt-audit requirements. SPEC §12 and README now explicitly
  distinguish these private archives from redacted diagnostic records. Tests
  cover original user text, tool arguments/results, subsequent model history,
  and redacted run/tool logs with a fixture credential.
- **M3:** Validate required SOUL/profile files as readable UTF-8 at startup, before
  storage or network clients are allocated. Empty profile files remain valid;
  missing ones fail clearly. Require the recall header when memory is enabled.
  Continue to read files live for each run.
- **N1/N2:** Retain the newline-terminated recall header and empty-but-present
  context fragment exactly. Document the resulting blank lines/separators and
  keep byte-level regression tests. No whitespace normalization was introduced.
- **N3:** Retain case-insensitive suffix matching, as the spec's globs require.
  Document and test that a key such as `monkey` is also redacted.
- **N4:** Share offline startup validation with `sola check`, which prints concise,
  actionable errors without a traceback or echoed invalid secret values. Uvicorn
  still prints its standard lifespan traceback when startup fails, ending with
  the actionable ConfigurationError; the CLI check is the operator preflight.
- **N5:** Model/memory clients default to explicit `trust_env = false`. Operators
  can opt into environment proxy/CA settings per service. The API CLI has an
  equivalent `--trust-env` switch. Process environment and other tools' networking
  are untouched. Tests cover malformed bracketed-IPv6 NO_PROXY values and both
  values of the explicit service setting.

This is a historical resolution record, not an active plan. Extraction tracking,
compaction and channel/job implementations have since shipped. The operator has
confirmed the live OpenViking round-trip. See the [current audit](implementation-status.md)
and [remaining plans](../plan/README.md) for outstanding validation and rollout.
