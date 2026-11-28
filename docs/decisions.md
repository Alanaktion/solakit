# Sola v2 decisions

`main-v2` is the production runtime. The v1 cutover is complete. This register
records durable design intent. The linked contracts define current behavior and
defaults. Git history retains the original decisions and rollout details.

## Runtime and deployment

- Run one process, event loop, worker, and shared inference semaphore per
  configured instance. Keep application services in plain async Python, with
  FastAPI at the HTTP boundary and adapters for external services. APScheduler
  wakes the in-process scheduler and SQLite owns schedules and occurrence claims.
  [Runtime contract](contracts/runtime.md), [jobs](contracts/jobs.md)
- Target Python 3.15 on macOS with local storage. The operator uses the web UI and
  authenticated API. Bind localhost by default, allow direct HTTP for exact
  configured private LAN and Tailnet IPs, and require HTTPS and Secure cookies
  for public IPs and hostnames. Preserve exact Host and Origin checks and CSRF
  protection. The macOS LaunchAgent manages one instance per canonical config
  path. [Deployment](contracts/api.md#deployment-and-login), [service CLI](cli.md#run-sola-at-login)
- Pin local Qwen through oMLX. Keep model substitution explicit and preserve
  exact request and response evidence. The operator owns SOUL, curated memory,
  and prompt fragments. [Model and prompt contract](contracts/model-prompts.md)
- Use packaged Lit components and the authenticated API, with a restrictive CSP.
  Keep configuration and credentials operator-owned. Allow a configured display
  name and local CSS. Preserve settled messages, drafts, and submission keys
  across transport recovery. [Chat and Settings guide](chat-ui.md)

## Evidence, sessions, and memory

- Keep immutable JSONL as evidence, `state.db` as operational authority, and
  `search.db` as a rebuildable projection. Acknowledge work after durable evidence
  publication. Backup and restore evidence with state. Archives remain readable
  and searchable, and archived payloads are verified on read and full restore.
  [Storage contract](contracts/storage.md), [backup and restore](backup-restore.md)
- Freeze session mode at creation. Shared topics can use OpenViking. Isolated and
  ephemeral topics keep evidence but have neither semantic recall nor automatic
  cross-topic transcript access. Ephemeral topics have no continuing context.
  Archiving freezes a topic until restored, and pinning affects navigation.
  [Session modes](contracts/session-modes.md)
- Use stable OpenViking >=0.5.5,<0.6.0 with an explicit account, user and managed
  memory root. Keep staged notes and extraction state inspectable. Reconcile
  uncertain submissions by attaching a verified provider task. Keep curated
  memory edits with the operator. [Memory contract](contracts/memory.md), [recovery guide](memory.md)
- Compact into a bounded summary and whole recent turns while retaining original
  evidence. Keep summaries out of memory extraction. Transfer topic evidence
  and delegated descendants as independent copies with fresh local identities
  and local authority. [Compaction](contracts/context-compaction.md), [session transfer](contracts/storage.md#session-transfer)
- Use the direct importer for any remaining legacy data. Preserve raw history,
  import jobs disabled and omit legacy login sessions. Keep config conversion and
  the optional shared-history OpenViking backfill explicit. Historical effects
  and uncertain submissions must never replay automatically. [Legacy import](migration/cutover.md)

## Execution and authority

- Freeze effective grants, workspace roots, budgets and integration identity at
  admission. Recheck current authority at dispatch and guarded operations.
  Revocation applies to existing runs and additional grants require new admission.
  Built-ins register automatically, and trusted packs register explicitly through
  the public SDK. Registration grants no execution authority. [Tool contract](contracts/tools.md)
- Use eager tool schemas in production. Compare deferred disclosure using paired
  measurements and the versioned reliability thresholds. Stop on unresolved
  execution or uncertain effects. Record intent before consequential I/O and
  distinguish tool success, failure, and uncertainty in receipts.
  [Run contract](contracts/run-loop.md), [evaluation thresholds](../evals/thresholds.json)
- Resume eligible queued work after restart and interrupt previously active work.
  An explicit retry of the latest failed interactive reply links a new run to
  the original input and admission under current authority. Reuse settled
  exchanges and skip exact repeats of confirmed or successful calls.
  [Reply retry](contracts/api.md#retrying-a-failed-reply)
- Reserve wake creation and changes for authorized interactive operators. An
  autonomous run remains bound to its origin, topic and budgets and cannot create
  successors. Use durable occurrence claims, rate caps, quiet hours and coalesced
  local delivery. The job's `enabled` switch controls automatic scheduling.
  Manual runs retain global pause and overlap controls. A timed follow-up creates
  an enabled one-shot with empty tool and workspace root ceilings in the
  current shared or isolated topic. [Job contract](contracts/jobs.md)
- Allow opt-in delegation from interactive runs to one configured isolated worker
  profile. Intersect worker and parent authority, reserve an existing run slot,
  share the inference permit and charge the parent's budgets.
  An explicit continuation reuses the same child topic and original worker
  ceiling.
  [Delegation](contracts/tools.md#bounded-delegation)

## Integrations

- Keep file access inside guarded roots with separate read, discovery, and mutation
  grants. File edits require exactly one literal match. Keep image generation to
  one configured ComfyUI workflow with durable submission and recovery. Use one
  image input route per deployment, through the pinned conversation or vision model.
  Retain extracted document text in evidence for history, retries and transfer.
  [Workspace rules](contracts/tools.md#workspace-behavior-and-access-matrix),
  [image workflows](image-workflows.md), [chat guide](chat-ui.md)
- Keep publication behind explicit named tools. Blog creation defaults to drafts
  and dream creation publishes through the configured build and Git workflow.
  Named Git mutations and letters publication are interactive. Letters writes own
  their reserved checkout and preserve the legacy message format. Retain local
  commits and receipts for recovery when publication fails.
  [Blog and dreams](blog.md), [Git and letters](contracts/tools.md#configured-git-checkouts)
- Make public web search, webpage text extraction, and configured MCP calls
  opt-in under tool grants and admission-time integration settings. Allow
  literal-argument shell execution only for interactive profiles with the
  explicit grant. Commands run with the server account's privileges.
  [Tool contract](contracts/tools.md#public-web-search)
- V2-50. Keep RSS and Atom subscriptions and dedupe tombstones in evidence-backed
  state. Agents propose pending subscriptions and operators control activation.
  A destination-checked, model-free service polls active feeds. Isolated digest
  jobs stage coverage and commit it atomically with local topic delivery.
  [Feed tools](contracts/tools.md#rss-and-atom-feeds), [reader](chat-ui.md#reading-feeds)
