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
The optional `shell.exec` tool requires an inherited literal grant and separate
operator opt-in. See [literal command execution](#literal-command-execution).

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
Guarded writes mark the publication boundary themselves. A consequential network adapter
must call `ctx.begin_effect((credential, ...))` **before any network access**;
this rechecks authority and registers credentials with the redactor. Call
`ctx.confirm_effect()` only when the provider confirms the operation, and return
provider receipt/idempotency IDs in result data. Track multiple sends honestly:
starting another operation returns effect certainty to uncertain. Never infer a
successful send from a timeout or retry an uncertain non-idempotent send blindly.

A built-in read-only network adapter uses a private dispatcher check immediately
before I/O. The check revalidates the active invocation, named tool, current
grants and admitted integration settings. The adapter resolves and registers its
credential with the shared redactor before sending the request. Read attempts
use the normal durable intent and receipt and leave the effect at `none`.

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

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `files.rename` | `source`, `destination` | Atomically rename or move one regular file on the same filesystem. The destination must be vacant. |
| `files.copy` | `source`, `destination` | Copy up to 1 MiB of bytes (including binary) to a vacant destination as a private file. The receipt returns the byte count and SHA-256. |
| `files.mkdir` | `path` | Create one private directory under an existing parent. |
| `files.delete` | `path` | Permanently remove one regular file. |

Each operation uses its own tool grant and the usual mutation receipts. Transfer
tools check both paths, with Git metadata protection on both sides. Rename, copy,
and delete operate on single files. Scheduled and proactive mutations require
explicit job workspace roots and follow the same quiet-hours treatment as `files.write`.

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
| `files.list`, `files.read`, `files.search` | Tool grant + roots + guards | Same | Same | Same admitted narrower profile |
| `files.write`, `files.rename`, `files.copy`, `files.mkdir`, `files.delete` | Tool grant + roots + guards | Same | Same | Same with explicit job workspace roots |
| `transcripts.search` | Messages in shared sessions only | Own session only | Own run only | Bound session; own run if ephemeral |
| `web.search` | Tool grant + stable enabled Brave credential reference | Same | Same | Same admitted narrower profile |
| `git.status`, `git.diff` | Tool grant + stable checkout alias inside effective workspace roots | Same | Same | Same admitted narrower profile |
| `git.pull`, `git.publish` | Same checkout authority + interactive origin + checkout outside letters reservation | Same | Same | Denied, including manual job runs |
| `letters.read` | Named grant + configured checkout in current and admitted roots | Same | Same | Scheduled only |
| `letters.inbox` | Same authority + clean checkout update | Same | Same | Scheduled only, with explicit job roots covering the checkout and normal effect policy |
| `letters.send`, `letters.reply` | Same authority + interactive origin | Same | Same | Denied, including manual job runs |
| `shell.exec` | Enabled + literal grant + guarded starting directory | Same | Same | Denied, including manual job runs |
| `runtime.status` | Own run/service health | Own run | Own run | Own run |
| `memory.recall` | Recorded, scoped OpenViking 0.5 recall when enabled | Denied | Denied | Bound memory scope |
| `memory.remember` | Exact local staging, extraction after completed runs | Denied | Denied | Bound memory scope; no wake creation |
| Generic Git metadata writes / managed file access | Denied | Denied | Denied | Denied |
| Blog/dream publication | Named tool grant + configured checkout within roots | Same | Same; generated covers limited to own run | Same; explicit job roots and quiet-hour policy |
| Delegation / autonomous wake creation | No provided service | No provided service | No provided service | No provided service |

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

## Configured Git checkouts

Configure named checkouts with `[git.repositories.<alias>]`. Each call checks
the alias, canonical path and expected branch against the admitted and
current configuration and verifies workspace access. Older admissions
default to an empty set of checkout aliases. Git requires an ordinary checkout
with its own `.git` directory. Linked worktrees, nested repositories, aliased
metadata, and checkouts that overlap protected source, runtime data or prompt
files are denied. A letters reservation allows inspection and blocks generic
guarded writes and Git, blog and dream mutations in the checkout.

| Tool | Behavior |
| --- | --- |
| `git.status(repository)` | Reports the current branch (null when detached) and staged, unstaged and untracked filenames. Caps output at 16 KiB and indicates truncation. |
| `git.diff(repository, path=None)` | Shows tracked differences in the worktree against HEAD, optionally restricted to one filename. Caps patch output at 32 KiB and indicates truncation. |
| `git.pull(repository)` | Fetches the configured branch from `origin` and only fast-forwards a clean checkout on that branch. Returns the old and new commit IDs. |
| `git.publish(repository, paths, message)` | Requires an empty index and local HEAD equal to the freshly fetched `origin` branch. Stages and commits the explicitly named files, then pushes that commit to the configured branch. Unrelated changes stay local. |

Git uses literal repository-relative filenames. Directories, globs, Git
metadata, and symlink and hardlink aliases are denied. A tracked deletion
requires an existing parent directory. Local inspection disables optional index
updates, external diff drivers and text converters. Treat diff text as external
content.

Mutations use the dispatch effect receipt and recheck authority after acquiring
the checkout lock, which is shared with blog publications. Git uses a
noninteractive environment and the operator's trusted repository configuration
and credentials. Local hooks are disabled. Fetch and push diagnostics omit
remote URLs and raw command output. Publication requires one matching fetch
and push destination for `origin`. A failed push leaves the commit locally for
operator inspection. Failures report the last verified phase and commit when
known, and an unverified push outcome is marked uncertain. A later publication
requires HEAD to match the upstream again.

## Git-backed letters

| Tool | Input and result |
| --- | --- |
| `letters.inbox` | `after=-1` is an exclusive sequence cursor. `limit=20` accepts 1–100. Fast-forwards the checkout and returns `letters`, `next_after` and `has_more`. |
| `letters.read` | `number` is a nonnegative integer. Returns a letter from the local checkout. |
| `letters.send` | `body` starts a new letter. Returns its `number`, checkout-relative `path` and `commit`. |
| `letters.reply` | Required nonnegative `reply_to` identifies a peer letter. Accepts `body` and also returns `reply_to`. |

Read and inbox rows include `number`, `date`, `author`, `body` and
`complete`. Treat Markdown marked as external content as untrusted.
Inbox consumes numbers in order, including skipped local letters. It stops
before the first peer letter beyond the entry or byte limit. That letter stays
after the cursor. An oversized first body returns a UTF-8-safe prefix with
`complete: false` and advances the cursor. If the metadata exceeds the budget,
the call returns a budget error. Inbox counts redaction, JSON escaping and
the model-facing result framing against the smaller of 64 KiB and the effective
result budget. Local read uses the effective result budget. Callers own read
tracking.

Use one checkout with private Git metadata and a literal letter directory.
Numbered Markdown files use `NNN-YYYY-MM-DD-author.md`. Scans count at most
10,000 entries and reject duplicate numbers, invalid dates and aliased or
non-regular letter files. Publication takes the next number, dates the filename
in the admitted background timezone, and writes the exact supplied body plus
one LF. Bodies require 1–64,000 characters and at least one non-whitespace
character, within the normal JSON argument limit.

All four tools share the checkout lock with Git inspection. Inbox and
publication require the configured local branch, clean index and worktree,
and a fast-forward to the freshly fetched branch. Publication uses a single
matching fetch/push destination on `origin`, commits only the new letter,
and uses a normal push. Scheduled inbox updates follow the normal effect and
quiet-hour policy and require explicit job roots. Letters tools create no wakes.

A durable publication key reserves each reply target, and a request fingerprint
deduplicates identical sends. Confirmed exact requests return their saved
result. Unfinished work and previously used reply targets require inspection.
A failed push preserves the local file and commit and reports the last
verified phase. The operator can publish the existing commit and reconcile it
with the runtime stopped.

```sh
uv run --locked python -m scripts.letters_work --config ~/.config/sola/config.toml
uv run --locked python -m scripts.letters_work --config ~/.config/sola/config.toml --reconcile PUBLICATION_ID
```

Reconciliation checks the original local bytes, the commit parent and subject,
the single added file, and reachability from the freshly fetched branch.
The script performs verification and records evidence. Git publication and
worktree repair are separate operator actions.

For v1 cutover, create or use the v2 data directory with publication grants
withheld. Stop the runtime and run `scripts.letters_work --seed-v1` using the
production configuration. The script verifies replies in the configured
remote branch's history, then checks imported receipts under
`imports/legacy/pack-operations`. Use `--legacy-operations` to consult the
original receipt directory, `--legacy-checkout` for the original checkout path
when rehearsing on a copy, and `--profile` if the checkout is admitted through a
named profile. Review the seeded records before granting publication. A verified
reply with insufficient request evidence still reserves its target, as does an
unresolved v1 receipt. [Configuration](../config.md#letters) · [Validation](../validation/letters-tools.md)

## Public web search

`web.search(query, limit=5)` accepts a public query of 1–600 characters and at
most 75 words, with a limit from 1 to 10. It returns an ordered JSON array of
`title`, `url` and `snippet` fields. Snippets are untrusted excerpts. The model
can cite the returned URLs and should use only the public terms needed for its
query. Solakit makes one request to Brave over verified HTTPS, with a 10-second
provider deadline and a 512 KiB response-body cap before parsing. Redirects and
requests to result URLs are disabled. Titles and snippets are plain text capped
at 160 and 400 characters. HTTP(S) source URLs can be up to 2,048 characters.

An empty result array represents a successful search with no usable matches.
Missing credentials, rate limits, timeouts and provider errors have distinct
failure messages. Malformed rows are skipped and a malformed envelope fails the
call. The query and returned excerpts are stored in regular tool evidence.
Admissions without a `[web_search]` reference, including older admissions, keep
search ineligible. Changing the reference or removing the grant revokes search
from an existing run. Matching wildcard grants can confer search in new runs.

## Literal command execution

`shell.exec(argv, cwd=None)` starts one executable with literal arguments and
closed stdin. Pass one to 128 argument strings, including the executable, up to
32 KiB of encoded arguments including terminators. The launcher uses a fixed
system PATH, a UTF-8 locale and the server's temporary directory. Commands
receive no server credentials through environment inheritance. The API offers
no shell expansion, TTY, background execution, or per-call environment and
timeout settings.

The tool requires `[shell] enabled = true`, a literal profile grant and an
interactive run. Both the admitted and current profiles must authorize it.
Older admissions without shell settings and an explicit grant default to
disabled. Omitted and relative `cwd` use the first root in the admitted and
current intersection. The existing no-symlink traversal and protected-path
checks apply to the directory itself, including Git metadata. A verified
directory descriptor supplies the starting directory at launch. The guard
assumes a local namespace where the operator controls directory moves.
**The executable has the filesystem and network privileges of the server account.**
Use OS isolation to restrict those privileges.

On success, the result contains `exit_code`, `stdout`, `stderr`,
`stdout_truncated` and `stderr_truncated`. Each stream retains its first 16 KiB
of raw output, decoded as UTF-8 with replacement. The standard redactor and
tool-result limit apply. Treat command output as external content. A known
nonzero exit fails with the exit code and a short redacted diagnostic.

Durable intent precedes launch. Confirmation records the observed exit of the
command. Individual filesystem or network effects require separate verification.
The runtime terminates ordinary descendants in the owned process group and
reaps the direct child. Processes that deliberately leave that group require
OS isolation for containment. Launch failures have no confirmed effect.
Timeout or cancellation after launch leaves an uncertain receipt. Inspect the
workspace before attempting the command again. The runtime never retries it
automatically.

The additive SDK `Services.blog(action, arguments)` method is reserved for the
matching named built-in blog/dream tool. It cannot turn an unrelated granted tool
into publication authority. Internal guarded binary reads/writes support PNG
cover copies without changing generic text-tool limits. Uncertain receipts keep
safe adapter error details (including the last known publication phase/commit)
while retaining `effect_uncertain` status and error code.

## Bounded delegation

`delegate.run(task)` accepts a brief of up to 8,192 characters containing the
goal, permitted source references and expected deliverable. Set
`delegation_worker` to a profile and grant `delegate.run` on the interactive
caller profile. Grants support the usual exact names and wildcards. Worker
selection is frozen with admission, and current worker and parent grants and
roots continue to apply while the child runs.

Worker prompts use their configured profile fragments, the brief and their local
transcript. OpenViking, global SOUL, curated memory and the parent transcript
stay outside those prompts. The worker can use granted built-in file, Git,
blog, dream and letter reads, public web search, local transcript search and
runtime status. The brief's source references constrain the task. The dispatcher
enforces access through tool grants and workspace roots.

A child admission records a link in the parent run using the parent call ID as
its admission key. The result carries `session_id`, `run_id`, `status` and
`evidence_ids`. A completed child adds a redacted `report` limited to 4,000
characters and further bounded by the parent's tool-result budget. A busy result
has only `status` because there is no child. The parent receives the outcome as
tool data and can describe an incomplete attempt using the recorded status.
The full child reply is available through authenticated evidence. See the
[run contract](run-loop.md#bounded-concurrency-and-waits) for budgets and recovery.
