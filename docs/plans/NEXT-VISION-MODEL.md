# Separate vision model for image recognition

Let the operator attach a photo or screenshot and ask about it while the
conversation model uses text only. A separately pinned vision model reads the
pixels and returns observations for the conversation model. Keep the native
image path available for deployments described in the
[image workflow guide](../image-workflows.md).

## Chat and API

- Add an attachment picker for one static PNG or JPEG. Keep the thumbnail,
  question and remove action together in the draft. Show image-analysis progress
  and a distinct failure if the vision model can't process the attachment.
- Add `POST /api/v1/sessions/{id}/image-uploads` for one authenticated binary
  image. Return a topic-scoped `upload_id`. Accept that ID as an alternative to
  the existing `{run_id, artifact_id}` reference in `POST /api/v1/runs`. Both
  forms use the normal admission key and cancellation path. The existing
  generated-image action can attach a same-topic artifact for recognition.
- Keep visual observations available as text in later turns. To ask a question
  that requires looking at the pixels again, attach the same image from the
  conversation. Link the observation to its image and the run that produced it.

## Image and model boundaries

- Add optional `[vision]` settings for the exact model pin, endpoint, credential
  reference, and timeouts. Use `[model].image_inputs = false` for this route and
  reject configuration that enables both routes. Freeze the selected route and
  model identity at admission. An unavailable vision service fails the image
  turn with a specific error. The text-only route remains eligible for a new
  turn with no image.
- Apply a route-specific upload limit, initially 8 MiB, while retaining the
  current JSON body caps. Enforce the limit on actual received bytes after auth
  and CSRF checks. Check format and pixel bounds before decoding, reject animated
  or corrupt input, and normalize to a bounded PNG with orientation applied and
  metadata stripped. Reuse the generated-image validation and artifact read
  boundaries. Stage a small, expiring per-topic upload set. Adopt the normalized
  image into run evidence on admission, with hash and length. Resolve duplicate
  admission keys before checking the temporary upload's expiry, using the
  original attachment identity. Clean up expired, unclaimed uploads during
  upload or startup with a fixed storage cap.
- Add a small app-owned adapter using the existing OpenAI-compatible client
  setup and shared inference semaphore. Spend one model call on vision for each
  newly attached image and include its time in the ordinary run budget. Send
  the verified image and the current user's question, with a short fixed
  instruction to describe relevant visible details and uncertainty. Return a
  bounded text observation. Keep parent history, secret values, tools, and
  workspace contents out of the vision request.
- Persist the pinned vision request and response evidence separately from the
  conversation-model evidence. Project the observation as untrusted supporting
  text, outside the system instructions. The text-model request contains text
  parts only, including on continuation, follow-up and compaction. Reuse
  authorized artifact reads for the normalized thumbnail. Cancellation settles
  both the active request and the normal run state.

## Acceptance

Use a fake vision endpoint to verify the pixels, pinned request, response bounds,
shared permit, and exact text-only conversation payload. Test unauthorized or
cross-topic references, invalid uploads, repeated admissions, expired drafts,
timeout, cancellation and model mismatch. Then use the intended VLM with a
scratch image containing a unique word or number only in its pixels. Ask the
text model to read it and verify its answer and the two recorded model requests.
Check thumbnail display, a text follow-up, and explicit reattachment in the UI.
