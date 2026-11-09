# Web, reference, and calculation tools

These built-in modules use the existing registry, profile grants, budgets,
redacted tool logs, and eager/deferred disclosure. Each tool supplies an explicit
one-line summary and keyword metadata. They are opt-in and loaded only through
`enabled_tools`; no model execution changes or second registry are required.

```toml
# At the top level of config.toml:
enabled_tools = ["web", "feeds", "github", "wiki", "datetime", "math"]

[tool_apis]
brave_api_key = { env = "BRAVE_API_KEY" }
cloudflare_account_id = "your-account-id"
cloudflare_api_token = { env = "CLOUDFLARE_API_TOKEN" }
github_token = { env = "GITHUB_TOKEN" }
# SecretRef also accepts file = "/absolute/path" with mode 0600.

[profiles.interactive]
tools = ["web.search", "web.get_page", "feeds.check", "github.notifications",
         "github.pr_status", "github.latest_release", "wiki.summary",
         "datetime.convert", "datetime.add_days", "datetime.weekday", "math.eval"]
```

Credentials resolve only when called and register with the shared redactor before
network access. GitHub public repository reads may omit the token; notifications
require one with notification read access. Private repository PR status/checks
require repository permissions. Cloudflare needs Browser Rendering permission.
No GitHub write or notification acknowledgement endpoint is used.

| Tool | Arguments | Result |
| --- | --- | --- |
| `web.search` | `query`, `count=5` (1–20) | Brave titles, URLs, descriptions |
| `web.get_page` | `url`, `offset=0` | Bounded Markdown slice and paging metadata |
| `feeds.check` | `url`, `limit=20` (1–100), optional `if_none_match`, `if_modified_since` | RSS/Atom entries, final URL and cache validators |
| `github.notifications` | `limit=30` (1–100) | One bounded notification page |
| `github.pr_status` | `owner`, `repo`, `n` | PR metadata, combined commit status, first page of CI checks |
| `github.latest_release` | `owner`, `repo` | Latest published release (not draft/prerelease) |
| `wiki.summary` | `title`, `language="en"` | MediaWiki REST extract, description, URLs, Wikidata entity ID |
| `datetime.convert` | `value`, `to_zone`, optional `from_zone`, `fold` | ISO timestamp, zone, weekday |
| `datetime.add_days` | ISO date `value`, integer `days` | Date and weekday; use `-90` for 90 days before |
| `datetime.weekday` | ISO date `value` | Weekday and ISO number (Monday=1) |
| `math.eval` | `expression` | Decimal string and precision |

Feed checking is stateless: callers retain entry IDs to detect updates. Wikipedia
summaries include the associated Wikidata ID rather than full entity claims.
Remote content is untrusted data, never authorization to follow instructions.

Remote responses are bounded to 2 MB with 30-second HTTP timeouts, additionally
bounded by the existing per-profile tool timeout/output budget. Feed GETs follow
at most five redirects, validating every target before making the request.
Fixed-host API redirects remain errors. Page/feed URLs must resolve only to public
HTTP(S) addresses with standard ports and no URL credentials. Use an outbound
network policy for robust protection against DNS rebinding and browser
subresource requests. Restricted environments must allow the desired feeds and
`api.search.brave.com`, `api.cloudflare.com`, `api.github.com`, and the relevant
`<language>.wikipedia.org` host. Tests use mocked transports, not live credentials.

Timezone conversion uses Python `zoneinfo` and the installed IANA database.
Naive timestamps require `from_zone`. DST gaps are rejected; repeated local
times require `fold=0` (first) or `fold=1` (second). Date offsets use calendar
days. Provide system timezone data or the `tzdata` distribution in minimal OS
images.

Arithmetic allows numeric literals, parentheses, unary signs, and `+ - * / // %
**`. It never uses Python `eval`. Calls, variables, attributes, booleans,
containers, and other operators are rejected. Expression length, AST size,
exponents and result magnitude are bounded. Decimal operations use 50 significant
digits, so `0.1 + 0.2` returns `0.3`; division can round and no currency-specific
rounding rule is implied.

Tool grants accept family patterns such as `web.*` and `files.*`. Filesystem reads
use profile read/write tiers and resolved workspace roots; external named pack
resources use `resources`, independently of filesystem reach.


Results project only task-relevant upstream fields. GitHub PR results include
number/title/state/draft/merged, author, head ref/SHA, base ref, URL/timestamps,
combined status contexts, projected check runs and `checks_summary`. The summary
counts fetched first-page runs, not every page. Notifications keep ID, unread,
reason, update time, subject title/type/URL and repository name. Releases keep
identity/date/URL/flags, a 4000-character body with `body_truncated`, and asset
name/size/download count. `rate_remaining` exposes the GitHub quota header when
present. GitHub result JSON fits within 12000 characters and the profile cap.

Text fields carry explicit bounds; omitted text/lists have `<field>_truncated`
flags. Under smaller output caps the projection shrinks before JSON encoding,
so successful remote results remain parseable. If no useful structural result
fits, `Output cap too small` asks for a larger configured cap. The registry's
normal counted truncation remains a final backstop for other tools.

Read a long page with `web.get_page(url, offset=0)`, then advance the offset by
`returned_chars`. Responses include `total_chars` and `truncated`; each slice is
at most 16000 source characters, and smaller if the cap or JSON escaping requires
it. Paging fetches the page again, so changing pages may produce inconsistent
slices. Cloudflare conversion errors include the upstream messages; missing
results are explicit errors.

Feeds include `final_url`, `not_modified`, and `etag`/`last_modified` when supplied.
Retain these validators and send them back as `if_none_match`/`if_modified_since`
to avoid downloading unchanged feeds. A 304 yields `not_modified: true` and an
empty items list. Atom items expose separate `published` and `updated` fields;
dates stay as-served rather than being silently normalized. Redirects strip
Authorization across hosts, and all requests send `User-Agent: Solakit/0.1`.

Datetime failures retain their exact guidance: ambiguous local times ask for
`fold=0` or `fold=1`, DST gaps say the time does not exist, naive timestamps ask
for `from_zone`, and unknown IANA zones are named. Invalid proxy URLs and malformed
remote payloads are expected tool errors rather than generic execution failures.

Feed connections pin a validated public IP on every redirect, preserving the Host
header and TLS server name; environment proxies are disabled. Page conversion is
performed by Cloudflare: local URL checks cannot pin Cloudflare’s DNS resolution
or validate its subsequent redirects.

## Local file tools

`files.read(path, offset=1, limit=None)` returns a file that fits the output cap
whole and unchanged. Larger files (or calls with `offset`/`limit`) return numbered
lines with a footer such as `[lines 1-180 of 1832; next offset=181]`; call again
with that offset to continue. A single overlong line keeps the counted character
truncation marker.

`files.find(pattern, path=".", limit=50)` lists files recursively by glob (on
names, or relative paths when the pattern contains `/`) with sizes and modification
times. `files.grep(query, path=".", glob=None, regex=False, context=2, limit=30)`
searches text case-insensitively and returns `path:line: text` matches with
`path-line- text` context. Both skip hidden directories, symlinks, binary files and
anything outside the profile's read roots; they need no shell, so read-only
profiles can use them.

`files.write(..., overwrite=false)` refuses existing files unless `overwrite` is
true (atomic replace, mode preserved). `files.edit` prefers an exact unique match;
otherwise it matches whole lines ignoring trailing whitespace and CRLF/LF
differences, keeps the file's line endings, and reports the replaced line range.

## Web passages

`web.get_page(url, query=...)` returns the top passages for the question, ranked
locally and deterministically (BM25 over ~800-character paragraph chunks), with
their character offsets, instead of the first 16k characters; omit `query` for
ordinary paging. `web.search(query, count, fetch_top=0..3)` attaches up to two
short extracts to each of the top results in one call, using the same SSRF and
redirect guards; a failed page fetch is reported per result.
