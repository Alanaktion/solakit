# Phase 12 — Basic tools and image workflows

Status: planned. Depends on [Phases 5–11](README.md) and [confirmed release scope](../product.md). Milestone: a daily-use candidate built around a small reliable tool set. This phase supersedes the outline's broad domain-pack port sequence.

## Outcome

Sola completes useful file and image work with durable artifacts, explicit resources, and honest receipts. Domain integrations do not obscure basic harness reliability. Letters, blog, email, journal, calendar, printer, delegation, and shell are not v1 release dependencies; their historical data remains preserved.

## Minimal image tool

Expose exactly one image-generation tool that creates one image through the ComfyUI API using [examples/image-workflow.json](../../examples/image-workflow.json). Its only parameters are:

- `prompt`: required text; sets node `45`'s `text` input.
- `width` and `height`: optional integers; set node `41`'s corresponding inputs, each defaulting to the workflow's 768 pixels when omitted.
- `seed`: optional integer; sets node `44`'s `seed` input. When omitted, choose a random large integer within ComfyUI's supported seed range once per generation and record the resolved value before submission.

Keep all other workflow inputs unchanged, including batch size 1. Validate inputs before submission and return the generated image artifact reference with the resolved parameters/seed and receipt. Workflow selection, model/sampler controls, batch generation, and separate submit/status/cancel tools are outside this phase's scope. Submission, bounded waiting, output retrieval, and recovery are internal to this one tool.

## Implementation slices

1. **Finish basic file workflows.** Exercise list/read/search/write against named granted roots, with useful bounded errors. Complete a task requiring several reads and one output artifact, follow up in the same topic, then repeat after compaction. Validate overwrite, missing parent, symlink, Unicode, oversized result, and denied-path behavior. Verify tool receipts and artifacts agree with the final reply.
2. **Integrate the minimal ComfyUI tool.** Use the workflow and four-parameter interface above; configure the ComfyUI endpoint outside the tool arguments. Record workflow hash, resolved generation parameters/seed, local request fingerprint, submission ID, output references/hashes, and failure/uncertainty. Keep API submission, status observation, and download inside the adapter. Do not reproduce legacy image options or introduce browser/computer control.
3. **Make generation recoverable.** Persist intent and the resolved seed before submission. After timeout/crash with unknown acceptance, retain an uncertain receipt; reconcile by the recorded submission ID when available, and do not blindly resubmit. Treat cancellation as best effort and continue observing potentially running ComfyUI work. Bound internal polling, concurrency, timeouts, and artifact sizes. Distinguish submitted, completed, downloaded, and failed states in receipts without adding model-callable tools.
4. **Keep the extension boundary small.** If image generation ships as a trusted Python pack, use the public SDK only and explicit tool loading/grants and workspace roots. Installing it enables no actions. Keep image-domain configuration and provider types out of the run loop. Add common support only for demonstrated shared needs; no domain-pack suite is required. Core-only wheel remains pack-free.
5. **Validate complete journeys.** Read a brief, search relevant local evidence, produce a text artifact, generate an image and show the output, follow up with a revision, and run a scheduled permitted file/image preparation task with traceable memory extraction. Include ordinary no-tool conversation, denial, image outage, interrupted submission, and no-action proactive wake. Score artifacts/receipts, not promises.
6. **Preserve deferred integration data.** Rehearse retention of legacy pack files, config references, receipts, unknown fields, and uncertain effects without loading deferred packs or activating credentials/jobs. Historical imports must not require their Python packages. Prepare an explicit later backlog; shell work only begins after these file/image reliability gates and a separate scope decision.

## Verification and exit gate

Deliver basic-tool acceptance report, one configured ComfyUI image tool, artifact handling, image recovery notes, and a deferred-data preservation report. First use fake adapters, temporary workspaces, and saved outputs; then run `examples/image-workflow.json` against a named test ComfyUI endpoint. Verify the tool exposes only required `prompt` and optional `width`, `height`, and `seed`; prompt-only calls use 768×768 and a recorded random large seed, while explicit dimensions/seed reach the correct workflow inputs and all other inputs remain unchanged. No production generation or outbound effect is inferred from package installation.

Fault-test crashes after intent, provider acceptance, output creation, and local receipt. Confirm no incorrect duplicate submission, unauthorized filesystem operation, or unconditional success claim for an uncertain effect. Validate image attachment behavior against Phase 4's model capability results.

A frozen selected-workspace install and core-only wheel/install must pass. Exit requires Phase 1/6 reliability gates on the minimal tool set, successful live image generation with retrievable artifact, scheduled workflow evidence, and operator browser acceptance. Deferred domain-pack parity is not a gate.
