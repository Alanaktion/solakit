# Phase 9: External packs and complete MVP verification

Status: generic foundation and first journal pack implemented locally; remaining
packs and full live acceptance incomplete. Replaces the in-tree blog/letters plan.

Prerequisites, in order:

1. [Dotted imports, shared services and collection resources](14-tool-pack-boundary.md).
2. [Generic filesystem/Git primitives and runtime grounding](15-generic-tools.md).
3. [Journal pack](16-journal-pack.md): configured collections unlock posts, dreams,
   and poems with no special blog code in core.

Remaining packs, in order:

4. [Letters](17-letters-pack.md), with separate sequential/immutable conventions.
5. [Email](18-email-pack.md), with named mailbox resources and reply-by-ID.
6. [Printer](19-printer-and-seam-provers.md), with an explicit device capability.
7. Later seam proofs: image workflow and calendar read, independently prioritized.

Packs must live in independently installable packages, compose Python services,
and never require execution-loop branches. Finish generic web/messages/device
seams only as concrete pack needs establish their shape. No speculative provider
framework or entry-point discovery is required.

Complete the profile capability matrix, operator documentation and all eleven
§14 acceptance checks, plus storage recovery, cancellation, auth and shutdown.
Mock checks do not satisfy live gates; keep Discord/jobs disabled until the live
scratch-memory check and full acceptance pass. Publishing grants remain separate
from filesystem writes. Existing Hermes installation/schedules stay untouched.
