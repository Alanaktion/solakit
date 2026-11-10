# Solakit 🌙

Solakit is Sola’s small, single-operator personal assistant runtime. It connects
one pinned OpenAI-compatible local model to operator-owned prompts, external
OpenViking memory, and explicitly granted tools.

The moonlight-themed web UI provides streaming chat, session history, prompt and
run inspection, conversation archival and compaction, and scheduled job management. Run inspection uses bounded catalog pages and
indexed reads across active and archived evidence.
Optional Discord channels share the same session execution. Tool packs add journal
publishing, Astro blog posts and dreams, letters, email, thermal printing, calendar
reads, and image workflows.
Discord image attachments also pass through to a vision-capable model; see
[vision input](docs/operations/vision.md) for setup, limits and live verification.

Solakit runs on localhost or a tailnet with Python 3.12+ and uv. Prompts and
configuration stay under operator control; optional `fragments/memory.md` supplies
a curated front page on every run, even with OpenViking disabled; portable session and run evidence stays
in a private data directory. Integrations are opt-in. Set `[server].cwd` to choose
the base directory for relative tool paths such as `files.list(".")`; profile
filesystem grants still apply.

- [Setup and configuration](docs/setup.md): installation, server settings, authentication, UI, and diagnostics.
- [Runtime operations](docs/operations.md): API, prompts, storage and recovery, search, memory, channels, and tool grants.
- [Recall direction and memory notes](docs/operations/memory-direction.md).
- [Delegated sub-agents](docs/operations/delegation.md).
- [Conditional tool disclosure](docs/operations/tool-disclosure.md): optional deferred schemas, with eager defaults.
- [Scheduled jobs](docs/operations/jobs.md) and [shell tools](docs/operations/shell.md).
- [Session archival](docs/operations/session-archive.md): reversible cleanup of imported history, activity filters and bulk restore.
- [Web, reference and calculation tools](docs/operations/read-tools.md).
- [Model tuning, `sola doctor` and `sola eval`](docs/operations/model-tuning.md):
  sampling and thinking, reasoning evidence, live probes and the real-model
  evaluation harness ([evals/](evals/README.md)). `sola init` scaffolds a new site.
- [Architecture](docs/architecture.md) and the generated [tool reference](docs/tools.md).
- [Optional tool packs](packs/README.md) and their resource configuration.
- [Configuration example](config.example.toml), [behavioral specification](SPEC.md), and [contributor guide](AGENTS.md).

Scheduled jobs can opt into `sub_session = true` for fresh child sessions with
shared memory and final-only channel delivery. Configure this in the job form,
API or chat tools; see [job management](docs/operations/jobs.md).
