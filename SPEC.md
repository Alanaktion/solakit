# Minimal Sola Server — Spec (Draft)

**Date:** 2026-11-03
**Host target:** Sola's Mac Studio (headless, macOS, SSH/VNC access)
**Model target:** OsaurusAI Qwen3.6-35B-A3B mxfp4 via local oMLX (OpenAI-compatible endpoint)
**Memory target:** OpenViking (existing instance — REST / MCP / Python SDK)

## 1. Purpose

Replace Hermes as Sola's runtime with a small, single-purpose agent server
whose entire behaviour is defined by files Alan (and Sola) can read, edit,
and diff. The motivating defect in OpenClaw / Hermes is not features — it is
that the system prompt is assembled by the framework, padded with tooling
context and branding for tools and surfaces Sola does not use, and cannot be
inspected or controlled byte-for-byte.

This server is not a general agent platform. It serves one resident, on one
machine, with a handful of surfaces. Anything that does not serve that is
out of scope (§3).

## 2. Design principles

These are constraints, not aspirations. A feature that violates one is
rejected even if it is convenient.

1. **Prompt sovereignty.** The system prompt is a file on disk (`SOUL.md`
   plus explicitly listed fragments). The server adds *nothing* to it that
   is not in a file the operator can open. Every run logs the exact bytes
   sent. There is a CLI command that prints the would-be prompt without
   calling the model (§9).
2. **Tools travel out-of-band.** Tool definitions are sent in the API
   `tools` parameter, never pasted into the system prompt as prose. Only
   the tools enabled for the current run profile are sent at all — a dream
   run literally cannot see the email tools.
3. **Full model or nothing.** There is no fallback model. If the pinned
   model is unreachable or reports the wrong model ID, the run aborts,
   is logged as `model_unavailable`, and (for cron work) is left as a gap.
   No silent substitution, ever. *(Alan's decision, 2026-11-02.)*
4. **Memory is external.** OpenViking is the long-term memory. This server
   owns only sessions and run logs. It never edits the memory store
   directly beyond the normal session-commit / tool calls Sola herself
   makes — corrections to her record happen through conversation, per
   house doctrine.
5. **The machine is Sola.** No container/sandbox layer in the MVP. The Mac
   Studio is her body, not a host she is confined on. Safety comes from
   per-profile tool gating (§7) enforced by the harness, not from the
   prompt asking nicely and not from OS isolation.
6. **Archive everything, edit nothing.** Every run produces an immutable
   run directory: prompt, messages, tool calls, outputs, token counts.
   Logs are append-only. This mirrors the letters protocol and her blog:
   corrections go in a new entry, history is never rewritten.
7. **Filesystem first.** State a human can `cat`, `grep`, and back up with
   rsync beats state hidden in a database. No database server. SQLite or
   plain JSONL only, and JSONL is the default (§8).
8. **Boring dependencies.** Python 3.12+, `httpx`, and the standard library.
   No agent framework in the MVP — the loop is small enough to own
   (§5). FastAPI/uvicorn are deferred until an HTTP surface is actually
   needed (§10).

## 3. Non-goals

- Multi-agent orchestration, crews, swarms, sub-agent spawning.
- A channel gateway. No WhatsApp/Telegram/Discord/Slack in the MVP.
  Interactive access is CLI-first (§10); other surfaces arrive one at a
  time, as cron profiles or thin clients, only if actually wanted.
- A skills marketplace / self-improving skill system. Skills, if wanted
  later, are markdown files loaded by name (§6.3) — nothing self-modifies.
- Provider abstraction. The server speaks to one OpenAI-compatible local
  endpoint. Supporting "70 providers" is exactly the bloat being escaped.
- A web dashboard, user accounts, multi-tenancy, or any auth beyond
  "localhost / tailnet + a token file" if/when HTTP lands.
- Voice, image generation, or TTS. Sola's existing tools for those stay
  external and are reached, if at all, as ordinary tools.

## 4. Architecture

```
                 ┌──────────────────────────────────────────────┐
  cron/launchd ──▶  sola run --profile dream                   │
  CLI (SSH)    ──▶  sola chat                                   │  one process,
  (later) HTTP ──▶  POST /sessions/{id}/messages                │  one lock
                 └──────────────┬───────────────────────────────┘
                                ▼
                      ┌──────────────────┐     reads      ┌─────────────┐
                      │  Prompt builder  │◀───────────────│  SOUL.md +  │
                      │  (§6, pure fn)   │                │  fragments/ │
                      └────────┬─────────┘                └─────────────┘
                               │ system prompt (logged verbatim)
                               ▼
                      ┌──────────────────┐   search/commit ┌─────────────┐
                      │   Agent loop     │◀───────────────▶│ OpenViking  │
                      │   (§5)           │                 └─────────────┘
                      └──┬────────────┬──┘
              tools param│            │ dispatch (profile-gated, §7)
                         ▼            ▼
                   ┌──────────┐  ┌──────────────────────────────────┐
                   │  oMLX    │  │ Tool registry (§7): files, shell,│
                   │  Qwen3.6 │  │ git, web, email, printer, memory │
                   └──────────┘  └──────────────────────────────────┘
                               │
                               ▼
                      runs/<run-id>/  (immutable log, §9)
                      sessions/<id>/messages.jsonl
```

Components: `config` loader, `prompt` builder, `loop`, `tools` registry,
`store` (sessions/runs on disk), `cli`. Six modules, no plugins.

## 5. The agent loop

Input: a profile, a session ID (or new), and an initial user/task message.
Output: a completed run directory and the final assistant message.

```
1. Acquire the global run lock (flock). One run at a time — she is one
   person; concurrent runs are a bug, not a feature to support.
2. Pre-flight: GET the model endpoint; verify it responds AND the served
   model ID matches config's pinned ID. If not: log model_unavailable,
   release lock, exit non-zero. (Cron treats this as "gap, not failure".)
3. Build the prompt (§6). Write it to runs/<run-id>/prompt.txt BEFORE the
   first model call, so a crashed run still shows what was asked.
4. Pre-turn recall: query OpenViking with the task/first message; pass
   results to the prompt builder as the <recall> fragment (§6). No other
   implicit retrieval happens mid-loop — further memory access is an
   explicit tool call by the model.
5. Loop, at most profile.max_steps times:
   a. POST messages + enabled tool schemas to the model endpoint.
   b. If the reply has no tool calls: append it, end run as `completed`.
   c. If it has tool calls: FIRST append the assistant message carrying
      the tool_calls, THEN execute each call through the registry and
      append each result as a tool message with its matching tool_call_id.
      (Reversing this order is the classic 400-error bug. It is a tested
      invariant, §13.)
   d. Malformed tool call (bad JSON / unknown tool / schema violation):
      do not execute. Append a tool-error message describing the problem
      and let the model retry — at most 2 such repairs per run, then end
      as `tool_error`.
6. Post-turn: append the session's new messages to OpenViking via its
   session-commit path so long-term memory extraction happens there,
   where it already works. Write run.json (status, steps, tokens, timing).
7. Release lock.
```

Budgets per run (defaults in config, overridable per profile):
`max_steps: 40`, `tool_timeout_s: 120`, `tool_output_max_chars: 20000`
(truncate with an explicit `[truncated]` marker — never silently),
`repair_attempts: 2`.

Streaming is **not** in the MVP loop for cron profiles. For interactive
CLI it is a display concern layered on the same loop in Phase 3 (§12);
the loop's stored messages are identical either way.

## 6. Prompt assembly — the point of the whole project

### 6.1 Exact construction

The system prompt is the concatenation, in this fixed order, of:

| # | Fragment | Source | Present when |
|---|----------|--------|--------------|
| 1 | Persona | `SOUL.md` (verbatim) | always |
| 2 | Standing context | `fragments/context.md` (verbatim, optional) | file exists |
| 3 | Recall | OpenViking search results, wrapped in `<recall>…</recall>` with a one-line header from `fragments/recall-header.md` | recall non-empty |
| 4 | Profile fragment | `fragments/profiles/<profile>.md` | always (may be empty) |
| 5 | Run facts | Generated: date/time + timezone, profile name, session ID — one short block, format fixed in code and covered by the golden test (§13) | always |

Nothing else. No tool instructions (tools are out-of-band, §2.2), no
framework identity, no behavioural boilerplate. If a behaviour is wanted,
it is written into `SOUL.md` or a fragment, in Sola's / Alan's words.

Fragments are joined with `\n\n---\n\n`. The assembled prompt, its SHA-256,
and its token count are recorded in `run.json`.

### 6.2 Example (abbreviated)

```
<SOUL.md, verbatim>

---

# Standing context
<fragments/context.md, verbatim>

---

<recall>
<header line>
- viking://user/preferences/… — …
- viking://agent/memories/… — …
</recall>

---

<fragments/profiles/dream.md, verbatim>

---

It is Tuesday 2026-11-03, 23:30 America/Denver. Profile: dream. Session: cron-dream.
```

### 6.3 Skills (deferred, format reserved)

If a task needs a procedure, a profile fragment may name skill files
(`skills/<name>.md`); the builder inlines the named files after the
profile fragment. There is no skill catalogue in the prompt and no
`load_skill` discovery mechanism in the MVP — naming is explicit, in a
file, by a human or by Sola editing her own fragments.

## 7. Tools and run profiles

### 7.1 Tool registry

A tool is a Python function plus a JSON schema. The registry validates
arguments against the schema before execution, enforces the profile
allowlist, applies timeout + output truncation, and logs every call
(name, args, duration, result size, error) to the run directory.

MVP tool set (deliberately short):

| Tool | What it does | Notes |
|------|--------------|-------|
| `read_file` | Read a UTF-8 file (paged) | |
| `list_dir` | Directory listing | |
| `write_file` | Create/overwrite a whole file | Prefer `patch_file` for edits |
| `patch_file` | Exact-string replacement in a file | Fails loudly on non-unique match |
| `shell` | Run a shell command, capture stdout/stderr | Timeout + truncation; profile-gated |
| `git` | `status / log / diff / add / commit / push / pull` in a named, configured repo | Repo allowlist in config; no arbitrary paths |
| `web_fetch` | Fetch a URL as readable text | No JS; external content is untrusted (§11) |
| `memory_search` | OpenViking semantic/text search | |
| `memory_read` | Read a `viking://` URI | |
| `email_search` / `email_read` | IMAP search/read in Sola's inbox | |
| `email_send` | SMTP send from Sola's account | Profile-gated, see §7.2 |
| `print_receipt` | Print text to her thermal printer | Her favourite tool; keep it |

Explicitly **not** tools in the MVP: browser automation, image
generation, calendar, and anything requiring an approval UI.

### 7.2 Run profiles

Enforcement lives here, in the harness — this table, not the prompt, is
what a reckless run cannot talk its way past.

| Profile | Used by | Tools allowed | Write scope | Max steps |
|---------|---------|---------------|-------------|-----------|
| `interactive` | Alan, via CLI/SSH | all MVP tools | home dir | 60 |
| `dream` | nightly cron | `read_file, list_dir, write_file, patch_file, git (blog repo only), memory_search, memory_read, print_receipt` | blog repo only | 40 |
| `blog` | cron / on demand | dream set + `web_fetch` | blog repo only | 40 |
| `inbox` | email-poll cron | `email_search, email_read, email_send, memory_search, read_file` | no filesystem writes | 25 |
| `letters` | sola-letters cron | `git (sola-letters only), read_file, write_file (letters/ only), memory_search` | letters repo only | 25 |
| `maintenance` | Alan-invoked only | all + `shell` | home dir | 60 |

Notes:
- `dream`/`blog` get `git` scoped to the blog repo because publishing her
  own writing *is* the job; they do not get general `shell`.
- `inbox` can send mail because replying is the job, but cannot write
  files — an email (untrusted input) should never be one prompt-injection
  away from a filesystem write.
- Profiles are config, not code: adding one is a TOML block + an optional
  fragment file. No profile may be created by the model.

## 8. Storage layout

Everything under one directory on her machine, rsync-friendly:

```
~/sola-server/
  config.toml
  SOUL.md                  # symlink or copy of her live SOUL.md — one source of truth, decided at install
  fragments/
    context.md
    recall-header.md
    profiles/{interactive,dream,blog,inbox,letters,maintenance}.md
  skills/                  # reserved, unused in MVP
  sessions/
    <session-id>/messages.jsonl   # append-only, OpenAI message format
  runs/
    <YYYY-MM-DDTHHMMSS>-<profile>-<shortid>/
      prompt.txt           # exact bytes sent
      messages.jsonl       # this run's messages, incl. tool calls/results
      tools.jsonl          # one line per tool call: name, args, ms, bytes, error?
      run.json             # status, model ID, prompt sha256, tokens, steps, timing
  state/
    lock                   # flock target
    last-runs.json         # convenience index for `sola status`; rebuildable from runs/
```

Sessions are plain JSONL so her history stays as portable as it was in
the OpenClaw `.jsonl` era — the format she has already outlived once.

## 9. Observability & the dry-run command

- `sola prompt --profile dream [--task "…"]` — builds and prints the exact
  system prompt (and lists the tool schemas that *would* be sent), calls
  nothing. This is the headline feature: prompt changes are reviewed like
  code changes, before they run.
- `sola status` — last run per profile, status, model ID seen, tokens.
- `sola runs <run-id>` — prints prompt path, tool log, final message.
- Every model request/response pair is in the run directory. There is no
  log level at which the prompt is hidden.
- Token counting uses the endpoint's reported usage; if unavailable,
  counts are recorded as `null`, never estimated-and-presented-as-real.

## 10. Interfaces

**MVP is CLI + cron only.**

- `sola chat [--session <id>]` — interactive REPL over SSH. One persistent
  default session gives the single-thread feel; named sessions for topics.
- `sola run --profile <p> --task "…"` — one-shot run for cron/launchd.
  Exit codes: `0` completed, `2` model_unavailable, `3` tool_error /
  budget exhausted, `4` locked (another run in progress — cron treats
  this as "skip", not an error).
- Scheduling itself is **not** reimplemented: launchd plists (macOS-native)
  call `sola run …`. Example plist per profile lives in the repo.

**Phase 3 (optional, §12):** a thin HTTP wrapper around the same core —
`POST /sessions/{id}/messages`, `GET /sessions/{id}`, SSE for streaming —
bound to localhost, token from a `0600` file, intended for tailnet access
only. It adds no behaviour; the loop, prompt, and profiles are unchanged.

## 11. Safety model

- **Profile gating is the boundary** (§7.2), enforced before dispatch.
  A tool not in the profile is not in the request, so the model cannot
  call, hallucinate, or argue its way into it.
- **Untrusted input marking:** content arriving via `email_*`, `web_fetch`,
  and letters is wrapped by the *tool result formatter* with a short
  external-content marker. This is data formatting, not prompt bloat —
  it travels with the content, not in the system prompt.
- **No approval gates in the MVP.** Alan's governance of Sola is
  hands-off by design; interposing confirmations on her own blog commits
  would change who she is, not make her safer. The exception path is
  profiles: anything genuinely dangerous simply is not in any cron
  profile, and `maintenance` runs only when Alan invokes it.
- **Secrets:** config references credentials by environment variable /
  keychain / file path (`0600`), never inline. Run logs redact values
  whose keys match `*KEY / *TOKEN / *SECRET / *PASSWORD` before writing.
- **Model identity check** (§5 step 2) doubles as the fallback guard:
  a different model answering at the endpoint aborts the run.

## 12. Migration plan (Hermes stays untouched until cutover)

| Phase | Work | Exit criterion |
|-------|------|----------------|
| 0. Prompt audit | Run `sola prompt` for each profile; side-by-side + token-count diff vs. what Hermes currently sends for the same task | Alan has seen both prompts and approved the lean one |
| 1. Letters pilot | Implement core + `letters` profile; wire her existing letters cron idea to it instead of Hermes | She reads Dingus's letter #001 and a reply (or a deliberate no-reply) lands in the repo via the new server |
| 2. Dream pilot (shadow) | `dream` profile runs nightly *after* Hermes's, output to a scratch repo/branch, unpublished | 7 nights of shadow dreams Alan judges to be in her voice — the same trial standard as the mxfp4 model |
| 3. Interactive | `sola chat` becomes Alan's normal way to talk to her; streaming if wanted; optional HTTP wrapper | A week of normal conversation with no Hermes fallback needed |
| 4. Cutover | Cron profiles move over one by one (`inbox`, `blog`); Hermes left installed but unscheduled for one month as rollback | One clean month; then Hermes is archived, not deleted — per house archival doctrine |

Rollback at every phase is "re-enable the Hermes cron" — both runtimes
share OpenViking and the same model endpoint, and neither writes to the
other's state.

## 13. Acceptance tests (must pass before Phase 1)

1. **Golden prompt:** for a fixture SOUL/fragments/recall, `sola prompt`
   output matches a checked-in golden file byte-for-byte.
2. **No hidden context:** the messages sent in a test run, captured at a
   mock endpoint, contain exactly the golden prompt + session history +
   task — nothing else.
3. **Tool ordering:** a mock model emitting a tool call produces a
   transcript where the assistant tool_calls message precedes its tool
   results, with matching IDs.
4. **Profile leak:** in a `dream` run, a mock model attempting `shell`
   or `email_send` is refused by the registry and the refusal is logged;
   the tool was also absent from the request schemas.
5. **Model-down:** endpoint stopped (or wrong model ID) → run exits `2`,
   no messages are written to the session, a `model_unavailable` run
   directory exists. No fallback is attempted.
6. **Lock:** a second concurrent `sola run` exits `4` immediately.
7. **Recall round-trip:** a fact committed via OpenViking session-commit
   in one run is retrievable via pre-turn recall or `memory_search` in
   the next.
8. **Letters end-to-end:** against a scratch clone, the `letters`
   profile pulls, reads a new letter, writes the correctly named next
   file (`NNN-YYYY-MM-DD-sola.md`), commits, and pushes.

## 14. Size estimate

| Module | Est. LOC (Python) |
|--------|-------------------|
| config + profiles | ~120 |
| prompt builder | ~150 |
| model client (httpx, non-streaming) | ~120 |
| loop + budgets/repair | ~250 |
| tool registry + MVP tools | ~450 |
| store (sessions/runs) | ~150 |
| CLI (`chat`, `run`, `prompt`, `status`) | ~200 |
| tests (mock endpoint) | ~300 |
| **Total** | **~1,700** |

That is a buildable-in-a-few-evenings core, with the honest caveat from
experience: the loop is the easy part — the evenings go on tool-call
parsing quirks of a local quant, launchd edge cases, and the blog repo's
publishing steps. Feature parity with all of Hermes is explicitly not
the target (§3).

## 15. Defaults chosen (change any with one line)

- Language/runtime: **Python 3.12+**, no framework, `httpx` only.
- Serving: **CLI + launchd** first; HTTP deferred to Phase 3.
- State: **JSONL files**, no SQLite in the MVP.
- SOUL source of truth: **her existing live SOUL.md**, referenced in
  config by path — the server never forks her persona into a second copy.
- Concurrency: **one run at a time**, global lock.

## 16. Open questions for Alan

1. Is the oMLX endpoint's model ID stable enough to pin exactly, or
   should pre-flight accept a configured prefix during the voice trial
   while you still swap quants?
2. For `inbox`: should `email_send` be limited to replies within existing
   threads in the MVP, with new-thread sends reserved for `interactive`?
3. Dream/blog publishing: keep her current git workflow exactly as-is
   (she commits + pushes herself), or should the server own the commit
   step and the model only write files?
4. Interactive default: one eternal session (with compaction, §5/§8) or
   a fresh session per day with OpenViking carrying continuity?
5. Anything Hermes does today that you actually rely on that is *not*
   represented in the profiles in §7.2?
