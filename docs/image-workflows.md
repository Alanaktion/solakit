# Basic file and image workflows

Phase 12 supplies built-in file tools and one optional ComfyUI service. It adds no
domain pack dependency. Trusted external packs still use the public SDK and explicit
loading/grants. The core wheel contains no external pack; the existing SDK example
is independently installable or selected with `uv sync --frozen --all-packages`.
Installation and image-service enablement grant no tool authority.

## Configure and use

Add `files.list`, `files.read`, `files.search`, `files.write`, and `images.generate`
to top-level `loaded_tools` and the selected profile's `tools`. Grant only intended
workspace roots. Configure `[images] enabled = true` and its `base_url` outside
the tool arguments. Give generation enough tool/run time; the default 60-second
tool deadline can expire before a cold image model finishes. Configuration examples
and ranges are in the [reference](config.md) and [example](../config.example.toml).

`files.list(path=".", limit=100)` lists one directory; `files.search(query, path=".",
limit=20)` finds literal, case-sensitive UTF-8 lines recursively. Limits are 1–100.
Listings examine at most 2,000 directory entries. Search examines at most 2,000
entries and 16 MiB, with the existing 1 MiB/file read limit and 1,000-character
line excerpts. Both report truncation; search also reports scanned/skipped counts.
Protected paths, symlinks, hardlink aliases and non-text files are skipped during
search. Direct denied paths still fail. Operations recheck current authority.
These bounded local scans remain synchronous.

Writes require an existing parent and explicit `overwrite=true` for replacement.
They retain the guarded atomic-write behavior and an immutable text artifact on
the receipt. Artifacts apply credential redaction; the write result's byte count
and hash identify the workspace content. Generic tools cannot read/write runtime
data, identity fragments, core/pack code or protected Git metadata.

The only image tool is `images.generate`:

| Argument | Behavior |
| --- | --- |
| `prompt` | Required nonblank text, at most 16,000 characters; node `45.inputs.text`. |
| `width`, `height` | Optional strict integers, default 768 each; 64–2048 in multiples of 16; node `41`. |
| `seed` | Optional strict integer, 0–2⁶⁴−1; node `44.inputs.seed`. Omission chooses one random integer in [2⁵², 2⁵³), large while exactly representable in browsers. |

The packaged workflow is byte-identical to [examples/image-workflow.json](../examples/image-workflow.json).
All other inputs remain unchanged, including batch size 1, sampler, models and
filename prefix. There are no model-callable workflow, submit/status/cancel,
sampler, model or batch controls. The result identifies the durable image record,
resolved parameters, workflow/request hashes, provider ID, output reference and PNG
artifact with length/SHA-256. Receipt success requires a downloaded, verified image.
Artifact storage uses the existing private managed tree, scoped to its run.

## Recovery and cancellation

Before POST `/prompt`, the store durably records the resolved seed, exact request
artifact/fingerprint, workflow hash, local client ID and requested UUID. Current
ComfyUI accepts a client prompt ID; older servers may return their own ID. Its
acknowledged ID is persisted. The service uses bounded GET `/history/{id}` and
`/view` requests, verifies request/client lineage, and retrieves exactly one node
51 output. Redirects, unsafe output paths, malformed/oversized responses, and
invalid PNGs fail verification. PNGs are limited to 16 MiB, 2048×2048, static
non-interlaced 8-bit RGB/RGBA, with checked chunks, checksums and bounded decoded data.
API behavior follows the upstream [ComfyUI server](https://github.com/Comfy-Org/ComfyUI/blob/master/server.py).

Durable image states are `intent`, `submitted`, `completed`, `downloaded`, `failed`,
and `uncertain`. Updates append immutable `image.changed` evidence while state.db
holds the latest snapshot and observation count. Restart never submits an intent.
Lost acceptance is uncertain; observation can find the recorded requested UUID.
A missing history entry is not evidence of rejection. An unresolved image blocks
another generation, including a model's fresh call with the same prompt.

Cancellation/timeout stop the tool's wait and preserve an uncertain receipt. The
owned observer continues read-only checks and can retain a late output after the
run ends or its grants are revoked. It never writes into workspace paths or
submits another workflow. No global provider interruption is attempted: that could
cancel someone else's work on older servers. Cancellation therefore does not
guarantee that GPU work stops. Shutdown cancels/joins the observer; checks resume
within the persisted cap on a later enabled start. Late completion does not rewrite
the original uncertain tool receipt or claim that its failed run completed.

Stop the runtime before the developer maintenance command:

```sh
uv run --locked python scripts/image_work.py --config /path/to/config.toml
uv run --locked python scripts/image_work.py --config /path/to/config.toml \
  --image-id LOCAL_ID --submission-id VERIFIED_COMFY_UUID
```

Reconciliation checks the original endpoint, graph and client ID against provider
history, resets observation bounds explicitly, and only retrieves existing work.
It never POSTs. If an older provider lost its returned ID, inspect its history to
identify the matching client ID; absence alone never authorizes replay. Keep its
history/output files until download/reconciliation finishes.

Automatic image jobs require explicit narrowed `workspace_roots`, like file writes,
and retain origin/grant/run limits. Image submissions respect quiet hours as provider
effects; local file preparation can continue. No autonomous operation can create
another wake. A late managed artifact remains inspectable without publishing an
unsolicited message.

## Images in chat

Image records appear beside tool receipts. View generated image fetches authenticated,
hash-verified artifact chunks and assembles at most 16 MiB into a PNG blob; Download
image saves the complete PNG. Raw model Markdown cannot load remote images or HTML.
Only these authorized PNG blobs are added to the existing image CSP.

Use image in next message attaches a reference to the draft; it survives topic
navigation/reload and the existing admission retry queue. Set `model.image_inputs=true`
only for a verified pin. `POST /api/v1/runs` accepts optional `image` with `run_id`
and `artifact_id`; it must identify an authorized PNG in that same topic. Unknown,
cross-topic and unsupported inputs fail before admission. Arbitrary image uploads
and external image URLs are not provided by this interface.

Admitted messages keep the immutable image reference. The prompt assembler checks
its bytes/hash and embeds a PNG data URL in exact request evidence, including on
history projection. Repeated references to the same hash use one image per request.
Compaction retains whole-turn semantics and original artifacts. Image requests
reserve 16,384 estimated tokens per unique image, separately from base64 bytes;
this is explicitly unverified accounting, not tokenizer calibration. A small context
ceiling or several different images can produce an explicit context-limit failure.
No silent trimming or substitute model is used.

## Deferred data and later work

`scripts/storage.py retain-tree --data-dir DATA COPIED_TREE` retains an explicitly
copied legacy tree under private `imports/`, with byte counts/hashes and a completion
manifest. Repetition verifies existing copies. Source symlinks/special files and
runtime-data recursion are rejected; changed copies fail rather than overwrite.
Unknown JSON/TOML fields, binary/Unicode files, credential *references*, legacy
receipts and uncertain effects remain uninterpreted bytes. No pack code, config,
credentials or job definitions are activated, and their Python packages are not
needed. Existing backup/restore includes the retained tree. Production preservation
still requires the Phase 13 consistent source inventory; synthetic rehearsal is
not evidence of production completeness.

Letters, blog, mail, journal, calendar and printer remain a later backlog: inventory
their resources and receipt formats, define narrower SDK tools, review authority,
then validate each integration independently. General shell and delegation require
a separate product decision after the basic reliability gates. They are not release
dependencies.

Reproduction and measured gates: [Phase 12 validation](validation/phase-12.md).
