# ComfyUI image workflow pack

`solakit-packs-image` submits named API-format ComfyUI workflows, including an
operator-installed ZImage workflow, without changes to core execution. Install
with `uv sync --frozen --all-packages`, set `SOLA_PACKS_CONFIG`, and explicitly
load `sola_packs.image`. Export the graph using ComfyUI's **API format**, with a
text prompt input and integer seed input already present. Models, dimensions,
sampling steps, output nodes and other graph choices remain in that file.

```toml
[images.studio]
base_url = "http://127.0.0.1:8188"
workflow = "/absolute/path/to/zimage-api.json"
prompt_node = "6"
prompt_input = "text"
seed_node = "3"
seed_input = "seed"
# token = { env = "COMFY_TOKEN" } # Optional bearer auth via an operator proxy.
timeout_s = 15
max_response_bytes = 1000000
```

Grant tools `image.generate`, `image.result`, resource `image:studio`, a
non-none write tier for generation, and read reach containing the graph.
Result-only profiles need only the result tool and resource alias.
The configured endpoint is operator-trusted; keep ComfyUI private and install
trusted custom nodes. HTTP redirects and automatic retries are disabled.

`image.generate(workflow, prompt, seed, operation_id)` changes only the configured
text/seed inputs and submits once. A stable operation ID returns its original
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
