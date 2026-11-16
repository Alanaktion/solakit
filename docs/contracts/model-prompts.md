# Model requests and prompt evidence

Phase 4, implemented and locally/live verified on 2026-11-16. See
[verification](../validation/phase-04.md). Prompt assembly lives in
[`prompts.py`](../../src/solakit/prompts.py); the oMLX/SDK boundary is
[`model.py`](../../src/solakit/model.py). The model adapter returns owned values,
never provider response objects. There is no tool execution or conversation loop.

## Ownership and identity

`compose` supplies one application-lifetime AsyncOpenAI client after the store and
before dependent resources. Startup constructs the client without contacting the
server. Each completion takes the application's single shared inference permit,
records its request intent, checks `/models`, and sends only the configured pin.
Additional served models are allowed. A missing pin or a different returned model
fails the attempt. Returned identity is checked whenever available; missing
response identity is not invented as verified identity. `/models` proves only an
advertised API ID, not weight hashes, quantization, tokenizer, or alias provenance.
The live probe records the actual model listing and this limitation.

SDK retries are explicitly zero. There is no application retry; another attempt
requires a new caller-supplied index and fresh evidence. Connection/preflight and
total generation deadlines default to 5 and 120 seconds. The generation deadline
includes preflight, request, and response consumption, after acquiring the permit.
Response cleanup has its own connection-timeout bound, including on cancellation.
HTTP transport uses neither environment proxies nor redirects. Credentials come
from `model.api_key_env`, or `OPENAI_API_KEY` when unset; a configured but missing
credential fails startup. Keyless local endpoints receive the placeholder `local`.
Runtime does not load dotenv. The developer probe can load only the selected
credential from `.env` as literal data, taking precedence over the inherited key.

## Placement version `sola-prompt-v1`

The assembler accepts an effective profile and a frozen `PromptInput`. It reads
operator files as UTF-8 bytes without universal-newline conversion. Nothing
creates or edits these files. Their exact content, source paths and SHA-256 values
are stored in the private manifest; content hashes serve as fragment versions.
Missing configured files or invalid UTF-8 fail assembly. Optional absent fragments
produce no replacement personality or policy prose.

| Order | Request role | Source |
| --- | --- | --- |
| 1 | system | Optional `soul_file`, verbatim |
| 2 | system | Optional `curated_memory_file`, verbatim |
| 3 | system, one per fragment | Effective profile `fragments`, configured order, verbatim |
| 4 | assistant | Optional recorded context summary; requires explicit context boundary |
| 5 | original user/assistant roles | Explicit selected history, supplied order |
| 6 | user, `name=recorded_recall` | Optional frozen/recorded recall, verbatim |
| 7 | system | Separate JSON metadata: UTC time, origin, session mode, profile |
| 8 | user | Current input, verbatim, with no harness prefix or suffix |

Selected message IDs cover summary, history and input in that order. Selections
must be distinct and belong to one session. The assembler never performs retrieval,
reads a clock, silently trims context, or searches another session. Isolated and
ephemeral prompts reject supplied recall; ephemeral prompts also reject continuing
history/summary. Current history accepts text user/assistant messages; provider
tool IDs and complete tool conversation projection arrive in Phase 6.

Eager function schemas are sorted by function name, with duplicate names rejected,
and live in the request `tools` field. `eager-v1` is the disclosure view; no deferred
catalog exists. Sampling defaults are temperature 0, top-p 1, thinking enabled,
and the admitted output-token ceiling. Explicit sampling/thinking/stream choices
are frozen in `PromptInput` and recorded in the body. Every object is serialized
as sorted-key, compact UTF-8 JSON, with nonfinite numbers rejected. Identical frozen
inputs and fragment bytes produce identical body and manifest bytes.

## Request and response protocol

Before network access, private immutable artifact files hold exact serialized body
bytes and manifest bytes. An immutable `ModelRequest` links both artifacts to the
session/run/index/pin; its `model.requested` JSONL event is durable before preflight
or generation. Artifact references carry path, byte length and SHA-256. The SDK's
raw `content` option sends those bytes without JSON reserialization. The manifest
captures placement/disclosure versions, sections, fragment versions/content,
selected IDs, context boundary, frozen recall status/hash, schema hash and budget
accounting, plus the safe endpoint, timeouts and retry policy. Messages, schemas,
model pin and sampling are present in the body.

The terminal `ModelStep` links the request, records content/reasoning, finish
reason, provider response ID and usage, and points to a private response artifact.
HTTP error bodies are retained up to the response bound. Failed/partial streams
retain received bytes. A process exit can leave request intent without a terminal
step; normal store recovery interrupts its run, without fabricating a model reply.
Older Phase 2/3 ModelSteps remain readable with optional request/response/usage
fields absent. Run admission request IDs and model request record IDs are distinct.

JSON responses and SSE streams normalize to the same values. SSE framing operates
on bytes before decoding, so split UTF-8 and JSON argument fragments survive.
Tool calls assemble by index with a stable provider ID and must have contiguous
indices, unique IDs, function names and finite JSON-object arguments. They remain
inert until the full response passes validation. Permission/schema enforcement at
dispatch is Phase 5/6, independent of model output. Responses are bounded to 4 MiB;
HTTP error content is capped when recorded (the SDK reads error bodies itself).

Streams require both a finish reason and a complete `[DONE]` event. Token limits,
broken/malformed streams, malformed calls, identity mismatch, and reasoning-only
or empty stopped output are failures with no dispatchable calls. Reasoning-only
and token-limited complete responses retain normalized diagnostic fields as well
as raw evidence. Cancellation records a cancelled model step, closes the response,
releases the permit, and propagates cancellation to the caller. The caller owns the
run lifecycle and must cancel/join its work before stopping dependent resources.

The exact observed empty oMLX heartbeat (`model=keepalive`, `created=0`, empty
assistant delta and no finish reason) is ignored before identity validation. Any
extra output or different shape retains normal identity/shape checks. Reasoning
comes exclusively from `reasoning_content`, with `reasoning` as a fallback. Literal
`<think>` examples and code in content are preserved. A server that emits inline
reasoning requires a separately verified adapter change; the harness does not guess
which user-visible tags are reasoning. See the upstream
[oMLX fields](https://github.com/jundot/omlx/blob/main/omlx/api/openai_models.py) and
[SDK retry controls](https://github.com/openai/openai-python#retries); local locked
SDK behavior and the actual service were exercised for this phase.

## Private fidelity and operational output

Body, manifest and response files use the store's mode-0600 immutable publication;
artifact directories use mode 0700. They contain private inputs and outputs
unmasked. No transport headers or credentials are included. A credential typed by
the operator into a conversation remains part of that private conversation.
Back up these artifacts with state/evidence, using the Phase 3 checkpoint protocol.

Operational failures use fixed safe descriptions and HTTP status codes, never SDK
exception strings or response bodies. The adapter suppresses SDK/transport debug
and informational logging that could print bodies or headers. Explicit `sola prompt`
inspection is private content presentation, not redacted operational logging.
Redirecting its output is an intentional export of private prompt data.

## Accounting and offline inspection

No tokenizer has been verified locally. Input accounting estimates UTF-8 JSON bytes
divided by four plus 32 tokens per message. Output reserves the admitted token
limit; tool-result headroom reserves the admitted byte limit divided by four.
The request fails if their sum exceeds `model.context_tokens` (default 32768).
These are labelled estimates, not a guarantee against the server's tokenizer limit.
There is no automatic context truncation or model substitution.

```sh
uv run sola prompt 'Hello' --config config.example.toml --hashes
uv run sola prompt --config config.example.toml --frozen /private/selection.json --hashes
uv run sola prompt --request-body /private/artifacts/BODY_ID \
  --manifest /private/artifacts/MANIFEST_ID --hashes
```

Fresh inspection labels recall omitted unless `--recall-file` supplies recorded
text. `--at` freezes UTC time; `--frozen` accepts a full PromptInput JSON including
IDs/time/history/recall/schemas. Recorded inspection reads the two immutable
artifacts without resolving current configuration, contacting services, taking
runtime ownership, or creating a data directory. Body/manifest hashes are checked,
so later fragment/config edits cannot change the earlier inspected request.

The developer probe covers text, tools and reasoning in both transport modes, plus
a separate synthetic image capability experiment. Image requests retain exact
multimodal bytes and explicitly label image accounting unverified. Production
attachment admission and image-token accounting remain Phase 12; unsupported
attachments must be rejected there rather than causing model substitution.
