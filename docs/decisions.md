# Sola v2 decision register

Statuses distinguish operator-confirmed requirements, implementer-selected reversible defaults, and deferred work. Starting a phase does not approve every proposal in its plan. Evidence: final decisions in [OUTLINE](plans/OUTLINE.md), Phase 0 conversation, and [local probes](validation/phase-00.md).

| ID | Status / owner | Decision and rationale | Consuming phase / boundary |
| --- | --- | --- | --- |
| V2-01 | Confirmed / operator | Pinned local Qwen via oMLX; OpenViking for semantic memory | 4/7: verify exact installed model/service contracts before live acceptance |
| V2-02 | Confirmed / operator | Immutable JSONL evidence; operational state.db; separate disposable search.db | 2/3: define reconciliation and backup, never rebuild jobs from search |
| V2-03 | Confirmed / operator | Web/API; Android compatibility excluded | 10/11: clean v2 API |
| V2-04 | Confirmed / operator | Persistent identity/memory, background and proactive work; no deeper control integrations | All: operator-owned fragments remain authoritative |
| V2-05 | Confirmed direction / operator | Shared; isolated without OpenViking; ephemeral without continuing context but retained evidence | 2/3/7: [session contract](contracts/session-modes.md) details selected below |
| V2-06 | Deferred / operator | No delegation in v1 | 6: no child-agent executor or general framework |
| V2-07 | Selected / implementer | Eager tools first; deferred disclosure requires paired evaluation | 1/6: freeze thresholds before candidate measurements |
| V2-08 | Confirmed / operator | Operator-enabled wakes may act/leave messages; autonomous work cannot create further wakes | 5/9: enforce by origin and dispatch, not prompt instructions |
| V2-09 | Confirmed direction / operator | This Mac, localhost/tailnet; local filesystem, one process/worker/permit | 1/2/10: Python 3.15 selected after smoke probes; full compatibility still gated |
| V2-10 | Confirmed / operator | Basic files and image generation first; domain packs deferred | 5/12: minimal tool set and one configured image workflow; preserve deferred pack data |
| V2-11 | Selected / implementer | Phase 1 freezes >=90% applicable task completion and first-tool accuracy; zero silent unattempted tasks, false continuations, incorrect/duplicate effects. Deferred disclosure additionally requires no lost paired successes, median extra steps <=1, >=20% tool-heavy schema-byte reduction. Soak duration selected before Phase 13 | [Threshold snapshot](../evals/thresholds.json), selected before live measurement; these are future candidate gates, not baseline claims |
| V2-12 | Confirmed / operator | America/Denver; quiet 22:00–08:00 for proactive notifications/outbound effects; local preparation allowed; one notification/topic/hour; wakes initially disabled | 9: apply at dispatch/publication and retain counters across restart |
| V2-13 | Deferred / operator | Shell until file/image reliability gates pass | 5 excludes process execution; later scope decision defines shell grants/host privilege boundary |
| V2-14 | Confirmed direction / operator | Image attachments accepted in v1 scope | 4 verifies actual model image support; no silent fallback model |
| V2-15 | Selected / implementer | Retain full admitted inputs, model/tool records and receipt references for all session modes; hide ephemeral topics by default | 3/6: ephemeral is context lifetime, not automatic evidence deletion |
| V2-16 | Selected / implementer | Isolated/ephemeral runs cannot automatically search/read other sessions; no OpenViking calls | 5/7: memory mode cannot be bypassed through search/memory tools |
| V2-17 | Selected / implementer | No curated-memory write tool initially; operator edits curated files; scoped remember staging remains in scope | 5/7: avoid writing managed semantic-memory internals |
| V2-18 | Selected / implementer | Initial wake bounds: 1 active, 1/topic/hour, 4 globally/hour, 8 model calls, 12 tools, 10 active minutes; no autonomous descendants | 2/9: configurable, measurable, durable counters |
| V2-19 | Selected / implementer | Archive hides/freezes a topic until restore; pinned only affects navigation; compaction never archives | 2/3/8/11: no silent job rerouting to a fresh topic |
| V2-20 | Selected / implementer | Revoke legacy browser logins at cutover, preserve backup evidence; v2 logins persist normally across restart | 10/13: no old-cookie compatibility requirement |
| V2-21 | Selected / implementer, 2026-11-16 | Strict TOML with default/child profiles; child authority and budgets only narrow. Per-session event sequence/cursor; immutable terminal run outcomes. One explicit resource owner with bounded reverse cleanup; retain ownership after failed cleanup. Interactive budgets 16 model / 32 tool attempts / 900 active seconds; proactive ceilings remain 8/12/600. Reversible internal defaults, no new product scope | 2 onward: [runtime contract](contracts/runtime.md), [local verification](validation/phase-02.md); durable event/admission enforcement remains Phase 3/6 |
| V2-22 | Confirmed / operator, 2026-11-16 | Simplify access configuration to workspace roots and tool grants; remove separate resource/effect permissions. Loading still grants nothing; profiles and admitted runs cannot widen access. Session isolation, proactive-origin restrictions, budgets, and protected paths remain service invariants | 2/5/7/11/12: supersedes earlier resource/effect grant proposals; [runtime contract](contracts/runtime.md) |

## Unavailable references and remaining verification

The outline refers to D1–D6 and companion source specifications that are not present in this checkout. Similarly named legacy refactor IDs are unrelated. Their absence is recorded; these v2 contracts replace the ambiguity for this release. Newly supplied requirements should become explicit decision changes, not retroactive assumptions.

No architecture-changing product question remains unanswered for Phase 0. Remaining work has explicit boundaries:

| Item | Status | Owner / last responsible phase |
| --- | --- | --- |
| Exact Qwen pin and image-input capability | Unverified live | Implementer/operator, Phase 4 |
| OpenViking version, roots, write/idempotency contract | Unverified live | Implementer/operator, Phase 7 |
| Reproducible dependency lock and full toolchain | Passed locally | Implementer, Phase 1; locked checks and clean wheel install; hosted CI execution remains pending |
| Production data paths, volumes, live SQLite and memory-root inventory | Not collected | Operator supplies consistent source copy before Phase 13 rehearsal; Phase 3 uses synthetic fixtures |
| Exact image workflow/resource and credentials | Unselected | Operator, Phase 12; Phase 5 uses a fake adapter |
| Numerical task-completion and disclosure thresholds | Selected before live baseline | Implementer, Phase 1; [versioned thresholds](../evals/thresholds.json), revise only before future candidate measurement |
| Quiet-hour/backlog behavior and cron DST details | Product boundary set; algorithms to verify | Implementer, Phase 9 |
| Shell and deferred domain integrations | Out of release scope | Operator may initiate a separate scope change after basic reliability |

Record changes here with date, reason, affected phases, and verification. Do not widen an existing run's admission ceiling when configuration gains authority.
