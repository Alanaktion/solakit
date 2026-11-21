# Tool SDK, dispatch, and workspace access

Phase 5 SDK/dispatch implementation, extended by [Phase 12 file/image workflows](../image-workflows.md).
See [validation](../validation/phase-05.md) and the
[external example pack](../../examples/phase5_pack/src/sola_example/__init__.py).
The supported extension surface is `solakit.sdk`. It imports neither FastAPI nor
provider SDKs. No general shell, process, or delegation service is provided.
Built-in job management retains its interactive-origin restriction. The optional
[blog service](../blog.md) runs fixed build/Git commands behind named tool grants;
it exposes no command or argument execution tool.

## Pack authoring

Install trusted Python packages in the runtime environment; list importable
modules explicitly in top-level `tool_modules`. Each module exports integer
`SDK_VERSION = 1` and a tuple/list `TOOLS` of explicit `Tool` declarations. Version
1 accepts exactly major 1; any future incompatible change increments the major.
No discovery, entry-point scanning, or implicit workspace imports occur. Import
errors name the configured module without echoing dependency exception text.
Duplicate names, unsupported versions, synchronous handlers, non-object argument
schemas, permissive extra fields, or disabled strict validation fail registration.

```python
from solakit.sdk import Tool, ToolArguments, ToolContext, ToolResult


SDK_VERSION = 1


class ReadArguments(ToolArguments):
    path: str


async def read(ctx: ToolContext, args: ReadArguments) -> ToolResult:
    return ToolResult(data=ctx.files.read_text(args.path))


TOOLS = (Tool("notes.read", "Read workspace text.", ReadArguments, read),)
```

`ToolArguments` supplies strict Pydantic object validation, forbidden extra fields,
nonfinite-number rejection, and generated JSON Schema. A handler receives its
validated argument object and returns `ToolResult(data=<JSON value>)`. No string to
boolean/integer coercion or guessed recipients, paths, or amounts occurs. Use
`ToolError(message, code)` for recoverable, safe errors; `ErrorCode` is available
from the SDK. Unhandled exceptions produce a generic failure without their raw
text. Handlers must be cooperative async functions, propagate cancellation, avoid
blocking/network work at import, and leave no detached tasks.

`Tool` declares name, description, argument class, async handler, optional
`consequential=True`, and optional immutable `origins`. Defaults permit all known
origins. This is tool metadata, not an additional operator effect grant. Guarded
writes and external effect markers require a consequential declaration. A context
contains fixed run/session/origin/mode, guarded files, scoped services, and effect
markers. It expires when its call finishes, including after failure/cancellation;
a retained old context cannot act under a later call's authority.

Trusted Python packs run as the operator. They can import `os`, open sockets, or
bypass services directly: **this SDK is not an OS sandbox against malicious pack
code**. Install only trusted packages. Operator grants constrain the dispatcher
and supported services; importing a module itself executes trusted Python.

## Loading and authority

Built-ins are always registered. Top-level `tool_modules` imports installed trusted
packs and registers their declared tools automatically. Configuration resolution
validates these registrations; unavailable modules and invalid declarations fail
resolution. Registration grants no execution authority.

Profile `tools` accepts exact names, `*`, and `?`. Patterns expand case-sensitively
against registered built-in and external tool names at resolution, including for child
profiles. Exact unregistered grants remain inspectable but non-executable. Children
must narrow the expanded inherited set; an empty list clears it. Expanding a new
wildcard match later cannot widen an existing admission.

Executable authority is registered tools intersected with profile grants,
admitted run/job restrictions, current revocations, and the declaration's allowed
origins. There are no separate resource/effect grant lists. Session mode, memory
disabling, budgets, protected paths, and run state remain service checks. A
narrower job profile uses the same evaluator; scheduled/proactive admission and
wake-management rules retain Phase 9's occurrence/service gate. No wake-management
tool is present for the model to invoke here.

The obsolete top-level `loaded_tools` key is rejected; remove it from existing
TOML configuration. Existing immutable admissions containing that field remain
readable and retain their already-frozen effective `tools` ceiling.

Tools have three useful states: hidden schemas, visible schemas, and executable
authority. `ToolSession.schemas()` eagerly lists eligible tools in sorted order.
Its optional deferred set hides schemas without changing permission.
`Registry.disclose(names, executable)` returns only schemas within that ceiling;
it cannot grant anything. Deferred behavior is an internal contract test, not an
enabled runtime mode before Phase 6's paired evaluation. Guessed hidden/excluded
names still undergo dispatch checks.

## Application entry point and evidence

Runtime composition owns a `Dispatcher` at `context.tools`, sharing the store and
credential redactor. It starts no worker. The caller must:

1. Admit through `Store.admit(..., admission=profile.model_dump(mode="json"))`.
   This saves the exact effective ceiling in immutable admission evidence. Runs
   without that snapshot remain readable but cannot dispatch tools.
2. Transition the run to running and append its `ModelStep`.
3. Bind `context.tools.bind(run.id)` and pass complete `ToolCall` records to
   `await session.dispatch(call)`. Use `session.schemas()` for prompt input.

The dispatcher reads the ceiling from verified evidence, checks run/session/mode/
origin lineage, and serializes calls within that bound session. Production callers
must own one session per run; the Phase 6 worker owns that lifetime. The store
rejects reuse of internal call IDs or provider IDs within the same model step.
Fresh attempts, including denials, count durably against the run's tool budget;
rebinding cannot reset it. No automatic retries occur.

`compose(..., current_profile=resolver)` accepts a resolver receiving the admitted
profile. `serve` and the default app resolve current configuration for that
profile name, original origin, and original mode on every guarded access. A
programmatic composition without a resolver uses `context.profile`; its caller
must replace that snapshot for revocations. Changes to runtime/model identity
require new admission. Current tool/root/budget intersections can only narrow the
saved ceiling; a new grant requires a new run.

Before a consequential operation, durable `tool.intent` contains internal/provider
call IDs, run/session/model-step lineage, original origin, explicit tool name and
arguments (including target/content/overwrite). Store failure stops execution.
Guarded writes mark the publication boundary themselves. A trusted network adapter
must call `ctx.begin_effect((credential, ...))` **before any network access**;
this rechecks authority and registers credentials with the redactor. Call
`ctx.confirm_effect()` only when the provider confirms the operation, and return
provider receipt/idempotency IDs in result data. Track multiple sends honestly:
starting another operation returns effect certainty to uncertain. Never infer a
successful send from a timeout or retry an uncertain non-idempotent send blindly.

Every completed attempt writes one durable `tool.receipt`, with status
`succeeded/failed/denied/cancelled/uncertain`, effect `none/confirmed/uncertain`,
result, error code/message, and any output artifact/hash. Cancellation records
its receipt then re-raises `CancelledError`. Timeout/failure after an unconfirmed
send is uncertain; timeout after a confirmed file write remains failed with a
confirmed effect. Receipt-persistence errors propagate without claiming success.
Restart marks unresolved intents uncertain without re-executing their tools,
conservatively including reads when the old declaration is unavailable.

## Bounds and errors

Arguments are capped at 64 KiB of canonical JSON before validation/execution.
Attempt count, active elapsed time, timeout, and output ceilings intersect
admission with current budgets. Elapsed time starts at run start, excluding queue
wait; rebinding conservatively includes wall time since that persisted start.
Timeout/cancellation use asyncio and require cooperative async handlers. Local
bounded file I/O is synchronous; this is not a hard real-time preemption guarantee
for blocked OS I/O, large workspace scans, or trusted Python ignoring cancellation.

Successful model-facing `receipt.result` is text: strings pass through, other JSON
values use canonical JSON. Its UTF-8 content respects `tool_result_bytes` and ends
with `[truncated]` on truncation (`!` for budgets below 11 bytes). Receipt metadata
is separate from that content budget. Errors use structured failure fields;
Phase 6 must bound any combined model-facing rendering too.

Oversized results retain at most 1 MiB of redacted UTF-8 in an immutable artifact,
with an explicit `[artifact truncated]` suffix if necessary. `result_bytes` and
`result_sha256` describe the complete redacted source; `artifact.length/sha256`
describe retained bytes. The schema additions default safely for old receipts.
Credential redaction applies recursively to result strings/keys before JSON
encoding, artifacts, and recoverable errors. The model adapter registers its
credential with the same redactor before creating its network client. Private
input/intent evidence is not a content scrubber; never pass credentials as model
tool arguments or emit them from pack logging.

## Workspace behavior and access matrix

Roots are canonical existing directories. Relative paths use the first currently
permitted root; absolute paths must be within a permitted root. `..`, NUL, symlink
components (including aliases within the same root), special files, and files
with multiple hard links are rejected. No parent directory is created implicitly.
A new leaf is allowed only under an existing verified parent.

On macOS/Linux, traversal opens each directory from `/` with `O_DIRECTORY` and
`O_NOFOLLOW`; leaf reads use no-follow/nonblocking descriptors and regular-file
checks. Writes stage private 0600 data in the verified parent, sync, and atomically
link a new name or replace an explicitly allowed existing name, then sync the
parent. Replacement replaces the leaf rather than following a raced symlink.
Writes default to no-overwrite; overwrite must be `true`. Existing file modes
are not preserved when replacing with a new private inode. Both read and write
are limited to 1 MiB per file.

Writes deny every case variant of a `.git` component, symlink aliases, and
metadata directories referenced by worktree `.git` files or `commondir`, including
nested worktrees, ancestor repositories, and bare repository directories. Discovery rescans granted trees;
case-alias protection also compares existing protected inodes during traversal.
Runtime data, the source checkout (or installed core package), registered pack
code directories, and configured identity/prompt files are protected from generic
file tools. Put pack code outside output directories. Operator TOML outside these
managed paths should remain outside granted roots. This boundary assumes the
operator's local namespace; it cannot sandbox hostile operator code moving whole
opened directory trees or concurrently redefining Git metadata.

| Access | Shared interactive | Isolated | Ephemeral | Scheduled/proactive |
| --- | --- | --- | --- | --- |
| `files.read` / `files.write` | Tool grant + roots + guards | Same | Same | Same admitted narrower profile |
| `transcripts.search` | Messages in shared sessions only | Own session only | Own run only | Bound session; own run if ephemeral |
| `runtime.status` | Own run/service health | Own run | Own run | Own run |
| `memory.recall` | Recorded, scoped OpenViking 0.5 recall when enabled | Denied | Denied | Bound memory scope |
| `memory.remember` | Exact local staging, extraction after completed runs | Denied | Denied | Bound memory scope; no wake creation |
| Generic Git metadata writes / managed file access | Denied | Denied | Denied | Denied |
| Blog/dream publication | Named tool grant + configured checkout within roots | Same | Same; generated covers limited to own run | Same; explicit job roots and quiet-hour policy |
| Shell / process / delegation / wake creation | No provided service | No provided service | No provided service | No provided service |

Built-in tools call these application services directly, never HTTP routes.
`transcripts.search` uses the independent evidence-verified search projection,
filters to messages before applying the result limit, and exposes reference/hash
provenance. An unavailable/corrupt projection produces an inspectable failure.
The fake adapter in the external example writes SVG via the same guard; it does
not exercise a real image endpoint. Phase 12 supplies one optional built-in ComfyUI
service, bounded file listing/search and immutable write artifacts; see the
[workflow and recovery contract](../image-workflows.md). Image artifacts use the
same authenticated run linkage, with a separate 16 MiB PNG limit. SDK file writes
retain at most the existing 1 MiB text bound. Installing/selecting the example pack
does not load or grant it.

Phase 7 memory lifecycle, limits and recovery are defined in the [memory contract](memory.md).

The additive SDK `Services.blog(action, arguments)` method is reserved for the
matching named built-in blog/dream tool. It cannot turn an unrelated granted tool
into publication authority. Internal guarded binary reads/writes support PNG
cover copies without changing generic text-tool limits. Uncertain receipts keep
safe adapter error details (including the last known publication phase/commit)
while retaining `effect_uncertain` status and error code.
