# Conditional tool disclosure

Profiles default to `tool_disclosure = "eager"`: the model receives every effective
tool schema, with the existing prompt and ordering. Tool discovery is opt-in.
A profile can instead use `tool_disclosure = "deferred"` and an ordered
`tool_resident = ["memory.search", "sessions.search"]` list, provided those tools
are loaded and granted. Keep interactive profiles eager until the pinned model
passes the live evaluation below.

Deferred runs receive a static, alphabetically grouped index of granted tool names,
one-line summaries and write markers. Full schemas initially include `tools.search`,
`tools.load`, then resident tools in configured order. Search ranks lexical matches
in names, summaries, keywords and documentation, with exact names first;
`email_search` also matches `email.search`. It returns and activates up to five
matches. Load accepts up to five exact names, reporting loaded, already-loaded and
rejected names independently. New schemas appear on the next model request in
activation order. Search with no matches returns available families and suggests
loading an exact name.

Calling a granted tool before loading it returns a notice and loads its schema;
it does **not** execute the function. Calling it again executes normally. A load
followed by a real call in the same sequential tool group can execute that call.
Discovery and these notices do not consume the malformed-call repair budget.
Pure discovery groups have a separate eight-step cap; reaching it returns
`tool_error` with `tool loading did not converge`. Groups that contain real work
consume the profile's normal `max_steps`. `Run.steps` still counts model requests;
`tool_view.work_steps` and `tool_view.disclosure_steps` distinguish the budgets.

Disclosure changes schema visibility, never authority. Profile grants, job tool
ceilings, capabilities, scopes, timeouts and output limits still apply. The
`tools.*` namespace is reserved for runtime discovery; do not put it in profile
or job tool/resident lists. Meta-tools never enter wildcard grants.

## Job overrides

The job form, API and `jobs.create`/`jobs.update` support:

- `tool_disclosure`: `inherit` (default), `eager` or `deferred`.
- `tool_resident`: `null` (inherit profile residents), `[]` (discovery only in
  deferred mode), or an ordered replacement list of exact names.
- `tools`: the existing narrowing-only ceiling; `null` inherits grants and `[]`
  permits no real tools. Deferred mode with no grants still exposes discovery.

Explicit job residents must belong to the job's effective tools. Inherited profile
residents intersect the job ceiling automatically. Duplicate or ungranted residents
are rejected at save, including updates that change profiles. Existing stored jobs
survive later profile trimming: runtime intersections shrink safely and Operations
shows excluded tools/residents. Disclosure edits do not rearm consumed one-time jobs.
Sub-session jobs carry the job overrides; delegated workers resolve their own
profile defaults. Subsequent interactive runs resolve afresh without remembering
activations or inheriting a preceding job's overrides.

## Inspection and budgets

`sola check`, `/tools` and `/api/tools` report per-profile and per-job disclosure
sizes: effective/resident schemas, index, initial visible bytes and deferred count.
Warnings suggest review above 25% of context budget for deferred initial payloads,
or 50% for eager schemas. Nothing automatically switches modes or imposes a tool
count limit. Context checks and mid-run compaction use the **currently visible**
schemas, including newly activated definitions.

Use `/prompt?job=JOB_ID`, `/api/prompt?job=JOB_ID`, or `sola prompt --job JOB_ID`
to inspect the initial view without a model call. A profile-only inspection uses
its defaults. The index appears after the profile fragment and before run metadata.
Eager prompts contain no index.

Runs record mode, effective/resident names, index hash and activation steps.
The index is preserved in the immutable prompt bytes and prompt hash. The initial
schemas remain in `runs/ID.tool-schemas.json`; deferred runs additionally preserve
all granted schemas and discovery definitions in immutable `ID.tool-catalog.json`,
covered by `tool_catalog_sha256`. Reads verify both schema file hashes. Per-request
visible-name snapshots reconstruct schemas using that catalog, independent of
current code or grants. Use the step links on a run, `/api/prompt?run_id=ID&step=2`,
or `sola prompt --run-id ID --step 2`. Without `step`, inspection returns initial
schemas. Old records without disclosure metadata retain their historical behavior;
unrecorded schema inputs cannot be reconstructed. Include these files in backups.

## Live evaluation before interactive rollout

Automated tests use mock models and establish mechanics only. Before enabling
interactive deferred mode, compare eager/deferred runs on the pinned model using
the same tasks and profiles: inbox check, exact-quote history, semantic memory,
schedule a job, journal publish, print a note and scoped file read. Record first
substantive tool chosen, activation calls, total model steps, task success and
initial request bytes. Require every eager-successful task to succeed deferred,
median extra model steps on tool-using tasks at most one, and at least 50% initial
request-byte savings on the interactive profile. If discovery is unreliable,
interactive stays eager; jobs and future models can still opt in. Do not claim
live acceptance from configuration or mock tests alone.
