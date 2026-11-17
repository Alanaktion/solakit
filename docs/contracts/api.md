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

HTTP is allowed for exact loopback and private LAN IP origins (IPv4 RFC 1918
and IPv6 ULA, with brackets around IPv6 addresses). Entries identify the server
URL, not client IPs; this is not a client-network firewall. CIDR ranges and
wildcards are unsupported. Changing the listen port does not update allowed
origins. Restart after editing configuration. Authentication and CSRF checks
still apply. LAN HTTP sends credentials and conversation data without encryption;
use it only on a trusted network.

For public IPs, hostnames or tailnet access, configure exact HTTPS origins and
`secure_cookie = true`.
Terminate TLS in an operator-managed reverse proxy and preserve the configured
Host; keep the direct HTTP backend private. Tailnet membership grants no login.
Origins outside loopback/private LAN IPs without HTTPS/Secure cookies are
rejected by configuration.
`serve` ignores proxy headers (`proxy_headers=False`); `Forwarded` and
`X-Forwarded-*` never establish Host, origin or client identity. Direct localhost
HTTP and explicitly configured private LAN HTTP support insecure cookies. Remote
TLS/browser integration is a separate deployment verification, not claimed by
local tests.

## Browser request matrix

The public chat shell at `/` and its three explicit `/assets/` files permit GET
without login; they contain no runtime data or credential values. All API paths
retain the authentication matrix below.

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
at 128 KiB, even without Content-Length. A JSON error is always
`{"error":{"code":"…","message":"…"}}`; messages omit raw input/provider errors.
404 means missing resource, 409 means conflicting state/current authority, 429
means admission/login/event capacity, 413 means bounded input/output exceeded,
416 means an artifact offset beyond EOF, and 503 means unavailable service/state.

| Resource | Operations and behavior |
| --- | --- |
| `/sessions` | POST creates shared/isolated/ephemeral topic, title <=256 characters; GET pages stable creation order |
| `/sessions/{id}` | GET; PATCH title/archived/pinned; mode stays frozen |
| `/sessions/{id}/messages` | GET public conversation pages; private background inputs/tools remain in run records |
| `/sessions/{id}/runs` | GET admission-order run pages; current state in each record |
| `/runs` | POST session_id, optional profile (server selection, normally `default`), content (1–32768 UTF-8 bytes); required Idempotency-Key; optional wait=0..30 seconds |
| `/runs/{id}` | GET current run, optional wait=0..30; wait expiry returns its current state |
| `/runs/{id}/cancel` | POST explicit cancellation; completed outcomes remain immutable |
| `/runs/{id}/records` | GET ordered execution evidence including tool receipts and memory work |
| `/runs/{id}/prompts` | GET exact recorded model-request references; includes compaction requests |
| `/runs/{id}/artifacts/{id}` | GET bounded verified bytes of a directly linked request/manifest/response/tool artifact |
| `/sessions/{id}/events` | GET [SSE protocol](events.md) |
| `/jobs` | GET stable creation pages; POST creates a disabled definition unless explicitly enabled |
| `/jobs/{id}` | GET/PATCH/DELETE through existing validation, narrowing and tombstones; PATCH enabled/paused provides resume/pause |
| `/jobs/{id}/run` | POST explicit manual occurrence; required Idempotency-Key; the occurrence references its durable run |
| `/jobs/{id}/occurrences` | GET occurrence pages including decisions and run references |
| `/jobs/control` | GET or PUT `{"paused":true/false}` for global scheduler pause |
| `/memory` | GET observer state and commit counts |
| `/memory/{id}/reconcile` | POST provider_task_id; verify ambiguous/failed work's provider ownership, never resubmit content |
| `/tools` | GET executable schemas for a selected profile |
| `/profiles` | GET configured profile authority with provenance and credential references redacted |
| `/health` | GET authenticated runtime/queue/memory/scheduler health; unversioned `/health` is public coarse liveness |
| `/search` | GET q, limit, optional session_id; independent evidence-backed search, no live memory call |
| `/prompt` | POST profile, content, optional hashes; private draft inspection using the assembler/tools, no recall/history/inference |

Admission returns `{ "run": Run }` and Location. 202 means durably accepted and
pending; 200 means terminal, including failed/cancelled outcomes. Wait timeouts
and disconnected HTTP/SSE clients leave accepted work running. Same key and same
payload return the original run/frozen authority, including after grant changes.
Different content, topic or profile conflicts with 409. Queue checks, archival,
ephemeral lifetime, current revocations and cancellation remain shared services.
Session/job creation and edits do not promise admission-key idempotency. Manual
job retry returns its original occurrence even after a definition edit.

## Pagination and bounded inspection

Limits are 1..100, default 50 (search 20). Session/job/occurrence `after` is
`UTC_TIMESTAMP,ID`, with exclusive stable `(created_at,id)` order. Message/run/
record/prompt `after` is a nonnegative exclusive session sequence (default 0).
Responses supply `items` and `next`; stop when items is empty. Row items for
sequence pages contain `sequence`, `kind`, `record`, and `truncated`. Run updates
do not move their admission position: use GET or events to refresh their state.
Search is a bounded ranked query, without a pagination cursor. Metadata catalogs
are bounded single responses.

Messages, session runs and run records additionally accept `before=0` for their
newest bounded page, or `before=<sequence>` for strictly older records. They
return items in ascending display order and `next` as the oldest returned
sequence to use as the next `before` cursor. An empty page ends backward paging.
Do not combine a nonzero `after` with `before`. Existing forward paging is
unchanged. Limits and byte caps apply before reversing a backward page, so large
records cannot skip unseen history. Truncated message/receipt views retain
origin, role and tool-call correlation; content stays bounded or artifact-backed.

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
before any bytes are returned. Paths and arbitrary filesystem reads are not API
parameters. Draft inspection is bounded and does not become a recorded run;
inspect actual requests through the run's prompt references.
