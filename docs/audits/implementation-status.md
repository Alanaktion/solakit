# Curated memory front page implementation status

2026-11-07 — CURATED-MEMORY-PLAN harness implemented.

Validation: `uv sync --frozen --all-packages` succeeded; 694 core/pack tests
passed (one existing Starlette/httpx deprecation warning); Ruff lint and format
passed; all nine distributions (core and eight packs) built as sdists and wheels.
`uv run --env-file .env sola check` reported Configuration valid. The strengthened
oversized-file run regression also passed in the targeted seven-test prompt suite.

Optional operator-owned `fragments/memory.md` is read live and verbatim between
SOUL and context, across profiles and independently of OpenViking enablement.
Absent input leaves the original golden unchanged; a separate synthetic golden
covers presence. Automated checks cover empty input, literal CRLF/trailing space,
live edits, invalid UTF-8, disabled memory, fragment ordering/hashes/counts,
offline cap errors/warnings and runtime reporting without truncation. New runs
retain immutable fragment breakdowns; historical inspection survives file deletion
and embedded separator text without consulting current files. Legacy runs have
no breakdown and return null.

Operator work remains: author/install the real front page and perform the plan's
fresh-session trivial-turn/vague-follow-up search and cold correction acceptance
checks. No real front page, template, new tool, automatic writer or widened
filesystem grant was installed. Mock tests establish assembly, not model uptake.
No browser visual QA was needed or performed; this change adds no UI layout.

Authoring guidance for the operator: keep facts flat and factual, with dated
entries and a last-reviewed header. Prefer one line per entry and pointers to
archive detail; paragraphs belong in the archive. Replace a corrected line in
the same edit rather than appending a contradiction. Prune dated history on each
review, using the operator's own review cadence. This is living content whose
revision is permitted, unlike published writing; optional fragments git history
can retain its revisions. In v1 Sola proposes changes in conversation and the
operator lands them with a text editor. Live outcomes remain unverified here.

# Tool layer implementation status

2026-11-06 — TOOL-FIXES-PLAN implementation

| Workstream | Implemented and automatically verified |
| --- | --- |
| Error contract | Dispatch probes preserve DST ambiguity/gap, naive-time and bad-date messages; unknown zones and invalid proxy URLs are expected errors with no tool-log traceback. Malformed GitHub/Cloudflare payloads fail explicitly. Remaining ValueError sites are config validators or private seams with converting callers. |
| Payload projections | Large realistic GitHub objects become parseable JSON under the default cap and at most 12000 characters; release bodies are bounded and flagged. Page slices reassemble 40000 characters, including an escaping-heavy case. Small caps produce valid projections or explicit errors. |
| Feed reads | Mock chains exercise redirects, private-target refusal before requests, five-hop limits, Authorization stripping and conditional 304 responses. Atom dates remain distinct. Metadata and User-Agent survive transport. |
| Schema metadata | All 49 tools have summaries and keywords; all 124 parameters have descriptions. Schema bounds reject bad counts/offsets before network access. The catalog snapshot measures 37863 schema bytes. |
| Discovery ranking | All seven plan queries pass against the full core/pack catalog; stopwords are filtered and field weights/thresholds prevent incidental docstring-only matches. Authority and activation semantics are unchanged. |
| Eager pressure | `sola check` reports actual profile schema sizes; disclosure docs explain the budget arithmetic and retain the live rollout gate. |

Validation: 608 core/pack tests pass; Ruff lint and format pass; all eight
workspace distributions build. `uv run --env-file .env sola check` reports
Configuration valid. Tests also cover invalid calendar bounds returning a named
domain error rather than the registry backstop.

Live transport probes, using the local configuration without changing the running
service, passed: ml-explore/mlx-lm PR #1982 produced complete projected JSON in
938 characters (two successful check runs); the BBC feed at
`http://feeds.bbci.co.uk/news/rss.xml` followed its redirect to HTTPS and returned
five entries in 1505 characters; ambiguous `2024-11-03T01:30:00` America/Denver
returned `Ambiguous local time; specify fold=0 or fold=1`.

The probes used this checkout directly, not the running instance. No service
restart/deployment or deferred-mode pinned-model evaluation was performed.
Interactive disclosure remains eager. Mock transports and deterministic ranking
tests do not establish model acceptance.
