# HTTP API v1

Phase 10 implementation contract. The authenticated `/openapi.json` and
[static OpenAPI fixture](examples/openapi.json) describe request shapes. HTTP
calls the existing store, run loop, scheduler and memory services. One lifespan
owns all clients, tasks, the data-directory lock and inference permit. A second
worker cannot own the same data directory. Partial startup/shutdown uses the
existing reverse cleanup protocol.

## Deployment and login

`sola serve` binds `127.0.0.1:8765` by default. Set the environment variable named
by `http.password_env` (default `SOLA_OPERATOR_PASSWORD`) before serving. There is
no default password. Missing credentials disable login with 503. Use a strong
operator password and keep it out of shell history. The runtime does not load
`.env` automatically. Neither the password nor raw login tokens enter JSONL,
state.db, diagnostic output or backups.

`POST /api/v1/auth/login` accepts `{"password":"…","transport":"cookie"}` or
`transport: "bearer"`. Every login requires an exact configured `Origin`, including
script clients. Cookie login sets host-only `sola_session` (HttpOnly) and
`sola_csrf` (readable by the client) with Path=/, SameSite=Strict and the configured
Secure flag; the response supplies `csrf_token`. Bearer login returns `token`
without setting cookies. Send it as `Authorization: Bearer …`. Never put tokens
in a URL. Credential query parameter names are rejected; access logging is off.

Sessions expire after 86400 seconds by default; there is no sliding renewal.
Only SHA-256 hashes of random 256-bit session/CSRF tokens are stored in state.db,
with expiry and revocation. They persist across normal restarts. Expired/revoked
rows are pruned at successful login. Password changes affect new logins; revoke
existing logins explicitly. Backup restoration preserves the recorded revocation
state and can restore an older login state: use revoke-all after restoring a
checkpoint if intervening revocations matter. Legacy browser cookies are not
accepted. Schema 1 upgrades transactionally to schema 2; restore supports both.

`GET /api/v1/auth/session` reports expiry/transport. `POST /auth/logout` revokes
the current token and clears cookies. `DELETE /auth/sessions` revokes all logins,
including the caller. These paths are under `/api/v1`. Five failed login attempts
within a fixed 60-second global window block further attempts until that window
ends. The counter persists across restart; forwarded client IPs cannot reset it.

For direct LAN access, bind all interfaces and explicitly list the server's
private IP address, scheme and port as used in the browser:

```toml
[http]
host = "0.0.0.0"
port = 8382
allowed_origins = ["http://192.168.1.20:8382", "http://localhost:8382"]
secure_cookie = false
```

For direct Tailnet access, bind the server's own Tailscale IP and use the same IP
in the browser URL. Replace `100.64.1.20` with the address assigned to the server.

```toml
[http]
host = "100.64.1.20"
port = 8382
allowed_origins = ["http://100.64.1.20:8382"]
secure_cookie = false
```

HTTP is allowed for localhost, loopback IPs, RFC 1918 IPv4 addresses, IPv6 ULA
addresses, and IPv4 addresses in the Tailscale shared range `100.64.0.0/10`.
See [Tailscale IP addresses](https://tailscale.com/docs/concepts/tailscale-ip-addresses).

Use brackets around IPv6 addresses in origins. Configure
the exact server URL, including the port when needed. Use Tailnet access policy
or a firewall to restrict clients. Authentication and CSRF checks still apply.
Restart after editing configuration. LAN HTTP sends credentials and conversation
data without encryption, so use it only on a trusted network.

For public IPs and hostnames, including MagicDNS names, configure exact HTTPS
origins and `secure_cookie = true`.
Terminate TLS in an operator-managed reverse proxy and preserve the configured
Host. Keep the direct HTTP backend private.
`serve` ignores proxy headers (`proxy_headers=False`); `Forwarded` and
`X-Forwarded-*` never establish Host, origin or client identity.

## Browser request matrix

The public chat shell at `/` and its explicitly allowlisted CSS and JavaScript
modules under `/assets/` permit GET without login. The shell includes the configured
`agent_name`; `/assets/user.css` serves the optional regular, non-linked
`<data_dir>/user.css`, or an empty stylesheet when absent. These presentation values
are public and must not contain private data. No other data-directory files are
served through this route. All API paths retain the authentication matrix below.

All paths, including health, require Host to match an authority in
`http.allowed_origins`. Duplicate Host, Origin, Authorization, CSRF and length
headers and duplicate login cookies are rejected. Scheme/authority origins are
canonical and exact; an allowed Host alone does not authorize an Origin.

| Authentication/request | Origin | CSRF | Result |
| --- | --- | --- | --- |
| Public `/health` | absent or allowed | none | coarse liveness only |
| Login | allowed required | password authenticates login | cookie or bearer receipt |
| Cookie read | absent or allowed | none | authenticated read |
| Cookie mutation | allowed required | valid `X-CSRF-Token` required | mutation permitted |
| Cookie mutation | absent, foreign, sibling, `null` | any | 403 |
| Bearer only | absent or allowed | exempt | authenticated request |
| Any request with foreign/`null` Origin | rejected | any | 403 |
| Cookie plus Authorization, even invalid cookie | any | any | 400; neither wins |
| Missing, invalid, expired or revoked token | otherwise valid | any | 401 |
| Host/Origin supplied only by forwarded headers | ignored | any | ordinary checks still apply |

No CORS middleware is enabled. OPTIONS does not bypass authentication. These
checks run before body parsing. Cookie mutations include prompt inspection and
all POST/PUT/PATCH/DELETE paths. A bearer client must omit ambient login cookies.
All responses use `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.

## Resources

All resource paths below have the `/api/v1` prefix. IDs and Idempotency-Key values
are 32 lowercase hex characters. Timestamps are aware UTC ISO strings. Unknown
body fields and malformed path/query/body fields receive 422. Bodies are capped
at 128 KiB, even without Content-Length. The binary image upload route has an
8 MiB cap on the actual bytes received after authentication and CSRF checks.
Session archives have a 128 MiB transfer limit and their own content type.
Errors use `{"error":{"code":"…","message":"…"}}` with a safe message.
404 means missing resource, 409 means conflicting state/current authority, 429
means a capacity limit, 413 means bounded input/output exceeded, 415 means an
unsupported upload media type, 416 means an artifact offset beyond EOF, and 503
means unavailable service/state.

| Resource | Operations and behavior |
| --- | --- |
| `/sessions` | POST creates shared/isolated/ephemeral topic, title <=256 characters; GET pages stable creation order, optional `q` title and `pinned` filters |
| `/sessions/catalog` | GET operator topic catalog with filters and newest-first creation cursor |
| `/sessions/catalog/profiles` | GET `{ "items": [name, ...] }` with sorted configured and recorded profile names |
| `/sessions/{id}` | GET; PATCH title/archived/pinned; mode stays frozen |
| `/sessions/archive` | POST `{ "session_ids": [id, ...] }` (1–100); returns `{ "archived": count }`; validates all IDs before one state transaction; duplicates/already archived sessions are no-ops |
| `/sessions/{id}/export` | GET a Solakit ZIP download for one topic and its delegated descendants |
| `/sessions/import` | POST one Solakit ZIP with an Idempotency-Key. Returns 201 and `{ "session_id": id }` for the copied root |
| `/sessions/{id}/messages` | GET public conversation pages; private background inputs/tools remain in run records |
| `/sessions/{id}/runs` | GET admission-order run pages; current state in each record. The newest page (`before=0`) adds `retryable`, a run ID or null |
| `/sessions/{id}/image-uploads` | POST one binary `image/png`, `image/jpeg`, or `image/webp`. Returns 201 and `{upload_id}` for a normalized, temporary PNG |
| `/sessions/{id}/image-uploads/{upload_id}` | GET the staged PNG while it is available |
| `/runs` | POST session_id, optional profile (server selection, normally `default`), content (1–32768 UTF-8 bytes), optional image reference `{upload_id}` or `{run_id, artifact_id}`; required Idempotency-Key; optional wait=0..30 seconds |
| `/runs/{id}` | GET current run, optional wait=0..30; wait expiry returns its current state |
| `/runs/{id}/cancel` | POST explicit cancellation; completed outcomes remain immutable |
| `/runs/{id}/retry` | POST [retry of a failed reply](#retrying-a-failed-reply); no body |
| `/runs/{id}/records` | GET ordered execution evidence including tool receipts and memory work |
| `/runs/{id}/prompts` | GET exact recorded model-request references for conversation, compaction and vision |
| `/runs/{id}/artifacts/{id}` | GET bounded verified bytes of a directly linked request/manifest/response/tool artifact |
| `/sessions/{id}/events` | GET [SSE protocol](events.md) |
| `/jobs` | GET stable creation pages; POST creates a disabled definition unless explicitly enabled |
| `/jobs/{id}` | GET/PATCH/DELETE through existing validation, narrowing and tombstones. PATCH `{"enabled":true/false}` toggles automatic scheduling |
| `/jobs/{id}/run` | POST explicit manual occurrence, including for disabled jobs. Requires Idempotency-Key and returns the occurrence with its durable run reference |
| `/jobs/{id}/occurrences` | GET occurrence pages including decisions and run references |
| `/jobs/control` | GET or PUT `{"paused":true/false}` for global scheduler pause |
| `/memory` | GET observer state and commit counts |
| `/memory/{id}/reconcile` | POST provider_task_id; verify ambiguous/failed work's provider ownership, never resubmit content |
| `/tools` | GET executable schemas for a selected profile |
| `/profiles` | GET configured profile authority with provenance and credential references redacted |
| `/health` | GET authenticated runtime/queue/memory/scheduler health; unversioned `/health` is public coarse liveness |
| `/search` | GET q, limit, optional session_id; independent evidence-backed search, no live memory call |
| `/prompt` | POST profile, content, optional hashes; private draft inspection using the assembler/tools, no recall/history/inference |

### Session transfer

Export returns `application/zip` as `solakit-session-{id}.zip`. All runs
in the topic and its delegated descendants must have reached a terminal state.
If a run is queued or running, the endpoint returns `409`. Export includes
private session evidence such as exact recorded prompts, tool activity, and
artifacts. [Archive format and validation rules](storage.md#session-transfer)

Send the ZIP bytes as the import request body with `Content-Type: application/zip`
and a 32-character lowercase hex `Idempotency-Key`. Cookie callers also send
the configured Origin and CSRF header. Import creates a new copy with its own
session and run IDs. The same key and identical bytes return the original copy’s
ID. A different payload with the same key returns `409`. A new key creates
another copy. A wrong content type returns `415`, malformed or unsupported
archives return `422`, and archives that exceed the byte or item limit return
`413`. Use the returned session ID with the normal session, event, and search
routes.

### Session listings

`GET /sessions?q=travel` matches a literal substring of the topic title/name,
ignoring case with Unicode case folding. Queries are limited to 256 characters;
leading/trailing whitespace is ignored and an empty query leaves the list unfiltered.
Filtering happens before pagination and combines with `archived` and
`include_ephemeral`; retain the same filters when following `next`. It searches
topic metadata in `state.db`, without reading message bodies or requiring `search.db`.
The optional `pinned=true` or `pinned=false` filter selects pinned or unpinned
topics before pagination; omitting it includes both. The API retains creation
order. The sidebar separately loads pins without a title query and places them
before the paginated, title-filtered unpinned topics.

### Run admission and delegation

Admission returns `{ "run": Run }` and Location. 202 means durably accepted and
pending; 200 means terminal, including failed/cancelled outcomes. Wait timeouts
and disconnected HTTP/SSE clients leave accepted work running. Same key and same
payload return the original run/frozen authority, including after grant changes.
Different content, topic, profile or image conflicts with 409. Image references
require a configured image route and either a live draft upload or an authorized
PNG from the same topic. Admission moves a draft into durable artifact evidence.
The original admission key and upload ID still return the same run after the
draft is removed or expires. For a new admission, missing, expired or off-topic
upload IDs return 404. Queue checks, archival, ephemeral lifetime, current
revocations and cancellation remain shared services. See [image inputs](../image-workflows.md).
Session/job creation and edits do not promise admission-key idempotency. Manual
job retry returns its original occurrence even after a definition edit.

#### Retrying a failed reply

`POST /runs/{id}/retry` admits a new run with `retry_of` set to the failed run and
returns the same `{ "run": Run }` response as admission. It records no user
message: the original message, attachment and frozen admission are reused,
current permissions apply, and the new run has its own budgets. Repeating the
request returns the same successor, so a lost response is safe to resubmit.

A run is eligible while it is the topic's latest run, failed, local, interactive
and not delegated, the topic is not archived, no attempt saved a reply, and every
recorded tool call has a certain receipt. Other states return 409; a full queue
returns 429. Cancelled and interrupted runs are not eligible.

Generation sees the settled tool exchanges of earlier attempts and none of their
partial reply text. A successful completed vision analysis is reused. A proposal
with the same name and arguments as an earlier successful or confirmed-effect
call is not dispatched; its receipt points to the earlier one.

Delegated topics carry `parent_session_id` and are available through authenticated
session and run reads. The parent run's `delegation` evidence row links the
parent tool call to `child_session_id` and `child_run_id` in sequence. The child
run carries `parent_tool_call_id` and uses the ordinary cancellation endpoint.
Direct run submissions and job definitions targeting the child return 409.

## Pagination and bounded inspection

Limits are 1..100, default 50 (search 20). Session/job/occurrence `after` is
`UTC_TIMESTAMP,ID`, with exclusive stable `(created_at,id)` order. Message/run/
record/prompt `after` is a nonnegative exclusive session sequence (default 0).
Responses supply `items` and `next`; stop when items is empty. Row items for
sequence pages contain `sequence`, `kind`, `record`, and `truncated`. Run updates
do not move their admission position: use GET or events to refresh their state.
Search is a bounded ranked query, without a pagination cursor. Metadata catalogs
are bounded single responses.

The topic catalog includes all memory modes and delegated topics. It combines
`q` (case-insensitive title substring), `profile` (exact match on any recorded
run), `mode`, and `status` (`active` by default, `archived`, or `all`). The
optional `created_from`, `created_before`, `activity_from`, and `activity_before`
parameters take aware UTC timestamps. `*_from` is inclusive and `*_before` is
exclusive. Each item extends session metadata with `last_activity_at`, the most
recent run's admission time or the creation time for a topic without runs. The
catalog orders pages newest first by `(created_at,id)` and sets `next` to `null`
on the last page.

Messages, session runs and run records additionally accept `before=0` for their
newest bounded page, or `before=<sequence>` for strictly older records. They
return items in ascending display order and `next` as the oldest returned
sequence to use as the next `before` cursor. An empty page ends backward paging.
Do not combine a nonzero `after` with `before`. Existing forward paging is
unchanged. Limits and byte caps apply before reversing a backward page, so large
records cannot skip unseen history. Truncated message/receipt views retain
origin, role and tool-call correlation. Truncated compaction records retain
`summary_message_id` so clients can identify the summary in the conversation.
Content stays bounded or artifact-backed.

Sequence pages stop around 512 KiB of canonical data, even before the requested
count. Individual execution records above 64 KiB return IDs and directly linked
artifact references with `truncated=true`; use those references or offline evidence
for full content. Other bounded inspections above 512 KiB fail with 413 and can
be narrowed. Large operator-created legacy session metadata may also require a
smaller page. Pagination is independent of the newest-50 event snapshot.

Artifact requests accept nonnegative offset and limit=1..262144 (default 65536).
The response is attachment/octet-stream, with X-Artifact-Length,
X-Artifact-SHA256 and X-Next-Offset. Request again at that offset until EOF.
Authorization precedes access; a guessed ID must also be directly linked to the
specified run's verified evidence. Symlinks, hardlinks, wrong lengths and checksum
mismatches fail closed. The complete artifact is hashed using bounded reads
before any bytes are returned. `preview=true` returns the complete linked,
bounded PNG inline and ignores the requested byte range. The API accepts only
artifact IDs linked to a run. Draft inspection is bounded. Inspect actual
requests through the run's prompt references.
