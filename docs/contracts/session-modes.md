# Session, retention, and routing contract

Session mode controls continuing context and memory access across the runtime.

| Mode | Continuing context | Local evidence | OpenViking | Cross-session access |
| --- | --- | --- | --- | --- |
| Shared | Topic history, with compaction boundaries | Full durable run/message/tool evidence | Scoped recall and eligible completed-run commits | Only explicitly granted transcript search/read |
| Isolated | Own topic history | Same durable evidence | No recall, commit, staged remember, or remote memory tools | Own session only initially |
| Ephemeral | Fresh per admitted run; no later continuation | Same durable execution evidence; hidden from normal Topics | No calls | Own run only initially |

All modes receive operator-owned identity and permitted profile fragments. Isolated means isolation from remote memory and other conversations, not anonymity or deletion of identity. Curated private topic content cannot enter another topic through shared fragments accidentally; fragments are operator-selected inputs. Evidence retention is not permission to feed it back into future prompts.

## Admission and lifetime

Shared is the default interactive mode. A session's mode is fixed at creation; changing mode requires a new session, avoiding retroactive reinterpretation of memory history. Profile permissions may narrow mode access but cannot turn an isolated run into shared memory implicitly.

A shared/isolated topic admits queued turns in FIFO order, with one active context mutation. Ephemeral admission receives a distinct run/session identity and may use tools under its profile. Release in-memory queues/locks/tasks when the ephemeral run and observers finish; retained evidence does not require permanent workers. A later fresh run can reference its artifacts only through a separately authorized explicit input.

Input/model/tool content and receipts remain durable for all modes. Use bounded referenced artifacts for large outputs, with source hashes and clear truncation markers. Never erase prior evidence as part of cleanup. No automatic data-retention deletion policy is introduced in v1. Private evidence is available to the authenticated operator; model-callable search has narrower run/session authority.

## Routing, archival, and compaction

The topic ID is stable and separate from filesystem paths. A job binds an explicit topic or an ephemeral target at definition time. Proactive context comes from that target and its allowed memory scope. Replies/artifacts refer to the originating run and topic; delivery cannot duplicate the answer on refresh/reconnect.

Archival hides a topic from normal navigation and refuses new submissions until explicit restore. A job targeting an archived topic is blocked with an inspectable outcome; it does not restore the topic or invent a replacement. Pinned topics stay easy to find but gain no memory, scheduling, or permission privilege.

Job-created ephemeral topics archive automatically when the run reaches a terminal
state. A pending result recorded with that archive can still publish there.

Compaction only changes future context selection, preserving summary provenance plus original sources. It does not archive the topic, change memory mode, erase tool calls, or make a fresh session. Older messages stay paginated/searchable within the reader's authority.

## Memory eligibility and failure

Shared runs recall at the recorded start boundary. Eligible completed interactions may commit asynchronously; a successful response does not imply completed extraction. Failed/cancelled/interrupted runs and their staged notes remain evidence and are not auto-committed. An explicit reconciliation action can examine them later.

Isolated and ephemeral modes perform no remote memory work, including calls guessed by the model. Enforce scope at dispatch and adapter entry. An ordinary shared conversation may continue with a labelled recall failure unless its profile requires memory.

Verify mode boundaries with both automatic recall and explicit tools, history search attempts, job routing, archival, restart, and ephemeral resource cleanup in Phases 3/5/6/7/9.
