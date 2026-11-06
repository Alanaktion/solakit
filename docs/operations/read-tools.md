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
| `web.get_page` | `url` | Cloudflare Browser Rendering Markdown |
| `feeds.check` | `url`, `limit=20` (1–100) | RSS 2.0 / Atom entry IDs, titles, URLs, dates |
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
bounded by the existing per-profile tool timeout/output budget. Redirects are
rejected; use the final feed URL. Page/feed URLs must resolve only to public
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
