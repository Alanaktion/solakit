# Ideas

- [ ] Multiple assistants with OpenViking account isolation

- [ ] Expand functionality of wakes/time-based triggers:

> On your flexibility question: it's a partial yes, and the split matters. What's genuinely extensible:
> 
> New time-based triggers are cheap. The schedule grammar is just a string parsed in one place (once: / cron:), occurrence identity is a hash of job + generation + instant, and everything executes through the ordinary run loop. An interval schedule or another cron dialect is a small branch.
> New delivery targets slot in cleanly. Delivery is a separate state machine (pending → sending → delivered, with dedup, cooldown, expiry) — an external notification adapter would plug in there without touching execution.
> The tick loop is the right seam for polling. The 1-second APScheduler wake evaluates whatever's due; a condition evaluator could live there.
> 
> What would need redesign, not extension:
> 
> Event/condition triggers (my hooks). There's no condition store, no event bus, no evaluator. A hook-style "wake me when X changes" needs condition definitions on the Job record, an evaluator, and occurrence identity keyed on events rather than instants — a new subsystem, not a new schedule string.
> Inbound-message wakes. The only out-of-band entry is manual(), which needs an interactive caller and a request ID. There's no ingress path for mail, chat, or device events arriving.
> Chaining. This is the big one: Muse chains wakes constantly, but Phase 9 forbids it by design — deny_autonomous_successor() runs on every schedule mutation and tool call, via a contextvar that survives relabeling tricks, and there's a test proving even a relabeled interactive profile can't slip past it. Supporting chains would mean redesigning the autonomy model (e.g. successors only to a declared parent), not adding a trigger type.
> External delivery. Local topic publication only; the contract is explicit that no notification provider exists in v1.

- [x] Jobs disabled vs pause/resume is stupid, just have one state toggle.
- [x] Allow manually triggering a job from web UI/API even if it's turned off.

## V1-V2 Gaps

The biggest gaps are **Git-backed letters, general-purpose journals, Discord, and advanced scheduled jobs**. Blog posts and dream poems already have built-in v2 tools. I compared the local checkouts at v1 `fd2086b` and v2 `4b47a7a`.

### Features still missing

| Area | What v1 had that v2 lacks |
| --- | --- |
| **Letters and journals** | `letters.*` pulled, read, replied to, and sent numbered letters through Git. `journal.*` managed configurable Markdown collections with their own filenames, frontmatter, and selective publication. The older porting plan calls out both workflows. [Letters pack](../solakit/packs/letters/README.md) · [Journal pack](../solakit/packs/journal/README.md) · [V2 porting plan](docs/plans/NEXT-TOOLS-JOURNAL-LETTERS.md) |
| **Integrations and utility tools** | Email, calendar reads, thermal printing, web search and page reading, feed checks, GitHub and wiki lookups, date/time and math tools, generic Git operations, and shell execution. [V1 tool catalog](../solakit/docs/tools.md) · [V2 built-ins](src/solakit/tools/builtins.py:211) |
| **Discord and external delivery** | Discord conversations and thread routing, inbound image attachments, agent-initiated channel posts, and scheduled delivery of replies or files to a channel. V2 scheduled delivery stays in local topics. [V1 channels](../solakit/docs/operations.md:463) · [V2 delivery](docs/contracts/jobs.md:137) |
| **Agent workflow tools** | Sub-agent delegation and follow-ups, reusable playbooks, run plans, persistent topic-local key/value notes, and opt-in tool discovery with deferred schemas and pinned tools. V2 ships eager schemas. [V1 delegation](../solakit/docs/operations/delegation.md) · [V1 tool catalog](../solakit/docs/tools.md) · [V2 disclosure decision](docs/decisions.md:32) |

### Workflows with partial parity

- **Scheduling.** V2 has cron, one-shots, run-now, and fresh ephemeral topics. V1 also had fresh child topics that retained shared memory, task placeholders such as `{date}`, polling-specific catch-up controls, schema-validated JSON output with template rendering, and `then` chains. Chaining conflicts with v2’s explicit rule against autonomous work creating further wakes. [V1 job management](../solakit/docs/operations/jobs.md) · [V2 job contract](docs/contracts/jobs.md)
- **Files, images, and publishing.** V2 has guarded file reads and writes, literal text search, one fixed ComfyUI graph, and blog/dream publishing for one Astro checkout. V1 also had dedicated append and targeted edit, recursive glob find and regex grep with context, multiple named image workflows, a separate image-result lookup, and the option to save a dream for later publication. V2 can feed generated images from the same topic back to the model. [V1 file tools](../solakit/docs/operations/read-tools.md:116) · [V1 image pack](../solakit/packs/image/README.md) · [V1 blog pack](../solakit/packs/blog/README.md) · [V2 images](docs/image-workflows.md) · [V2 blog and dreams](docs/blog.md)
- **Topic and memory administration.** V2 has automatic compaction, individual archive and restore, and a bulk archive API. V1 also had manual compaction, transcript rotation that started fresh context in the same topic, an inactivity cleanup screen with preview and bulk undo, and per-profile OpenViking users and roots. [V1 session administration](../solakit/docs/operations/session-archive.md) · [V1 memory controls](../solakit/docs/operations.md:366) · [V2 session API](src/solakit/api/routes.py:473)
- **Operator tooling.** V1 exposed `sola init`, `doctor`, `memory-test`, `eval`, `config diff`, and `tools residents` as CLI commands. V2 has `check --explain`, health checks, and developer probes and eval scripts. V1 also exposed per-profile sampling, thinking modes, and reasoning budgets. [V1 CLI](../solakit/src/solakit/cli.py:26) · [V1 model tuning](../solakit/docs/operations/model-tuning.md) · [V2 CLI](docs/cli.md)
