# Recall direction and deliberate memory notes

Memory remains opt-in, shared-scope and restricted to configured canonical roots.
These changes improve automatic recall and provide explicit note staging. They do
not change OpenViking's extraction policy or edit/delete existing memories.

## Automatic recall

For shared-memory runs, recall queries contain the last eligible user/assistant
text messages from the active transcript (oldest first), followed by the incoming
text. Tool outputs and empty text do not enter the query. The combined string is
truncated to its last `recall_query_max_chars` Unicode characters. Empty image-only
input retains the existing `Image attachment` fallback. Jobs and channel messages
use the same rule.

Configure the existing `[memory]` table:

```toml
recall_window = 3
recall_query_max_chars = 1000
recall_skip_trivial = true
recall_trivial_phrases = ["thanks", "thank you", "ok", "okay", "yes", "yeah", "lol", "nice", "cool", "got it"]
remember_max_chars = 2000
```

Skipping requires trimmed/case-folded incoming text of 1–12 characters, no `?`,
and either an exact configured phrase or an emoji-only string. Common pictographs,
flags, skin tones, ZWJ/variation sequences and keycaps are recognized. Ordinary
numbers, currency, mathematical symbols and mixed words/emoji are not skipped by
the emoji rule. The phrase list replaces the defaults, so include the phrases you
want to retain when extending it. Set `recall_skip_trivial=false` to disable the
rule. For legacy incoming-only recall, use `recall_window=0`,
`recall_query_max_chars=32000` (the incoming text limit) and
`recall_skip_trivial=false`.

Run evidence records `recall_status`, including `skipped_trivial`, `recalled`,
`recall_failed`, `ephemeral`, `disabled` or legacy `not_recorded`. A skip is also
recorded in the run journal's intermediate `memory_status`. Later commits and
extraction observations continue updating `memory_status`; they do not erase
`recall_status`. Run inspection displays both. Skipping recall does not skip the
normal post-turn commit. Disabled/ephemeral memory makes no remote memory calls;
isolated memory remains unsupported. Prompt assembly is unchanged with memory off.

## `memory.remember`

Load the `memory` built-in module and grant `memory.remember` to selected profiles.
The example configuration has commented grants for interactive and maintenance;
nothing is enabled automatically.

```json
{"text":"An exact note to carry into this run's normal memory commit."}
```

The tool requires an active run with enabled shared memory. It refuses blank notes
and notes longer than `remember_max_chars` (default 2000, configurable up to
32000). It never truncates, paraphrases or trims note text. Ephemeral child agents
cannot stage shared-memory notes.

Successful results say `staged` and include `note_id` and `run_id`. This is a local
receipt, not a storage or extraction confirmation. Notes are appended verbatim to
`runs/<run-id>.notes.jsonl` with ID, time, run/session and profile attribution.
They are private immutable evidence like transcripts and prompts; include them in
backups. `GET /api/runs/{id}` includes `memory_notes`; the run page has a **Staged
memory notes** section. Presentation still masks credentials and escapes text.
Existing tool-call arguments remain their original transcript evidence.

When the run reaches its normal commit, each staged note joins the existing batch
once as an additional assistant message with exactly:

```text
[noted for memory]
<the original note text, verbatim>
```

The marker distinguishes deliberate tool staging from something the user said.
This additional message is not appended to local conversation history. Normal
conversation evidence is still committed alongside it. Compaction summary-only
operations do not submit these notes to long-term memory. Accepted commits retain
the existing replay guard; a note is not appended again on refresh/restart.

A model failure, cancellation or crash before commit can leave a staged note
unsent. A failed or uncertain commit leaves it staged with existing memory intent
and failure evidence. Nothing drains/retries these notes automatically. Inspect
the run and reconcile uncertain remote writes before attempting further action.
Accepted and completed extraction are tracked with the existing `memory_status`,
`memory_commit`, observer and `POST /api/runs/{id}/memory/refresh` route. Even a
completed extraction task does not prove a particular fact is retrievable.

## Verified upstream surface and chosen implementation

Reviewed OpenViking tag **v0.4.17.1**, commit
[`420d4074f74070bc6fe7151aa54527cf1ff15152`](https://github.com/volcengine/OpenViking/tree/420d4074f74070bc6fe7151aa54527cf1ff15152).
The [Content API write contract](https://github.com/volcengine/OpenViking/blob/420d4074f74070bc6fe7151aa54527cf1ff15152/docs/en/api/12-content.md#write)
and [router](https://github.com/volcengine/OpenViking/blob/420d4074f74070bc6fe7151aa54527cf1ff15152/openviking/server/routers/content.py)
expose `content/write` and `content/batch-write`. These take explicit file URIs and
content and delegate to the filesystem write coordinator; they are file creation/
replacement/append, not a semantic remember operation that chooses and safely
maintains managed memory placement. Using them here would hand-author the managed
memory layout. This implementation therefore chooses staging through the existing
session/message/commit contract, without any direct managed-memory content writes.

## Operator work and validation

Search/store judgment belongs in operator-owned recall/context/profile fragments.
No draft policy text or live prompt fragments were installed by this change.
The harness does not filter automatic extraction by a storing rubric, enforce a
content policy, or implement in-place corrections/superseding of old memories.

Automated checks use mock memory/model transports. They cover query composition,
trivial skipping and overrides, scope discipline, exact staging, refusal, one
marked note in the commit batch, replay guard, extraction observation, API/UI
presentation and retention after failure/restart. Existing prompt golden tests
cover memory-disabled assembly. These checks are not live memory acceptance.

After deployment, the operator can explicitly test a distinctive fact in a live
shared session, wait for extraction to complete, then ask about it in a **fresh**
session without repeating its wording. Use `memory.search` and, when useful,
`memory.read` to verify the source. Record the actual retrieval result separately
from mock coverage. This implementation has not run that live write/read test.
