# Sola v2 implementation plans

These plans expand [OUTLINE.md](OUTLINE.md) into fourteen implementation phases for Sola's agent harness. They are planning documents, not claims that implementation or live validation has happened. Read the outline's final decisions as overriding its earlier Android and storage assumptions. The old runtime is migration and behavioral evidence, not the architecture to reproduce.

## Build sequence

| Phase | Plan | Result |
| --- | --- | --- |
| 0 | [Product and contracts](PHASE-00-product-contracts.md) | Scope, journeys, decisions, migration inventory |
| 1 | [Skeleton and evaluation](PHASE-01-skeleton-evaluation.md) | Installable core and reproducible baseline |
| 2 | [Records and configuration](PHASE-02-records-configuration.md) | Shared records, lifecycle, effective authority |
| 3 | [Durable evidence and search](PHASE-03-evidence-search.md) | JSONL evidence, state.db, disposable search.db |
| 4 | [Model and prompts](PHASE-04-model-prompts.md) | Pinned Qwen adapter and exact request evidence |
| 5 | [Tools and permissions](PHASE-05-tools-permissions.md) | Public SDK and dispatch enforcement |
| 6 | [Run loop](PHASE-06-run-loop.md) | Reliable headless multistep work |
| 7 | [Memory and config migration](PHASE-07-memory-migration.md) | OpenViking recall and tracked extraction |
| 8 | [Context and compaction](PHASE-08-context-compaction.md) | Long sessions without rewriting history |
| 9 | [Jobs and proactive work](PHASE-09-background-proactive.md) | Durable scheduled and bounded autonomous work |
| 10 | [API, authentication, CLI, events](PHASE-10-api-cli-events.md) | Clean client contract |
| 11 | [Chat web UI](PHASE-11-chat-ui.md) | Streaming conversation and Settings |
| 12 | [Basic tools and image workflows](PHASE-12-packs-workflows.md) | Daily-use candidate |
| 13 | [Migration and cutover](PHASE-13-migration-cutover.md) | Recoverable production transition |

Execute in numbered order. Later-phase contracts can be drafted earlier, but their gates require their dependencies. Phase 6 is the headless milestone; Phase 9 adds durable personal agency; Phase 12 validates basic file/image workflows and produces the release candidate; Phase 13 imports legacy data.

## Follow-up tool plans

These plans propose additions to the shipped v2 tool set. The initial release
scope and the later blog/dream decision are recorded in the [decision register](../decisions.md).
Web search and wiki lookups can land independently. Letters reuse the existing
Git checkout adapter. Email and shell have no dependency on that work.

| Plan | First tool surface |
| --- | --- |
| [Letters](NEXT-TOOLS-LETTERS.md) | `letters.inbox`, `letters.read`, `letters.send`, `letters.reply` |
| [Email](NEXT-TOOLS-EMAIL.md) | `email.search`, `email.read`, `email.reply`, `email.send` |
| [Web search](NEXT-TOOLS-WEB-SEARCH.md) | `web.search` |
| [Wiki lookups](NEXT-TOOLS-WIKI.md) | `wiki.summary` |
| [Git](../contracts/tools.md#configured-git-checkouts) | `git.status`, `git.diff`, `git.pull`, `git.publish` |
| [Shell](NEXT-TOOLS-SHELL.md) | `shell.exec` |

Use one typed input and a short description for each tool. Use the existing
registry, eager schemas, dispatch, result limits, and receipts. Put provider
and process logic behind the matching named tools using internal services.

Use one operator-owned configuration section per integration. Empty or disabled
configuration leaves its tools ineligible. Within enabled integrations, check
capabilities such as configured SMTP separately. Apply eligibility consistently
to prompt disclosure and dispatch. Exact grants are preferable for outbound
mail, Git mutations, letters, and shell. Existing grants can include wildcard
matches, so document that enabling an integration can expose its tools to those
profiles at new admission. Shell requires a literal grant. Freeze enabled
resource identity with the admission and recheck it and current grants when the
matching tool is invoked. Default older admissions to disabled for these new
integrations. Use credential references in configuration snapshots and keep raw
values private to the adapter and redactor.

Treat search hits, mail, letters, and command output as external content. For
read-only network calls, add a private dispatcher check that revalidates the
invocation and registers any credential with the redactor before I/O. Record the
normal tool attempt and receipt. Update the [tool contract](../contracts/tools.md)
to describe this read path and keep effect markers for mutations and sends.
Unresolved effects stay uncertain on restart. Tools never create wakes.

Validate each slice with focused dispatcher tests, a disposable integration
fixture, and a short model acceptance scenario. Keep live checks separate and
record which ones ran under `docs/validation/`. When implementing a plan, update
the configuration example, tool contract, and decision register for that slice.

## Agent and operator follow-up plans

| Plan | First usable workflow |
| --- | --- |
| [Sub-agent delegation](NEXT-AGENT-DELEGATION.md) | Hand one bounded research task to an isolated child and inspect its report. |
| [Follow-ups](NEXT-AGENT-FOLLOW-UPS.md) | Continue an existing child or schedule a one-time follow-up in the current topic. |
| [Separate vision model](NEXT-VISION-MODEL.md) | Attach an image and ask a question while keeping the conversation model text-only. |
| [Operator diagnostics and configuration review](NEXT-OPERATOR-DIAGNOSTICS.md) | Check deployment health, inspect effective authority, and compare it with a recorded run. |
| [Operator probes and evaluations](NEXT-OPERATOR-EVALUATIONS.md) | Run fixed synthetic checks and compare results in Settings and the authenticated API. |

Delegation precedes child continuation. Timed follow-ups reuse the current
scheduler and can land independently. The vision adapter precedes its live
pixel-recognition probe. Operator diagnostics can land before evaluation runs.

## Phase 0 contracts

Phase 0 is complete with bounded later-phase verification. Read [product scope](../product.md), [decision register](../decisions.md), [session semantics](../contracts/session-modes.md), [acceptance journeys](../contracts/acceptance.md), [inventory](../migration/inventory.md), and [evidence](../validation/phase-00.md). The operator subsequently narrowed v1 to basic file access and image generation; domain packs, delegation, and shell are deferred. This scope supersedes the earlier broad pack-port sequence in the outline. Historical data from deferred integrations must still be preserved.

## Confirmed architecture

Phase 2 records, effective configuration, and resource ownership are implemented
and locally verified. Read the [runtime contract](../contracts/runtime.md) and
[evidence](../validation/phase-02.md) before implementing the Phase 3 storage
protocol. No live client or durable admission/dispatch is claimed by Phase 2.

Phase 3 durable evidence, operational state, session services, independent offline
search, fixture preservation and coherent backup/restore are implemented and
locally verified. Read the [storage protocol](../contracts/storage.md),
[maintenance runbook](../backup-restore.md) and [Phase 3 evidence](../validation/phase-03.md).
Dispatch/live services and semantic production migration retain their later gates.

Phase 4 deterministic prompt assembly, private exact request evidence, offline
inspection and the pinned model adapter are implemented. The chosen Qwen passed
live oMLX text/tool/reasoning and synthetic image probes in both transport modes.
Read the [request contract](../contracts/model-prompts.md) and
[Phase 4 evidence](../validation/phase-04.md). Weight identity, verified tokenizer
accounting and production attachment admission retain their documented limits.

Phase 5 explicit SDK/registry, frozen/current authority enforcement, guarded file
services, durable dispatch/receipts, scoped transcript search and status are
implemented and locally verified. The separately installed example pack uses
only the public SDK, including a fake image adapter. Read the
[tool contract and access matrix](../contracts/tools.md) and
[Phase 5 evidence](../validation/phase-05.md). The conversation loop, scheduled
admission, live memory and real image workflow retain Phases 6/9/7/12 gates.

Phase 6 adds the headless run service, bounded queues, complete tool-history
projection, cancellation and bounded reliability recovery. Read the
[execution contract](../contracts/run-loop.md) and
[Phase 6 evidence](../validation/phase-06.md). Deterministic checks and live corpus
results are reported separately. The final eager candidate passes the declared
reliability thresholds; deferred disclosure fails its paired gate and stays disabled
in production.

Phase 7 adds OpenViking recall (stable >=0.5.5,<0.6.0; live baseline 0.5.5), exact note staging, asynchronous durable
commit/extraction observation, copied-configuration migration and reconciliation.
Read the [memory contract](../contracts/memory.md), [runbook](../memory.md), and
[Phase 7 evidence](../validation/phase-07.md). The live test passes extraction and
fresh-session retrieval in the operator-approved `sola-test` account. Production
configuration/data migration and cutover retain their Phase 13 gate.

Phase 8 adds conservative component accounting, bounded local-Qwen summaries,
whole-turn context boundaries and recoverable manifests without rewriting history.
Read the [context contract](../contracts/context-compaction.md) and
[Phase 8 evidence](../validation/phase-08.md). Repeated compaction and process-death
recovery pass deterministic checks; a synthetic live session preserves identity,
corrections, known facts and an unfinished commitment across two compactions.

Phase 9 adds SQLite-owned job definitions/occurrence claims, APScheduler wakes,
durable quiet-hour local delivery and bounded proactive work. The real scheduled
Qwen → bound topic → OpenViking extraction gate passed in the approved test account.
Read the [job contract](../contracts/jobs.md) and [Phase 9 evidence](../validation/phase-09.md).
Job HTTP/CLI/UI surfaces and production job migration remain later gates.

Phase 10 adds persistent operator authentication, exact Host/origin/CSRF checks,
versioned resources over shared services, bounded SSE snapshots/replay/previews,
authorized artifact chunks and opt-in live CLI health checks. A disposable live
pinned-Qwen conversation passed over authenticated HTTP. Read the
[API contract](../contracts/api.md), [event protocol](../contracts/events.md),
[CLI guide](../cli.md) and [Phase 10 evidence](../validation/phase-10.md).
Browser usability, remote TLS deployment and production cutover retain later gates.
The operator subsequently enabled direct LAN HTTP for exact private IP origins;
see V2-33 in the [decision register](../decisions.md) and the
[deployment guide](../contracts/api.md). CIDR/wildcard origins remain unsupported.

Phase 11 adds the packaged chat/Settings shell, stable Lit component rendering,
bounded backward history, durable browser retries and shared SSE/polling recovery.
Its UX follow-up adds system light/dark appearance, clearer empty/disabled states,
compact mobile controls and configured-server Qwen browser verification.
Read the [UI guide](../chat-ui.md) and [Phase 11 evidence](../validation/phase-11.md).
Operator completion of the conversation/tool/scheduled/recovery journeys remains
an explicit acceptance gate; browser automation does not stand in for it.

- One Sola, one operator, one runtime process, one event loop, one worker, one shared inference semaphore.
- Pinned local Qwen served by oMLX. No silent model substitution.
- Operator-owned SOUL, curated memory, and prompt fragments preserve identity. The harness supplies mechanics rather than personality policy.
- OpenViking supplies semantic memory; local evidence remains independently readable.
- Immutable JSONL is authoritative for conversation and execution evidence. `state.db` owns operational state. `search.db` contains only projections rebuildable from JSONL.
- Plain async Python core, adapters at external boundaries, thin FastAPI routes, explicit composition in `create_app`. No FastAPI imports below the HTTP layer; no SDK response objects in stored records.
- APScheduler wakes in-process scheduling services. SQLite owns schedules and occurrence claims. No Celery, Redis, or second scheduling authority.
- Web UI and HTTP API are the clients. Android compatibility, Discord parity, general shell access, browser/computer control, and a general multi-agent platform are outside this release unless separately selected.
- Trusted Python workspace packs extend tools through the public SDK. Installing or loading a pack does not grant authority.

Phase 12 adds bounded file listing/search, immutable write artifacts and one optional
ComfyUI workflow with durable intent, read-only recovery and authorized PNG preview/
same-topic attachments. Live Qwen file/image, compaction, scheduled revision and
test-account extraction checks passed. The configured AntiLoop model fails the
frozen exact-write reliability corpus, so the release gate remains failed;
operator browser acceptance also remains pending.
Deferred integration trees are retained without loading or activating packs.
Read the [workflow guide](../image-workflows.md) and [verification](../validation/phase-12.md).

Phase 13 provides a single direct importer from the stopped legacy runtime into
new v2 data. The operator has backups and accepts manual cleanup; snapshot,
checkpoint, verifier and rollback machinery has been removed. See the
[import guide](../migration/cutover.md) and [checks](../validation/phase-13-simple.md).

## Shared implementation conventions

Suggested boundaries are `core/` for records and orchestration, `adapters/` for model/memory/storage, `tools/` for built-in tools, `registry.py` for registration, `api/` for HTTP, and `web/` for the HTML shell, local Lit components and shared CSS. Names are proposals, not a requirement to create empty abstraction layers. `sdk.py` is the supported pack surface. Keep composition small enough to read in one sitting.

The public executable is `sola`, with exactly `serve`, `check`, `prompt`, and `search`. Phase 1 replaces the bootstrap's `solakit` entry point; prompt/search behavior remains assigned to its later phases. Evaluation, migration, index rebuild, and live probes are developer scripts, not extra public commands. The skeleton's `check --explain` reports installation status; Phase 2 adds effective configuration without revealing secrets. Offline commands must not construct the server or live clients.

The bootstrap requests Python >=3.15. Phase 0 must verify the supported interpreter against required dependencies before selecting a floor and lockfile; the legacy runtime's Python floor is not authority for v2. Dependency versions are selected and verified during implementation, not guessed in these plans.

Every slice should produce working behavior, relevant executable verification, and a short evidence report. Store reports under `docs/validation/` with commit, environment, command, result, artifacts, limitations, and outstanding decisions. Use `pending`, `passed`, `failed`, or `blocked` for gates; a mock test never passes a real-service gate. Record operator acceptance separately from deterministic checks. Do not add date estimates before the baseline and contracts establish the work.

## Decisions and unresolved references

Maintain `docs/decisions.md` from Phase 0. Each decision has an ID, status (confirmed/proposed/deferred), owner, rationale, consuming phase, and evidence. The operator owns product and deployment choices; implementers can settle reversible internal details and record them.

| ID | Initial status | Decision and deadline |
| --- | --- | --- |
| V2-01 | Confirmed | Pinned local model and OpenViking; verify installed contracts in Phases 4/7 |
| V2-02 | Confirmed | Separate evidence/state/search authorities |
| V2-03 | Confirmed | Web/API; no Android gate |
| V2-04 | Confirmed | Persistent identity, memory, bounded proactive work |
| V2-05 | Confirmed direction | Shared/isolated/ephemeral; see session contract |
| V2-06 | Deferred | No delegation in v1 |
| V2-07 | Selected | Eager schemas first; measured deferred-disclosure gate |
| V2-08 | Confirmed | Operator-enabled wakes may act; no autonomous wake creation |
| V2-09 | Confirmed direction | This Mac, localhost/tailnet; Python 3.15 smoke checked |
| V2-10 | Confirmed | Basic files and image generation; domain packs deferred |
| V2-11 | Selected | Evaluation thresholds in Phase 1; soak duration before cutover |

The full [decision register](../decisions.md) includes quiet hours, image inputs, shell deferral, and remaining verification boundaries.

The outline's D1–D6 and its unspecified numeric disclosure gates refer to unavailable source material. Do not equate them with similarly named legacy refactor issues or invent approval. Track the missing source explicitly; use this register to resolve the affected contracts. B1/S1/S3 are translated into concrete cleanup, path-protection, and request-origin tests in Phases 5, 6, and 10.

## Reference evidence

Reference checkout inspected at HEAD `fd2086bea12e247297cf0acef47544fb535e2324`. Its working tree may differ from that commit; migration rehearsals must record both commit and source-copy manifest. No live model, OpenViking, credentials, or production data were accessed for these plans.

Useful local references are the legacy [record definitions](../../../solakit/src/solakit/core/schemas.py), [pack catalog](../../../solakit/packs/README.md), [job behavior](../../../solakit/docs/operations/jobs.md), [memory notes](../../../solakit/docs/operations/memory-direction.md), and [completion evaluation](../../../solakit/docs/completion-check-evaluation.md). These links assume the sibling checkout remains available. Legacy checks and reports are reported evidence, not verified v2 results.
