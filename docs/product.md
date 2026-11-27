# Sola v2 product contract

Status: Phase 0 contract established on 2026-11-16. Product scope and session/deployment direction were confirmed in the Phase 0 conversation. Internal defaults below remain adjustable implementation decisions. See [decisions](decisions.md), [acceptance journeys](contracts/acceptance.md), and [migration inventory](migration/inventory.md).

## Purpose

Build Sola's local agent harness: persistent identity and memory, reliable multistep tool use, background work, and bounded proactive actions. Sola should finish permitted work or leave an honest, inspectable outcome. Personality and memory judgment belong to operator-owned SOUL and prompt fragments rather than hidden harness prose.

One operator uses the runtime on this Apple Silicon Mac through a web UI and HTTP API, over localhost or configured tailnet access. Each configured instance has one process, one event loop, one worker, and one shared inference semaphore. [LaunchAgents](cli.md#run-sola-at-login) can run instances with separate configuration files, data directories, and HTTP bindings. Qwen stays pinned through oMLX; OpenViking supplies semantic memory. FastAPI handles HTTP/lifespan; plain async Python application services own execution. APScheduler wakes the in-process scheduling service; SQLite owns schedules.

## First-release scope

- Conversation with stable topics, streaming progress, cancellation, offline-searchable history, and inspectable requests/tool receipts.
- Shared memory, isolated sessions without OpenViking access, and ephemeral runs with no continuing context but retained execution evidence.
- Basic file listing, reading, searching, and guarded writes in granted workspace roots. Transcript search, status, and scoped memory operations support the harness.
- Image generation through explicitly configured existing workflow resources, with artifact references and submission receipts. Image attachments are in scope; actual pinned-model support is a Phase 4 gate, with an explicit unsupported response if unavailable rather than a model substitution.
- Attach UTF-8 documents in chat. The configured oMLX API can also convert PDF,
  DOCX, and PPTX when it advertises MarkItDown.
- Operator-enabled scheduled work and proactive wake-ups, using the same run loop, grants, budgets, and evidence as interactive work.
- Preservation of existing history, identity/config fragments, operational data, and legacy effect receipts, including data belonging to integrations deferred from v1.

The operator subsequently approved optional high-level blog and dream tools;
see the [blog guide](blog.md). They use a fixed Markdown/build/commit/push service
without exposing shell access. Other domain packs for letters, email, journal,
calendar, and printer remain deferred. Legacy image-generation behavior is
reference material; v1 needs one reliable configured workflow, not every legacy
option. A separate operator opt-in enables [literal interactive commands](contracts/tools.md#literal-command-execution).
An operator can enable one isolated research worker for
[bounded delegation](contracts/tools.md#bounded-delegation).
Android compatibility, Discord parity, browser/computer control, and independent
agents are deferred.
No domain execution logic belongs in the run loop.

## Proactive authority

The operator enables wake-ups and records or approves commitments. Sola can inspect bounded relevant context, do permitted work, leave a message, or do nothing. Autonomous runs may not create, rearm, enable, or reschedule wake-ups, directly or through another tool. An autonomous origin remains attached to any descendant work; a model cannot relabel it interactive. Interactive scheduling requests still require the configured job-management authority.

Confirmed starting settings: America/Denver; quiet hours 22:00 inclusive to 08:00 exclusive; local preparation may continue; proactive notifications and externally visible outbound effects wait; no more than one proactive notification per topic per hour; new wake-ups start disabled. Quiet hours apply to autonomous activity, not an explicit immediate interactive request. At quiet-hour end, coalesce deferred notifications and discard stale ones instead of bursting a backlog. The visible topic message is itself a notification, so defer its publication too; private run evidence remains available.

Initial engineering defaults, configurable downward/upward by the operator: one autonomous run at a time; at most one wake per topic per hour and four admitted wakes globally per rolling hour; eight model calls, twelve tool calls, and ten minutes active elapsed time per wake. Queue wait before start does not count against active elapsed time. No autonomous successors: consecutive autonomous-chain depth is one. Apply cooldown/rate counters durably across restarts. Later evaluation may tune these values with evidence without broadening capabilities.

A wake sees its explicit task/commitment, bound-session context and relevant scoped recall. It cannot scan all private topics or resources simply because it is proactive. Allowed outputs stay in that bound topic or a specifically configured artifact root. No external notification channel is required for v1. No-action is a successful evaluated wake with evidence, not an empty user-facing chat message.

Phase 9 selects eight pending autonomous admissions, 24 admissions per rolling
24 hours, one-hour cron misfire grace with coalescing, and 24-hour notification
expiry. These are reversible engineering defaults in `[background]`; hourly,
daily and topic counters survive restart. Both automatic scheduled and proactive
origins share these bounds. Explicit run-now is separately attributed and bypasses
automatic admission-rate and quiet-hour limits while retaining grants, run budgets,
global pause and overlap controls. It works with the job's schedule off. See the
[job contract](contracts/jobs.md).

## Durability and honest outcomes

Immutable JSONL owns conversation/execution evidence, state.db owns operational coordination, and disposable search.db is rebuilt entirely from JSONL. Backups must include state.db and the evidence tree. Compaction selects a summary plus recent context without changing source evidence. Archives remain readable/searchable; archival does not delete data.

Durable admission precedes an accepted receipt. Tool intent precedes a consequential call; a receipt follows it. A broken response, unattempted action announcement, or ambiguous external effect cannot become an unconditional success claim. No universal exactly-once external-effect guarantee is possible: use provider idempotency or visible uncertainty, and never retry an uncertain non-idempotent operation blindly.

A recall outage may produce a labelled degraded conversation; it does not erase identity or history. A reply may be complete while memory extraction remains pending. A disconnected client or timed-out wait leaves accepted work running. Explicit cancellation and process interruption have distinct outcomes.

## Deployment boundary

Target Python 3.15 on macOS arm64 and a local filesystem supporting SQLite, advisory locks, atomic replacement, and file/directory sync. Initial smoke probes passed on Python 3.15.2; see [evidence](validation/phase-00.md). This is a target, not a claim of full dependency/model compatibility. Phase 1 locks the environment, Phase 4 verifies oMLX, and Phase 7 verifies OpenViking. Network filesystems, containers, multiple workers, and other platforms are not release gates.

Bind localhost by default; tailnet exposure is an explicit configuration choice with operator authentication. Tailnet membership does not bypass authentication. No production service is started or changed by Phase 0.
