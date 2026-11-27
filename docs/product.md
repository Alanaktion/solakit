# Sola product scope

Sola is a local agent for one operator. It maintains identity and memory across
conversations, completes permitted tool work, and runs operator-enabled scheduled
and proactive tasks. The operator owns SOUL, curated memory, and prompt fragments.
The runtime records what happened so the operator can inspect failures and
uncertain outcomes. [Design decisions](decisions.md)

## Capabilities

- Chat and authenticated HTTP access with stable topics, streamed progress,
  cancellation, reply retries, searchable history, and inspectable requests
  and tool receipts. [Chat and Settings](chat-ui.md), [API](contracts/api.md)
- Shared topics with optional OpenViking memory, isolated topics without memory
  access, and ephemeral runs without continuing context. Each mode retains
  execution evidence. [Session modes](contracts/session-modes.md)
- Guarded workspace tools, one configured ComfyUI workflow, image and document
  attachments, and optional integrations such as web search, Git, letters, and
  blog publishing. Trusted packs can extend tools through the SDK. Interactive
  profiles may opt into literal command execution or one isolated delegated
  worker. [Tools and authority](contracts/tools.md), [file and image workflows](image-workflows.md)
- Durable schedules and proactive wake-ups that use the same run loop, grants,
  budgets, and evidence as interactive work. [Job contract](contracts/jobs.md)

## Proactive work

The operator enables wakes and approves commitments. A wake can inspect its
task, bound topic and scoped recall, do permitted work, deliver a message in the
bound topic, or record a private no-action outcome. Autonomous work cannot
create, rearm, enable, or reschedule a wake, directly or through another tool.
The runtime enforces this using the original admission lineage.

Quiet hours defer local notifications and external effects while allowing
permitted local preparation. Deferred messages coalesce and expire. Occurrence
claims, rate limits and cooldowns survive restart. An explicit run-now can
operate with the job's schedule disabled and still respects global pause,
overlap rules, grants and budgets. [Background defaults](config.md#background)

## Durability and deployment

Immutable JSONL owns conversation and execution evidence. Back up the evidence
with `state.db`, which owns operational coordination. Search is rebuilt from
evidence. Compaction preserves source records, and archiving keeps history readable
and searchable. The runtime records intent before consequential work and stops on
uncertain effects without replaying them. Accepted work survives client disconnects,
and memory extraction can continue after a reply completes.
[Storage](contracts/storage.md), [backup and restore](backup-restore.md)

The production runtime targets Python 3.15 on macOS arm64 with local storage. Each
configured instance uses one process and a shared inference permit. Localhost is
the default bind, and configured LAN and Tailnet access still requires operator
authentication. [Deployment](contracts/api.md#deployment-and-login)

Android and Discord clients, general browser or computer control, and
independent agents are outside the current product scope. See the
[follow-up plans](plans/README.md) for proposed additions and the
[regression scenarios](contracts/acceptance.md) for workflows to verify.
