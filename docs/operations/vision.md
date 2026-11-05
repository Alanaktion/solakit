# Discord vision input

Send a static PNG, JPEG or WebP attachment in an enabled Discord channel or thread
whose author/mention rules permit the message. Add a question as the caption, or
send only the image when the channel does not require a mention. Message Content
intent must be enabled in the Discord developer portal as well as the bot client.

Images use the existing `[model]` endpoint and exact pinned model ID. With vLLM,
serve a vision-capable model through its OpenAI-compatible `/v1` API and set
`model_id` to the ID returned by `/v1/models`. Backend image limits must accommodate
the intended conversation. Text-only models may reject image requests; Solakit
records the failure without dropping images, retrying as text or selecting another
model. Streaming uses the same image-bearing request format.

## What reaches the model

Channel/author/bot/mention gating happens before downloads. Accepted attachments
download from allowlisted Discord CDN attachment paths over HTTPS, without
redirects, with a 15-second timeout and byte bounds. Expiring/signed CDN URLs and
bot credentials are never stored in transcripts or forwarded to the model.

The private `media/originals/<sha256>` file retains the exact source bytes. A
separate JPEG rendition applies EXIF orientation, fits within 1024×1024 while
preserving aspect ratio, and composites transparency on white. Metadata and
content hashes link both files to the run and user message. Captions stay verbatim.
Immediately before each model POST, local JPEGs become OpenAI-compatible parts:

```json
{"role":"user","content":[
  {"type":"text","text":"What is in this picture?"},
  {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}
]}
```

Base64 exists only in the model request, not in session JSONL, run events, search
or tool logs. Subsequent tool/model steps and follow-up turns retain the image
until archival/compaction replaces that context. Missing/corrupt local image files
fail the run before a text-only request can be sent. OpenViking receives image
metadata plus conversation text/assistant observations; this does not claim that
memory extraction itself interprets image pixels.

## Bounds, viewing and backup

- At most four attachments per Discord message, 8 MB each / 16 MB combined.
- Static PNG/JPEG/WebP only; animated images and other files are explicitly refused.
- At most 16 megapixels in a source image; each model rendition is at most 2 MB.
- At most two concurrent Discord ingestion operations, and 16 images in a model
  request across current history. Normal inference concurrency also applies.
- Context accounting includes text/metadata bytes plus an 8192-byte reservation
  per image. This is a guard, not measured vision tokens; backend-reported usage
  remains authoritative. Long image histories may require compaction or archival.

Input validation/download errors return a Discord notice; image-only messages
are accepted. Invalid input is never silently omitted from an otherwise accepted
run. Keep the entire private `media/` directory with session/run evidence and
app.db in backups. Original and rendition hashes are checked when reused.

Operator chat/history shows authenticated image thumbnails. `GET /api/media/{hash}`
requires existing Cookie/Bearer authorization, checks stored bytes and returns
JPEG with `no-store`/`nosniff`. Filenames are escaped presentation data. There is
no public static-media directory or remote image embed from assistant Markdown.

## Live verification

1. Verify `/v1/models` serves the configured vision-capable model. `/models`
   availability by itself does not prove vision support.
2. Send an image with an unpredictable word/number visible **only in its pixels**,
   using an unrelated filename. Ask the bot to read it in an allowed channel.
3. Confirm the reply reads the content; ask a follow-up without reattaching.
4. Inspect the recorded run/session for `input_images`/`images` hashes, unchanged
   caption and stored final reply. Check the operator thumbnail and archived image
   references. A refusal/error is not a successful recognition result.

Automated tests exercise actual Discord event handling, bounded CDN download,
image decoding/storage, model request serialization, tool turns, streaming,
follow-up history and outbound replies with controlled gateway/CDN/model fixtures.
They decode the submitted image bytes and check its pixels. Those checks validate
transport and persistence; live Discord gateway access and real VLM recognition
require the operator's connected services.
