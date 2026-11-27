# Background jobs and proactive work

Jobs share the run loop, event loop and model permit. `context.jobs` owns
application operations. APScheduler supplies a one-second in-memory wake. Startup
evaluates SQLite job definitions before starting that timer.

## Definitions and operations

`Job` definitions live in the existing `state.db` entity ledger and immutable JSONL
outbox. They retain task, scheduled/proactive origin, selected profile, tools,
workspace roots, budgets, bound session/mode, delivery policy, version, schedule
generation, next UTC instant, enabled state, consumed state and tombstones.
New definitions are disabled unless explicitly enabled. Existing per-job pauses
read as disabled, with stored evidence retained. Versions change with
definition edits; generations change only with schedule/timezone edits. Timer
advancement changes neither. A deleted ID cannot be imported/created again.

Supported schedules:

- `once:2026-12-01T09:00:00-07:00`: an offset is required; the instant is UTC internally.
- `cron:0 9 * * mon-fri`: five APScheduler cron fields (minute/hour/day/month/weekday),
  in the stored IANA timezone. APScheduler weekdays use Monday=0; names avoid ambiguity.

The application service validates profiles, model/runtime identity, session mode,
archival, IANA zones, schedules, roots and budget ceilings. A supplied tool list
must be a subset of currently granted/loaded tools. An omitted list inherits the
profile. Background file mutations require explicit job `workspace_roots` under
the profile's roots. Without them, file read grants still apply, the file mutation
tools are omitted, and guarded writes through other packs are denied. A profile
cannot broaden the invoking interactive run through job tools. Current revocations intersect frozen
admission authority again at execution/guarded access.

```python
from solakit.records import Job, Origin, new_id

job = context.jobs.create(
    Job(
        origin=Origin.PROACTIVE,
        profile="default",
        task="Review the unfinished commitment in this topic; act only if useful.",
        schedule="cron:0 9 * * mon-fri",
        session_id=topic.id,
        tools=(),
    )
)
context.jobs.update(job.id, enabled=True)
context.jobs.update(job.id, enabled=False)
context.jobs.update(job.id, task="Review the corrected commitment.")
receipt = context.jobs.manual(job.id, request_id=new_id())  # Explicit run-now.
context.jobs.delete(job.id)
context.jobs.global_pause(True)
```

`list()` returns nondeleted jobs; `store.jobs(include_deleted=True)` includes
tombstones. `store.occurrence(id)` and `store.run(id)` inspect outcomes.
`manual()` is idempotent by job ID plus caller request ID, including after edits or
deletion. Each new manual key is a distinct requested occurrence; it cannot overlap
queued/running work for the same job. Global pause blocks admission, start and
delivery. A disabled job skips automatic admission and holds queued automatic
runs. Use ordinary run cancellation to stop running work. Explicit manual
requests work with the job enabled or disabled. They respect global pause and
queue/overlap bounds and bypass automatic hourly/daily admission limits and
quiet-hour/notification cooldown restrictions.

Grant only needed tools: `jobs.list`, `jobs.create`, `jobs.update`, `jobs.pause`,
`jobs.resume`, `jobs.run_now`, `jobs.delete`, `jobs.pause_all`. Loading grants nothing.
Existing `jobs.pause` and `jobs.resume` grants toggle the same `enabled` state.
Tool operations are scoped to the invoking topic; mutations require interactive
origin and the exact operation grant. `jobs.run_now` requires a stable 32-hex
request ID. HTTP/CLI job controls and the Settings UI retain Phases 10–11 gates.

## Time, backlog and admission

Cron uses APScheduler's UTC trigger to enumerate *civil wall labels*, then resolves
them with `zoneinfo` in the job's IANA zone. Nonexistent labels are skipped; repeated
labels use `fold=0`, the first offset, exactly once. The occurrence stores the UTC
instant and full local offset. This explicit adapter avoids relying on unspecified
[native cron DST behavior](https://apscheduler.readthedocs.io/en/3.x/modules/triggers/cron.html).
Tests cover spring gaps, both fall folds, and every-minute schedules crossing a fold.

Cron downtime coalesces to the latest due slot within a one-hour grace window.
At most 61 minute slots are examined per job/tick; older backlog is represented by
`coalesced_from`, not replayed. No eligible slot, overlap, or a rate/queue cap records
one skipped range and advances beyond now. Disabling stops automatic admissions.
Enabling a cron starts at its next future slot without replaying missed slots.
An overdue unconsumed one-shot waits for queue/rate/overlap room and runs once.
Invalid profile/target admission records failure and consumes that one-shot;
repair it and explicitly set a new future schedule. No invalid job blocks other jobs.

The scheduled occurrence key hashes job ID, generation and scheduled UTC instant.
Manual keys use a separate namespace plus the caller request ID. Claim, queued run,
frozen definition/profile, input, consumption/next instant and evidence outbox share
one SQLite transaction. Only after that transaction and evidence flush does the
run service get notified. An ephemeral target creates a private one-run session.
An archived bound topic blocks admission. The terminal run and occurrence commit
archives the one-run session, including on failure, cancellation, or recovery
from interruption.

A one-shot is consumed at admission, including manual execution before its due
time. Task edits, enable/disable, restart and search rebuild cannot rearm it. An
explicit changed *future* one-shot schedule creates a new generation and clears
consumption. Changing timezone alone does not rearm a consumed one-shot.

## Bounded autonomy and publication

Both automatic scheduled and proactive work obey the operator's `[background]`
settings. Defaults are one active, eight pending, one admission/topic/hour, four
admissions/rolling hour, and 24/rolling 24 hours. Counters derive from durable
claimed occurrences, including failed/no-action runs, and survive restart. Manual
occurrences have their own trigger marker and do not consume those counters.
`[proactive_budgets]` bounds both origins, initially eight model calls, twelve tool
attempts and 600 active seconds. Definition budgets only narrow those ceilings.
Queue wait is excluded. Interactive work remains eligible in other run slots and
shares FIFO inference; a background run cannot occupy all eight default slots.

There are no autonomous successors, even if another tool constructs an interactive
profile or submits through Store directly. An inherited execution context guards
all admission and schedule mutations. Tool origin/grant checks independently deny
schedule management. Trusted Python packs remain trusted code, not an OS sandbox.

Wake context is the explicit task, bound-session context/compaction and scoped
recall. Autonomous transcript search cannot scan other topics. A final exact
`NO_ACTION` is a successful evaluated wake with an occurrence decision and private
model evidence, no delivery and no empty chat bubble. Confirmed tool work records
`work`; otherwise a nonempty final answer records `message`. Failure stays failure.
Scheduled input/tool messages remain in execution evidence; normal `messages()`
lists only published assistant output for background origins.

Quiet hours use the policy timezone, initially America/Denver, 22:00 inclusive to
08:00 exclusive. Local file preparation and exact note staging may continue.
Outbound-capable profiles wait in the durable queue until daylight. If execution
crosses into quiet hours, consequential outbound calls wait **before tool intent**,
within the existing active deadline. A long wait exhausts the run budget and is
not retried. Dispatch and `begin_effect()` check again; a pack that crosses the
boundary during its handler is denied before starting the effect. Unknown
consequential pack tools are conservatively treated as outbound. There is no
external notification provider in v1.

Successful final output creates a pending Delivery with frozen text, stable message
ID and 24-hour expiry. The visible topic message is deferred during quiet hours or
global pause. On release, only the newest pending message/topic remains; expired,
archived/deleted-target and identical already-delivered text (within 24 hours) are
cancelled. Durable delivery timestamps enforce one notification/topic/hour. Local
pending → sending/accepted → published message → delivered/completed share one
transaction, so a crash cannot append a second copy. `delivery="none"` retains
private results without publication.

A result queued as part of a one-run topic's automatic archival can publish to
that archive after quiet hours or a global pause. Expiry and job deletion still
apply. Archive reads retain both the published result and its execution evidence.

## Recovery and inspection

Store recovery flushes pending outbox entries, resumes untouched queued work after
validation, and marks active work interrupted. Its occurrence becomes uncertain;
an unresolved tool intent gets an uncertain receipt. Neither is automatically
replayed. Pending local deliveries resume safely using their stable IDs. Existing
nonlocal `sending` deliveries recover uncertain and are never blindly sent again.
Provider idempotency must be added with any future external delivery adapter;
universal exactly-once external effects are not promised.

Back up state.db and evidence together using the existing maintenance commands.
Search is rebuilt from JSONL, including job/occurrence/control evidence, and owns
no schedules or consumption state. Global job/control records are eventless ledger
entries; run/tool/message/delivery records retain per-session event sequences.
No SQL tables or state schema migration are needed. Older record fields remain
readable with defaults; an older binary will reject the new evidence kinds.

`runtime.status` exposes durable global pause and scheduler failure type. Inspect
occurrence snapshots, run records, delivery states and tool receipts before an
explicit new attempt at interrupted or uncertain work.
