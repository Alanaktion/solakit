# Configuration reference

`config.toml` is operator-owned. The CLI reads `~/.config/sola/config.toml` unless
you select another file with `--config PATH`. See the
[example](../config.example.toml) for built-in effective defaults and the
[loader](../src/solakit/config.py) for validation. When adding, removing or changing
a setting or default, update this reference and the example in the same change.

## Setup and validation

Copy the example to your chosen config path, then set `model.pin` to the exact
model ID served by oMLX and `defaults.workspace_roots` to at least one existing
directory. The untouched example is deliberately unconfigured: the built-in pin
is empty and the built-in roots are empty. A minimal runnable configuration is:

```toml
[model]
pin = "Qwen3.6-35B-A3B-mxfp4"

[defaults]
workspace_roots = ["."]
```

This grants no tools and leaves semantic memory disabled. Set the environment
variable `SOLA_OPERATOR_PASSWORD` before serving to enable login. Normal runtime
does not load `.env`; store credential values in the environment, with only their
variable names in TOML.

```sh
uv run sola check --config /path/to/config.toml --profile default --explain
uv run sola check --config /path/to/config.toml --live
uv run sola serve --config /path/to/config.toml
```

The first check is offline and read-only; explanation reports effective settings
and their sources with credential references redacted. `--live` explicitly checks
model/memory health and identity without inference. A missing default config
reports an unconfigured installation; a missing explicitly selected file or an
invalid config exits with status 2. See the [CLI guide](cli.md).

All filesystem paths expand `~` and resolve relative to the selected config
file's directory. Workspace directories, identity files and fragments must
already exist with the expected type. The data directory may be created at
runtime. Paths are canonicalized and duplicates removed while preserving order.

Types are strict: integers, booleans, strings and arrays must use their TOML types;
strings such as `"false"` are not booleans. Unknown keys fail validation, including
inside unused profiles. All numeric ranges below are inclusive. Optional settings
are omitted to use their default; TOML has no `null` value.

## Top-level values

Place these before any `[table]` header.

| Key | Type / built-in default | Meaning and constraints |
| --- | --- | --- |
| `schema_version` | integer: `1` | Only version `1` is supported. |
| `agent_name` | string: `"Sola"` | Agent display name throughout the web UI, including login, messages and page titles. Must contain a non-whitespace character; at most 128 characters. Restart the server and reload the page after changing it. Does not rewrite identity files or conversation evidence. |
| `data_dir` | string: `"~/.local/share/sola"` | Evidence, operational databases and managed runtime files. Must be nonempty and, if present, a directory. One runtime owns it at a time. |
| `soul_file` | optional path string: omitted | Operator-owned UTF-8 identity file, read verbatim. Must be an existing file when configured. |
| `curated_memory_file` | optional path string: omitted | Operator-owned UTF-8 curated memory file, read verbatim; independent of OpenViking being enabled. Must exist when configured. |
| `delegation_worker` | optional profile name: omitted | Selects one worker profile for `delegate.run`. Interactive caller profiles also need the tool grant. Workers use isolated topics and the intersection of worker and parent grants and roots. |
| `shutdown_timeout_seconds` | integer: `5` | Deadline in seconds for each of the two shutdown stages, HTTP connection drain and runtime cleanup. Range 1–60. |
| `tool_modules` | array of name strings: `[]` | Explicit importable modules for installed trusted Python packs. Each exports `SDK_VERSION = 1` and `TOOLS`; configuration resolution imports and registers them. Imports execute trusted code; registration grants no access. Missing modules or invalid declarations fail resolution. |

Names used for modules, profile names and memory account/user must
match `[A-Za-z0-9_][A-Za-z0-9_.-]*` and contain at most 128 characters. Module names
must also be importable Python modules.

Built-ins currently include `files.list`, `files.read`, `files.search`, `files.glob`, `files.grep`,
`files.write`, `files.append`, `files.edit`,
`files.rename`, `files.copy`, `files.mkdir`, `files.delete`,
`images.generate`, `transcripts.search`, `web.search`, `web.fetch`, `mcp.list`, `mcp.call`, `git.status`, `git.diff`,
`git.pull`, `git.publish`, `shell.exec`, `delegate.run`,
`runtime.status`, `memory.recall`, `memory.remember`, and `jobs.list`, `jobs.create`,
`jobs.update`, `jobs.pause`, `jobs.resume`, `jobs.run_now`, `jobs.delete`,
`jobs.pause_all`, `blog.list`, `blog.read`, `blog.create`, `blog.publish`,
`dreams.list`, `dreams.read`, and `dreams.create`.
Built-ins are always registered; enable them in the selected profile's
`tools`. Remove the obsolete top-level `loaded_tools` key from existing configuration;
it is rejected. There are no separate `resources` or `effects` settings. See the
[tool contract](contracts/tools.md) for protected paths and pack permissions.
`files.*` expands to all registered file tools at admission. Use exact names to
grant a subset.

The other supported top-level keys are tables: `model`, `http`, `memory`, `images`, `blog`, `git`, `letters`, `web_search`, `web_fetch`, `mcp_servers`, `shell`,
`defaults`, `profiles`, `proactive_budgets` and `background`, described below.
The example pack's image adapter remains a fixture; the optional built-in image
service uses the fixed [ComfyUI workflow](image-workflows.md).

## `[model]`

| Key | Type / built-in default | Meaning and constraints |
| --- | --- | --- |
| `model.pin` | string: `""` | Exact model ID. A nonblank explicit pin is required for a runnable profile; no automatic model substitution. The tested pin is `Qwen3.6-35B-A3B-mxfp4`. |
| `model.image_inputs` | boolean: `false` | Enable native PNG attachments only after verifying the exact conversation pin's image capability. Mutually exclusive with `[vision]`. |
| `model.base_url` | string: `"http://127.0.0.1:8000/v1"` | oMLX OpenAI-compatible API base URL, including its API path. |
| `model.context_tokens` | integer: `32768` | Request context ceiling, 1024–1048576 tokens. Conservative accounting reserves completion, tool-result and safety headroom; this does not change the server's actual model window. |
| `model.connect_timeout_seconds` | integer: `5` | Connection timeout, 1–60 seconds. |
| `model.generation_timeout_seconds` | integer: `120` | Generation deadline, 1–3600 seconds, including streamed completion. |
| `model.api_key_env` | optional environment-variable name: omitted | When set, that variable must contain a credential. When omitted, uses `OPENAI_API_KEY` if present, otherwise the local placeholder `local`. |

Model and memory base URLs require `http` or `https`, a hostname and a nonzero
port if specified. They reject embedded credentials, query strings and fragments.
Environment-variable names match `[A-Za-z_][A-Za-z0-9_]*`.
See the [model/prompt contract](contracts/model-prompts.md) and
[context contract](contracts/context-compaction.md) for request accounting.

## `[vision]`

Omit this section to disable separate recognition. Configure it together with
`model.image_inputs = false` and restart the server.

| Key | Type / built-in default | Meaning and constraints |
| --- | --- | --- |
| `vision.pin` | required nonblank string | Exact model ID expected in completion responses. |
| `vision.base_url` | required URL | OpenAI-compatible API base URL using the same URL constraints as `model.base_url`. |
| `vision.api_key_env` | optional environment-variable name | Use the named credential. Omission uses the local placeholder `local`. |
| `vision.connect_timeout_seconds` | integer: `5` | Connection timeout, 1–60 seconds. |
| `vision.generation_timeout_seconds` | integer: `120` | Completion deadline, 1–3600 seconds within the run's active-time budget. |

The endpoint must accept a PNG data URL in a non-streaming chat completion
request and identify the exact pin in its JSON response. Vision spends one
model call in addition to the conversation call.

## `[blog]`

| Key | Type / built-in default | Meaning and constraints |
| --- | --- | --- |
| `blog.repository` | optional path string: omitted | Existing blog checkout, resolved relative to this config file. Unset disables blog/dream operations. Calls require the entire checkout within current and admitted workspace roots. Changing this path requires a new admission. Publication builds with npm, commits pending content, and pushes all existing pending commits to `origin/main`. |

Grant `blog.*` and/or `dreams.*` as needed; reading needs only the respective
list/read grants. `blog.create` includes the `auto_publish` capability;
`dreams.create` always publishes. Build/push commands use the server's PATH and
existing Git credentials. Give publication an adequate tool timeout, within the
run's active budget. Background jobs also need explicit workspace roots, and
existing quiet-hour rules apply to these consequential tools, including draft
creation. See the [blog and dream guide](blog.md).

## `[images]`

| Key | Type / built-in default | Meaning and constraints |
| --- | --- | --- |
| `images.enabled` | boolean: `false` | Start the optional ComfyUI observer/client. Execution also requires `images.generate` in loaded tools and profile grants. |
| `images.base_url` | string: `"http://127.0.0.1:8188"` | Explicit operator endpoint, with the same URL restrictions as the model. No endpoint or workflow argument is exposed to the model. |
| `images.timeout_seconds` | integer: `15` | Whole HTTP request deadline, 1–60 seconds. No submission retries or redirects. |
| `images.poll_seconds` | integer: `2` | Delay between observation passes, 1–60 seconds. |
| `images.max_checks` | integer: `300` | Persisted observation limit, 1–600. Exhaustion leaves uncertainty and blocks new generation until reconciliation. |

One unfinished generation is allowed globally. Set `defaults.budgets.tool_timeout_seconds`
high enough for the selected endpoint (for example 600, within `active_seconds`).
The usual run deadline still applies. Cancellation stops waiting; provider work
may continue and is observed separately. See [recovery](image-workflows.md).

## `[git.repositories.<alias>]`

Set `path` to an existing checkout and `branch` to the expected local and origin
branch, such as `main`. Relative paths resolve from the configuration file.
List the alias and its purpose in an operator-owned profile prompt fragment so
Sola can select it in tool calls. Include the checkout in the profile's
workspace roots and grant the individual tools it needs. Empty
configuration disables the tools. Matching wildcard grants can expose newly
configured Git tools to new admissions. Pull and publication also require an
interactive run.

## `[letters]`

Set `repository` to an existing operator-trusted Git checkout. Paths resolve
like `[blog] repository`, including paths relative to the configuration file.
Set `branch` to the checked-out branch and the corresponding branch on
`origin`. Set distinct `author` and `peer` identifiers with 1–64 lowercase
letters, digits, underscores or hyphens, starting with a letter.
`directory` defaults to `"."`. It must be repository-relative and contain
ordinary directories.

A repository-only setting reserves the entire checkout. Use a separate
checkout for blog and dream content. Generic guarded writes, Git mutations and
blog or dream mutations are denied for the reservation, including aliases.
Git inspection can be granted separately.

Admit the checkout in `defaults.workspace_roots` and grant the named letters
tools in `defaults.tools` before narrowing them in dedicated profiles.
A correspondence profile needs only `letters.inbox`, `letters.read`,
`letters.send` and `letters.reply`. For a scheduled job, use a profile with
`letters.inbox` and `letters.read`, and set the job's own `workspace_roots`
explicitly for inbox updates. Existing wildcard grants may match newly enabled
letters tools at admission. [Tool behavior and legacy reply records](contracts/tools.md#git-backed-letters)

## `[web_search]`

| Key | Type / built-in default | Meaning and constraints |
| --- | --- | --- |
| `web_search.api_key_env` | optional environment-variable name, omitted | Enables Brave web search when set. Supply the Brave API key in this variable in the server environment and grant `web.search` in `defaults.tools` or the selected profile's inherited grants. |

Enablement can expose `web.search` to profiles with matching wildcard grants at
new admission. Changing or removing this reference revokes search in existing
runs. Only the environment-variable name is saved in admissions. Search queries
go to Brave and are kept with the tool result in normal tool evidence. See the
[web search contract](contracts/tools.md#public-web-search).

## `[web_fetch]`

Omit this table to disable `web.fetch`. When configured, grant `web.fetch` in
`defaults.tools` or the selected profile's inherited grants. Matching wildcard
grants can expose the tool on new admissions.

| Key | Type | Meaning |
| --- | --- | --- |
| `web_fetch.account_id` | 32-character hexadecimal string, required | Cloudflare account ID for Browser Run. |
| `web_fetch.api_token_env` | environment-variable name, required | Server environment variable holding a Cloudflare API token scoped to that account with Browser Run - Edit permission. |

Changing either field or removing the table revokes the tool from existing
runs. Cloudflare receives the requested URL, and Solakit stores the URL and
returned Markdown in regular tool evidence. [Tool contract](contracts/tools.md#webpage-markdown) · [Cloudflare setup](https://developers.cloudflare.com/browser-run/quick-actions/markdown-endpoint/)

## `[mcp_servers.<name>]`

| Key | Type / built-in default | Meaning and constraints |
| --- | --- | --- |
| `url` | required HTTP(S) URL | Streamable HTTP MCP endpoint. No credentials, query or fragment. stdio servers are not supported. |
| `headers` | table of strings, empty | Literal, non-secret request headers. |
| `headers_env` | table of environment-variable names, empty | Each header's whole value, such as `Bearer <token>`, read from the server environment at call time and redacted from evidence. A missing value fails the call. |

Any configured server enables `mcp.list` and `mcp.call` for profiles that grant
them. Both tools take a `server` name, so a grant covers every configured server.
Changing or removing `mcp_servers` revokes both tools in existing runs. See the
[MCP contract](contracts/tools.md#remote-mcp-tools).

## `[shell]`

Set `enabled = true` here and include the exact name `shell.exec` in
`defaults.tools` to grant interactive commands. The default is `false`.
Child profiles inherit the grant when they inherit their parent's tools.
If a child supplies a new tools list, it must retain the literal grant from its
parent. Wildcards confer no shell authority.

**Commands have the server account's filesystem and network privileges.**
Workspace roots govern the starting directory. Deploy the server with OS
isolation when commands need narrower filesystem or network access. The
command's arguments and retained output are part of durable tool evidence.
See the [literal command contract](contracts/tools.md#literal-command-execution).

## `[http]`

| Key | Type / built-in default | Meaning and constraints |
| --- | --- | --- |
| `http.host` | string: `"127.0.0.1"` | Server bind address. Use the server's Tailscale IP to listen on that address, or `"0.0.0.0"` to listen on all IPv4 interfaces. |
| `http.port` | integer: `8765` | Listen port, 1–65535. Changing it does not update allowed origins. |
| `http.allowed_origins` | array of strings: `["http://127.0.0.1:8765", "http://localhost:8765"]` | Nonempty exact server origins accepted by Host/origin checks; duplicates are removed. Use scheme and lowercase authority only, with no path or trailing slash. No embedded credentials, query or fragment. |
| `http.secure_cookie` | boolean: `false` | Set the Secure flag on login cookies. Required with HTTPS origins outside loopback, private LAN, and Tailnet IP ranges. |
| `http.password_env` | environment-variable name: `"SOLA_OPERATOR_PASSWORD"` | Login password source. No default password; missing/empty credentials disable login. |
| `http.session_seconds` | integer: `86400` | Login lifetime, 60–2592000 seconds (up to 30 days). Fixed expiry; sessions survive ordinary restarts. |

Each allowed origin identifies the exact server URL used by the client. HTTP
with insecure cookies is allowed for localhost, loopback IPs, RFC 1918 IPv4
addresses, IPv6 ULA addresses, and IPv4 addresses in the Tailscale shared range
`100.64.0.0/10`. Other IPs and hostnames require HTTPS and `secure_cookie = true`.
Use Tailnet access policy or a firewall to control which clients can connect.

Restart after deployment configuration changes. Remote TLS termination requires
an operator-managed proxy with a private backend and the configured Host preserved;
forwarded headers are not trusted. See the
[deployment and login guide](contracts/api.md#deployment-and-login) for LAN and Tailnet examples
and authentication behavior.

## `[memory]`

| Key | Type / built-in default | Meaning and constraints |
| --- | --- | --- |
| `memory.enabled` | boolean: `false` | Opt in to OpenViking. Profile memory mode and session mode also apply. |
| `memory.base_url` | string: `"http://127.0.0.1:1933"` | OpenViking HTTP endpoint; follows the base URL rules above. |
| `memory.account` | name string: `"default"` | Selected OpenViking account, sent in `X-OpenViking-Account`. |
| `memory.user` | name string: `"default"` | Selected OpenViking user, sent in `X-OpenViking-User`. |
| `memory.roots` | array of strings: `[]` | Managed roots in the form `viking://user/USER/memories`. Trailing slashes and duplicate roots are removed. When enabled, must contain exactly `viking://user/<memory.user>/memories`. |
| `memory.api_key_env` | optional environment-variable name: omitted | Optional `X-API-Key` source. When configured, a missing/empty variable makes memory requests fail. No automatic environment-variable fallback. |
| `memory.timeout_seconds` | integer: `15` | HTTP request timeout, 1–300 seconds. |
| `memory.backlog_cap` | integer: `128` | Maximum unfinished/ambiguous commits before a new commit is recorded as failed, 1–10000. Existing pending, running, accepted and ambiguous work counts toward the cap. |

Enabling memory requires configuring its root even if a selected profile disables
memory. Automatic recall/extraction needs enabled memory and a shared session
whose profile mode is `optional` or `required`; explicit memory tools additionally
need tool loading and grants. Supported OpenViking versions are stable
`>=0.5.5,<0.6.0`, including four-part hotfixes; 0.5.5 is the live-tested baseline.
See [memory setup and recovery](memory.md).

## `[defaults]` and `[profiles.NAME]`

`defaults` defines the `default` profile. Named profiles inherit it, or another
named parent through `extends`. No child profiles exist by default.
The following keys work in both tables; replace `defaults` with `profiles.NAME`
for a child override.

| Key | Type / effective default | Meaning and constraints |
| --- | --- | --- |
| `defaults.extends` | name string: `"default"` | For child profiles, selects the parent. In `[defaults]`, only `"default"` is valid. |
| `defaults.workspace_roots` | array of path strings: `[]` | Granted existing directories. A runnable profile requires at least one. Child roots must lie within inherited canonical roots. |
| `defaults.tools` | array of tool grants: `[]` | Exact names or case-sensitive `*` / `?` patterns. Patterns expand against registered built-ins and `tool_modules` tools; exact unregistered names remain inspectable but cannot execute. Child grants must be a subset of inherited grants. |
| `defaults.memory_mode` | string: `"optional"` | `"off"` disables semantic memory; `"optional"` allows conversation through memory outages; `"required"` fails the run if initial recall is unavailable. Does not enable the service or widen scope. |
| `defaults.fragments` | array of path strings: `[]` | Ordered operator-owned prompt files. Must exist. Overrides replace the inherited list, preserving order and removing duplicates. |
| `defaults.budgets` | table: built-in run budgets below | Per-field limits; use `[defaults.budgets]` or `[profiles.NAME.budgets]`. |

Tool grant strings contain at most 128 characters and match
`[A-Za-z0-9_*?][A-Za-z0-9_.*?-]*`.

Omitted profile values inherit; explicit `[]` clears a list. Empty workspace roots
therefore make a profile unrunnable. Fragments replace rather than append and may
select different files. Children cannot enable inherited `memory_mode = "off"`;
otherwise they may select `optional` or `required`. Isolated and ephemeral sessions
always force memory off. Required mode governs recall failure, not asynchronous
extraction completion.

The profile name `default` is reserved; do not create `[profiles.default]`.
Unknown parents and inheritance cycles fail resolution. Semantic inheritance and
path checks apply to the selected profile and its ancestors; type/unknown-key
validation applies to every table.

```toml
[profiles.short]
extends = "default"
tools = []

[profiles.short.budgets]
model_calls = 4
tool_calls = 8
active_seconds = 300
```

Select with `--profile short`. The child can only lower inherited budgets.
See the [runtime contract](contracts/runtime.md#configuration-and-effective-authority).

## Run budgets and `[proactive_budgets]`

All six keys below are accepted in `[defaults.budgets]`,
`[profiles.NAME.budgets]` and `[proactive_budgets]`. Omitted fields inherit.
Ordinary run defaults apply before profile overrides; scheduled and proactive
origins then take the minimum of the profile limit and their automatic ceiling.

| Key | Ordinary default | Automatic default ceiling | Range / meaning |
| --- | --- | --- | --- |
| `model_calls` | `16` | `8` | 1–256 provider attempts per run, including recovery attempts. |
| `tool_calls` | `32` | `12` | 1–256 tool dispatch attempts per run, including denials. |
| `active_seconds` | `900` | `600` | 1–86400 seconds after run start, including inference waits; excludes queued admission time. |
| `output_tokens` | `4096` | inherited | 1–32768 requested completion tokens per model call. Must fit the context ceiling together with prompt and other reserves. |
| `tool_result_bytes` | `65536` | inherited | 1–1048576 UTF-8 bytes per model-facing tool result; full artifacts have separate bounds. |
| `tool_timeout_seconds` | `60` | inherited | 1–3600 seconds per tool attempt. |

`[proactive_budgets]` can raise or lower automatic ceilings within these ranges,
but cannot exceed the selected profile's limits. It applies to both scheduled
and proactive work; job-specific budgets can narrow limits further. For example,
`proactive_budgets.model_calls = 12` allows up to 12 calls only when the selected
profile permits at least 12.

Admission freezes authority. Current revocations and lower limits apply at
dispatch; later additions cannot widen an admitted run. Runtime/model identity
or memory scope changes require a new admission. See the
[run-loop contract](contracts/run-loop.md).

## `[background]`

Both automatic scheduled and proactive work share these controls. Configuring
them does not create or enable jobs: definitions, per-job schedules/timezones,
enabled state and the global pause live in `state.db` and are managed through
the API/Settings. New job definitions are disabled unless explicitly enabled.

| Key | Type / built-in default | Meaning and constraints |
| --- | --- | --- |
| `background.timezone` | string: `"America/Denver"` | Valid IANA timezone for quiet-hour policy. Jobs retain their own separately configured timezone. |
| `background.quiet_start` | integer: `22` | Quiet hours begin at this local hour, inclusive; 0–23. |
| `background.quiet_end` | integer: `8` | Quiet hours end at this local hour, exclusive; 0–23. Equal start/end disables quiet hours. Overnight intervals are supported. |
| `background.topic_cooldown_seconds` | integer: `3600` | Minimum spacing for automatic admissions per topic and for published notifications per topic, 1–86400 seconds. |
| `background.hourly_runs` | integer: `4` | Automatic admissions per rolling hour, 1–256. |
| `background.daily_runs` | integer: `24` | Automatic admissions per rolling 24 hours, 1–10000. |
| `background.max_active` | integer: `1` | Concurrent active background occurrences, 1–256; the shared run service's capacity still applies. |
| `background.max_pending` | integer: `8` | Pending background occurrences, 1–256; shared queue bounds still apply. |
| `background.notification_ttl_seconds` | integer: `86400` | Lifetime of pending local delivery before expiry, 1–86400 seconds. |

Durable occurrence counters include failed and no-action runs and survive
restart. Explicit manual occurrences bypass automatic rate and quiet-hour limits,
while still obeying pause, overlap and queue bounds. During quiet hours,
outbound-capable automatic work waits; local preparation can continue. Pending
topic publication waits until quiet hours/global pause end, coalesces per topic,
and expires at its TTL. Automatic work cannot create further wake-ups.
See the [jobs contract](contracts/jobs.md) for scheduling, delivery and recovery.
