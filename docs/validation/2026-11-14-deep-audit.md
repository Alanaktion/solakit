# Solakit deep audit — 2026-11-14

What this repo does: Solakit runs a personal assistant against one pinned local
model, with durable conversation evidence, optional external memory, granted
tools, scheduled work and an operator UI. This review assumes the documented
single operator, single uvicorn worker and localhost/tailnet access.

The user requested both an audit and practical refactoring. The five findings
below are fixed in this change. No dependencies or configuration options were added.

## Must fix

1. **Valid structured answers fail when they contain `data`**
   (`src/solakit/core/structured.py:91`).
   - **What this is:** A job can render its validated JSON answer through a
     template, with object fields and the whole answer available as variables.
   - **Problem:** An answer such as `{"data":"nested","title":"Report"}`
     passed `data` twice to Jinja, raising `TypeError` and failing the run.
   - **Fix:** Pass one context mapping. Reserve `data` for the whole value;
     `data.data` accesses the object's own field. Object and array regressions pass.
   - **If we skip it:** Correct model output still fails instead of being delivered.

2. **Broken calibration cache entries can fail real runs**
   (`src/solakit/core/store_sidecars.py:22`, `:186`, `:194`).
   - **What this is:** The optional calibration cache estimates tokens from bytes
     for context-budget checks and learns from successful model responses.
   - **Problem:** Syntactically valid JSON such as an entry with a zero ratio,
     a string, missing fields or a malformed sample count was trusted. Reads
     could divide by zero or use the wrong type; updates could crash as well.
   - **Fix:** Validate both reads and updates with one small shared check. Ignore
     invalid entries and relearn them, preserving other model entries.
   - **If we skip it:** An advisory cache can prevent work from completing until
     the operator repairs it. Ten malformed-entry cases now have regressions.

3. **Invalid job schemas are accepted before execution**
   (`src/solakit/core/structured.py:18`).
   - **What this is:** Job saves validate the supported output-schema subset.
   - **Problem:** Only the top-level type and size were checked. For example,
     `{"type":"object","properties":[]}` was accepted, then failed at
     `.items()` after inference; `{"type":[]}` raised `TypeError` at validation.
   - **Fix:** Walk nested schemas and validate the shapes of supported fields
     before accepting the job. Eight malformed-schema cases now raise `ValueError`.
   - **If we skip it:** Jobs can be saved successfully but repeatedly fail when
     their model response is checked. Previously saved malformed definitions need
     correction under the stricter validation.

## Should fix

4. **Every completed channel delivery scans the entire conversation**
   (`src/solakit/channels/__init__.py:226`).
   - **What this is:** Delivery retrieves the stored final answer for one run.
   - **Problem:** It read and validated every active and archived message before
     filtering by run. Work and temporary memory grew with the topic's lifetime.
   - **Fix:** Use the existing indexed `Store.run_messages` reader. Reuse the
     already loaded session for artifact checks. A regression rotates the answer
     into an archive and verifies delivery without a full-session scan.
   - **If we skip it:** Long-lived channel topics repeatedly pay for all their
     history. The existing reader retains its validated scan fallback if SQLite
     is unavailable; the first stale-index refresh can still scan changed files.

5. **Grant resolution repeats its own work for every tool**
   (`src/solakit/tools/__init__.py:669`).
   - **What this is:** The registry computes tools granted to a profile and
     intersects them with a narrower per-run selection.
   - **Problem:** For every name returned by `tool_names`, it called `allowed`,
     which expanded the same profile grants again. With N tools and no selection,
     this expanded the grants N+1 times instead of once.
   - **Fix:** Remove the redundant membership check. Existing wildcard, toolset,
     access-tier and dispatch tests verify the authority checks remain intact.
   - **If we skip it:** Catalog, disclosure and admission repeatedly spend CPU on
     an answer they already computed.

## Scope and verification

Reviewed the architecture and behavioral contracts, dependency/build/CI setup,
app lifecycle and authentication, execution admission and cancellation, delegation,
tool dispatch and grants, filesystem/Git/process services, transcript and sidecar
storage, message offsets/search, rotation, memory observation, scheduling and
delivery. Inspected UI authentication, internal-client isolation and Markdown
sanitization. Sampled remote-read controls and calendar, printer, letters and
email side-effect paths, plus shared pack receipts and locks. This was a
risk-focused review, not a line-by-line review of every file.

The baseline was 1,041 passing core/pack tests. The 20 added regression cases
reproduced the defects or unwanted full scan before their corresponding fixes.
Focused checks passed after the fixes. Final checks:

- Full core and pack pytest suite: 1,061 passed.
- Ruff lint and formatting: passed.
- Configured mypy subsystems: passed (34 source files).
- Offline `sola check`, with the operator environment loaded: passed; existing
  front-page warning remains (7,143 characters against an 8,192-character cap).
- Source and wheel builds for core and all eight packs: passed.
- Locked production dependency audit: no known vulnerabilities found.

The first final-suite attempt had 1,060 passes and one sandbox failure: the
distribution test's nested `uv build --offline` could not access uv's cache.
The suite was rerun with cache access enabled.

Concurrent edits to the model SDK transport, its tests, `pyproject.toml`,
`uv.lock` and `SPEC.md` were present during validation. They were left untouched
and excluded from this audit's commit. Test results describe the shared working
tree at validation time, including those concurrent edits.

Verdict: the reviewed paths retain the app's small single-operator design;
fix malformed derived inputs at their boundary and reuse the existing indexes
and grant resolution rather than adding new layers.

Lean: three redundant operations removed; zero dependencies added. Runtime
source grows by 35 lines overall because the missing input checks need code.

Not checked: live model/OpenViking behavior, Discord/mail/publishing/printer
effects, browser visual or accessibility QA, every pack implementation line,
real long-history timing, and Python 3.12 execution (local tests used 3.13).
No prompt or tool-schema behavior changed, so no real-model eval compare was run.
