# Phase 12 — Basic tools and image workflows

Status: planned. Depends on [Phases 5–11](README.md) and [confirmed release scope](../product.md). Milestone: a daily-use candidate built around a small reliable tool set. This phase supersedes the outline's broad domain-pack port sequence.

## Outcome

Sola completes useful file and image work with durable artifacts, explicit resources, and honest receipts. Domain integrations do not obscure basic harness reliability. Letters, blog, email, journal, calendar, printer, delegation, and shell are not v1 release dependencies; their historical data remains preserved.

## Implementation slices

1. **Finish basic file workflows.** Exercise list/read/search/write against named granted roots, with useful bounded errors. Complete a task requiring several reads and one output artifact, follow up in the same topic, then repeat after compaction. Validate overwrite, missing parent, symlink, Unicode, oversized result, and denied-path behavior. Verify tool receipts and artifacts agree with the final reply.
2. **Integrate one image workflow.** Select an operator-owned existing workflow/resource, validate required input fields and allowed outputs, and expose a small model-callable interface. Record workflow hash, explicit generation parameters/seed, local request fingerprint, submission/provider ID, output references/hashes, and failure/uncertainty. Prefer durable submit and status observation over a long untracked tool wait. Do not reproduce every legacy image option or introduce browser/computer control.
3. **Make generation recoverable.** Persist intent before submission. Use provider idempotency where available; after timeout/crash with unknown acceptance, retain an uncertain receipt and reconcile by provider ID/key rather than resubmit. Treat cancellation as best effort and continue observing potentially running provider work. Bound polling, concurrency, timeouts, and artifact sizes. Distinguish submitted, completed, downloaded, and failed states.
4. **Keep the extension boundary small.** If image generation ships as a trusted Python pack, use the public SDK only and explicit loading/grants/resources/effects. Installing it enables no actions. Keep image-domain configuration and provider types out of the run loop. Add common support only for demonstrated shared needs; no domain-pack suite is required. Core-only wheel remains pack-free.
5. **Validate complete journeys.** Read a brief, search relevant local evidence, produce a text artifact, generate an image and show the output, follow up with a revision, and run a scheduled permitted file/image preparation task with traceable memory extraction. Include ordinary no-tool conversation, denial, image outage, interrupted submission, and no-action proactive wake. Score artifacts/receipts, not promises.
6. **Preserve deferred integration data.** Rehearse retention of legacy pack files, config references, receipts, unknown fields, and uncertain effects without loading deferred packs or activating credentials/jobs. Historical imports must not require their Python packages. Prepare an explicit later backlog; shell work only begins after these file/image reliability gates and a separate scope decision.

## Verification and exit gate

Deliver basic-tool acceptance report, one configured image integration/distribution, artifact handling, image recovery notes, and a deferred-data preservation report. First use fake adapters, temporary workspaces, and saved outputs; then run the selected image workflow against a named test destination. No production generation or outbound effect is inferred from package installation.

Fault-test crashes after intent, provider acceptance, output creation, and local receipt. Confirm no incorrect duplicate submission, unauthorized filesystem operation, or unconditional success claim for an uncertain effect. Validate image attachment behavior against Phase 4's model capability results.

A frozen selected-workspace install and core-only wheel/install must pass. Exit requires Phase 1/6 reliability gates on the minimal tool set, successful live image generation with retrievable artifact, scheduled workflow evidence, and operator browser acceptance. Deferred domain-pack parity is not a gate.
