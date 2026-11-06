# ComfyUI image workflow pack

`solakit-packs-image` submits named API-format ComfyUI workflows, including an
operator-installed ZImage workflow, without changes to core execution. Install
with `uv sync --frozen --all-packages`, set `SOLA_PACKS_CONFIG`, and explicitly
load `sola_packs.image`. Export the graph using ComfyUI's **API format**, with a
text prompt input, integer seed input, and optional integer width/height inputs
already present. Models, sampling steps, output nodes and other graph choices
remain in that file. The included [reference workflow](workflow.json) uses
prompt node `45`, seed node `44`, and dimension node `41`.

```toml
[images.studio]
base_url = "http://127.0.0.1:8188"
workflow = "/absolute/path/to/workflow.json"
prompt_node = "45"
prompt_input = "text"
seed_node = "44"
seed_input = "seed"
width_node = "41"
width_input = "width"
height_node = "41"
height_input = "height"
# token = { env = "COMFY_TOKEN" } # Optional bearer auth via an operator proxy.
timeout_s = 15
max_response_bytes = 1000000
```

Grant tools `image.generate`, `image.result`, resource `image:studio`, a
non-none write tier for generation, and read reach containing the graph.
Result-only profiles need only the result tool and resource alias.
The configured endpoint is operator-trusted; keep ComfyUI private and install
trusted custom nodes. HTTP redirects and automatic retries are disabled.

`image.generate(workflow, prompt, seed, operation_id, width=768, height=768)`
changes only the configured text/seed/width/height inputs and submits once.
Dimensions must be integers, multiples of 8, from 64 through 4096 pixels. For a
landscape cover, use e.g. `width=1024, height=576`. Configure both dimension nodes;
they may share a node but all mapped input slots must be distinct. Use
`size_node = "41"` as shorthand for both node mappings. Input names
default to `width` and `height`; custom names and separate nodes are supported.
For compatibility, workflows without dimension mappings keep their original
sizes when arguments use the defaults; requesting other sizes requires mappings.
The workflow file is never edited, and changed dimensions under an existing
operation ID are rejected just like changed prompts/seeds. A stable operation ID returns its original
acknowledgement on identical retry. Changed requests, torn receipts, concurrent
calls and pending/uncertain submissions fail closed. Accepted filesystem/network
operations finish before cancellation propagates. A timeout never resubmits work.

`image.result(workflow, operation_id)` reads the receipt then polls history once.
It reports pending/missing, failed or completed with output filename/subfolder/type
references. Empty history is ambiguous (queued, cleared or unknown), not proof of
failure. Images stay on the configured ComfyUI server; no local file or delivery
artifact is claimed. Result text is external data, with bounded response size and
output count. Endpoints cannot be switched under an existing receipt.

Trusted packs can compose `completed_png(ctx, workflow, operation_id, image_index)`
to retrieve one completed output as PNG bytes without submitting generation.
It revalidates the image resource grant and original receipt endpoint, accepts
only safe output references, and downloads from the configured `/view` endpoint
with registered credentials, no redirects/retries, a bounded request deadline,
a 20 MB download/output limit and a 40 megapixel decoded-image limit. The caller
owns scope-checked exclusive file creation and publication receipts. The optional
[blog pack](../blog/README.md) uses this seam for Astro cover images.

The image wheel includes the reference graph as `sola_packs/image_workflow.json`.
Copy it to an operator-owned path and point `workflow` there; it is never
automatically loaded or submitted. Adjust model names to match your installation.
