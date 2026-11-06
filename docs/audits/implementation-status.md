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
