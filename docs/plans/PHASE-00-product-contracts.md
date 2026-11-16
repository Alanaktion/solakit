# Phase 0 — Product and compatibility contracts

Status: complete (2026-11-16); live-service and production-inventory verification explicitly assigned to later phases. Dependency: [outline and shared decisions](README.md). Unlocks Phase 1 and the contracts consumed by every later phase.

## Outcome and scope

Define the harness that lets Sola retain her identity, perform tool-assisted work, remember across sessions, and act through bounded background wake-ups. Historical evidence is preserved; old API shapes and internal architecture are not compatibility requirements. Android is excluded.

## Implementation slices

1. **Write five acceptance journeys.** Record input, observable success, allowed effects, evidence, and interruption behavior for conversation, tool-assisted work, cross-session recall, scheduled/proactive work, and restart recovery. Include an offline-memory conversation and a proactive wake that decides to do nothing. Identity continuity uses unchanged operator fragments and operator review rather than an exact-answer assertion.
2. **Define proactive authority.** Specify who may enable a wake-up, what context it sees, which profiles it can use, where it may leave a message, and when it may initiate work. Proposed first version: operator-enabled schedules only, explicit durable commitments, no spontaneous event-source integrations. Choose quiet hours, notification cooldown, maximum wake budget, and consecutive-autonomous-run limit. A wake cannot expand its grants or create an unbounded self-scheduling chain.
3. **Set session semantics.** Proposed matrix below must be resolved before storage and memory work. Keep retention separate from remote-memory behavior. Decide archival, pinned-session behavior, and routing for background replies. Do not infer session IDs from filesystem paths.
4. **Inventory reference data.** On a copied fixture, enumerate transcript files, archived segments, run/prompt/tool-schema evidence, session metadata, jobs/tombstones/cursors, pack receipts and data, credential references, and OpenViking roots. Record format, authority, volume, owner, migration method, unknown fields, and backup requirements. List pack candidates without loading credentials. Record source commit and manifest hashes.
5. **Resolve implementation boundaries.** Select supported Python and deployment filesystem after dependency installation probes. Confirm one-process ownership and localhost/tailnet binding. Decide minimal delegation (proposed: defer), image-input scope, initial pack scope, and curated-memory writes. Mark missing companion specifications and D1–D6 as unavailable rather than inventing their content.

| Mode | Local evidence | Remote recall | Remote commit |
| --- | --- | --- | --- |
| Shared | Durable | Configured shared roots | Eligible completed runs |
| Isolated, proposed | Durable | Disabled initially | Never |
| Ephemeral, proposed | Durable minimal run/tool evidence, hidden from normal topic navigation | Never | Never |

Ephemeral describes context/memory lifetime, not permission to erase execution receipts. Confirm this meaning before implementing cleanup. If isolated namespaces are needed later, add an explicit root contract rather than silently mapping them to shared memory.

## Deliverables

Create `docs/product.md`, `docs/decisions.md`, `docs/contracts/session-modes.md`, `docs/migration/inventory.md`, and a journey-to-phase acceptance matrix. Distinguish confirmed outline decisions from recommendations. For every unresolved item, record the last phase that can proceed without it.

## Verification and exit gate

Walk all five journeys through admission, prompt/model, tools, evidence, memory, and delivery. Each must have an observable result and a failure path. Verify the inventory against copied examples, including archived and malformed legacy records; leave unavailable live inventory explicitly pending. Confirm no journey requires Android, unrestricted shell, or browser control.

Exit when architecture-changing decisions have an answer or a documented boundary that blocks only dependent work. The planning task itself does not authorize production migration or outbound effects.

## Completion artifacts

- [Product contract](../product.md) and [decision register](../decisions.md), incorporating the operator's Phase 0 answers.
- [Session modes](../contracts/session-modes.md) and [five acceptance journeys with traceability](../contracts/acceptance.md).
- [Migration inventory](../migration/inventory.md), copied-source manifest, and synthetic format examples.
- [Phase 0 evidence and remaining boundaries](../validation/phase-00.md).

The proposed all-domain-pack release has been replaced by basic file access and image generation. Shell and delegation are deferred. Quiet hours and the ban on autonomous wake creation are confirmed. This phase establishes contracts; no runtime implementation or production migration is claimed.
