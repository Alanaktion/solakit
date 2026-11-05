# Solakit 🌙

Solakit is Sola’s small, single-operator personal assistant runtime. It connects
one pinned OpenAI-compatible local model to operator-owned prompts, external
OpenViking memory, and explicitly granted tools.

The moonlight-themed web UI provides streaming chat, session history, prompt and
run inspection, conversation archival and compaction, and scheduled job management.
Optional Discord channels share the same session execution. Tool packs add journal
publishing, letters, email, thermal printing, calendar reads, and image workflows.
Discord image attachments also pass through to a vision-capable model; see
[vision input](docs/operations/vision.md) for setup, limits and live verification.

Solakit runs on localhost or a tailnet with Python 3.12+ and uv. Prompts and
configuration stay under operator control; portable session and run evidence stays
in a private data directory. Integrations are opt-in.

- [Setup and configuration](docs/setup.md): installation, server settings, authentication, UI, and diagnostics.
- [Runtime operations](docs/operations.md): API, prompts, storage and recovery, search, memory, channels, and tool grants.
- [Scheduled jobs](docs/operations/jobs.md) and [shell tools](docs/operations/shell.md).
- [Optional tool packs](packs/README.md) and their resource configuration.
- [Configuration example](config.example.toml), [behavioral specification](SPEC.md), and [contributor guide](AGENTS.md).
