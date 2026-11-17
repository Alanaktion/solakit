# Semantic memory contract (Phase 7)

Phase 8 context summaries are excluded from automatic extraction, including
prepared and superseded summaries. Only original conversation and staged exact
notes enter the memory payload; see [context compaction](context-compaction.md).

OpenViking accepts stable **>=0.5.5,<0.6.0**, including four-part hotfix versions
such as `0.5.5.1`. Older releases, prereleases, local version labels, and new minor
versions require review. The verified live baseline remains **0.5.5**; accepting a
patch version is not a compatibility guarantee. Run the isolated live probe after
upgrading. The HTTP adapter uses one application-lifetime client, bounded responses, explicit timeouts,
no transport retries, and no proxy/environment discovery. Credentials are optional
environment references and registered for redaction before use. Identity and prompt
fragments remain operator-owned files.

## Scope and recall

Memory is opt-in through `[memory].enabled`. Select an account, user, and exactly
`viking://user/<user>/memories`; trailing slashes are canonicalized. Arbitrary
subdirectories, aliases, traversal, and roots belonging to another user are refused.
This restriction reflects the 0.5.5 session-extraction API: it selects a user memory
namespace, rather than accepting an arbitrary destination directory. Separate test
accounts isolate test extraction even when the user and URI spelling are the same.
Changing service identity or scope requires new admission and a restarted runtime.
Child profiles can disable memory; they cannot select a broader namespace.

At run start, automatic recall runs once. Its query is the last 1,000 characters of
eligible user/assistant conversation text followed by the incoming message. Tool
messages and reasoning are excluded. There is no trivial-message heuristic. Recall
uses `/api/v1/search/find`, explicit target roots, eight hits, and inline content.
Returned URIs must be canonical descendants of the selected root; retained source
content is capped at 32 KiB and HTTP responses at 1 MiB. A malformed/out-of-scope
response degrades the entire recall rather than injecting partial results.

A `MemoryRecall` JSONL record preserves query, account/user/roots, source URI/content,
elapsed time, and `ok`, `degraded`, or `disabled` status. The same observation is
included as external user data in every request of that run. It never becomes a
system/identity fragment. Required-memory runs fail before model inference when
recall is unavailable; optional runs may reply with degraded recall recorded.
Isolated, ephemeral, and memory-off runs make no remote calls, including guessed
memory tools. Revocation removes recalled context from subsequent model requests.

Explicit `memory.recall` uses the same adapter checks and evidence. Tools require
both loading and grants. Offline `sola prompt --request-body ... --manifest ...` uses recorded bytes;
frozen/recall-file inspection remains offline and missing recall is marked omitted.

## Notes and commit lifecycle

`memory.remember` accepts 1–2,000 characters, without trimming or truncation. It
stages a local immutable `MemoryNote` tied to its tool/run and returns **staged**
with note/run IDs. It does not edit curated files or managed OpenViking internals.
A tool call can stage only one note. Failed/cancelled/interrupted notes remain local
and are never drained automatically.

Only completed shared runs admitted with enabled memory create extraction work.
The batch contains the run's user/final assistant messages and staged notes, with
source IDs. Tool output and reasoning are excluded. The commit stores its ID,
exact payload, SHA-256, source/note IDs, scope, next check, and attempt count before
external submission. One commit per run prevents duplicate enqueue across restart.
The observer recovers the terminal-reply/enqueue crash gap. Replies never wait for
submission or extraction, and memory failure does not rewrite their terminal state.

| State | Meaning |
| --- | --- |
| pending | Local durable intent; no remote mutation has started |
| running | Submission started; acceptance is not yet durably recorded |
| accepted | Provider task ID recorded; extraction may still be running |
| completed | Provider reports completed extraction; retrieval is a separate check |
| failed | Known extraction/configuration/cap failure; provider reference retained |
| ambiguous | Submission response was lost or process stopped during submission |
| skipped | Reserved local lifecycle outcome; no automatic submission |

Preflight `/health` checks the supported version range before submission. Each commit gets provider
session `sola-<commit-id>`, automatic commits disabled, self extraction enabled,
peer/working memory disabled, and only profile/preferences/entities/events types.
Configured OpenViking templates and service-side enforcement are part of the trusted
service boundary; custom templates must preserve those types' managed user roots.
The adapter verifies the created session account/user/URI and revalidates
authority between session creation, batch addition, and commit. Acceptance requires a task ID; `/api/v1/tasks/<id>` supplies extraction state.

OpenViking 0.5.5 provides no verified idempotency guarantee for this sequence. A lost
response or interrupted submission becomes ambiguous and is **never replayed**.
Pending work survives restart; accepted work resumes polling. Preflight outages and
status checks use persisted exponential backoff (2 seconds to 300 seconds), with
12 observations before a visible failure. A default cap of 128 unfinished/ambiguous
commits refuses additional work visibly. Batches over 100 messages or 64 KiB fail
locally. Failed work is not silently retried; references remain inspectable.

## Copied configuration migration

`python -m scripts.migrate_config` translates one explicitly selected legacy
profile into a strict v2 draft with absolute paths and a separate data directory.
It retains supported file/memory/status/search tool names, narrows budgets, preserves
SOUL and existing memory/context/profile fragments, and leaves remote memory off.
Split legacy read/write roots drop the generated write grant, because one v2 root
set cannot preserve those separate ceilings. Resource/effect permissions do not
become grants. Shell, channels, Android, and deferred domain settings stay inactive.
Unsupported keys are listed, and the original bytes are preserved privately.

A proposed v2 `--candidate` produces an effective diff for roots, tools, fragments,
budgets, and memory scope. Broader authority blocks config emission until the
operator records named `--resolve` choices. Nothing is installed or overwrites an
existing output directory. Review the report and unsupported settings before using
the draft; migration is a rehearsal, not production cutover.

The [runbook](../memory.md) covers setup, outage inspection, reconciliation and probes.
The upstream contract was checked against [OpenViking v0.5.5 source](https://github.com/volcengine/OpenViking/tree/v0.5.5/openviking/server/routers)
and the running service's OpenAPI schema. See [Phase 7 evidence](../validation/phase-07.md).

The version range replaces the original exact pin after review of the
[v0.5.4.1 to v0.5.5 changes](https://github.com/volcengine/OpenViking/compare/v0.5.4.1...v0.5.5):
the session/search/task/health route files are unchanged. Upstream's
[0.5.5 upgrade notes](https://github.com/volcengine/OpenViking/releases/tag/v0.5.5)
still include behavior changes elsewhere, so new minor versions remain gated and
all account, scope, payload, and response validation stays enforced.
