# Solakit journal pack

An independently installable package, outside `src/solakit`. It may be moved to a
separate repository unchanged. Provides `journal.create`, `journal.list_recent`
and `journal.publish`; dreams, poems and posts differ only by configuration.
No core imports this module, and core installations do not install it.

For local development from a Solakit checkout:

```sh
uv sync --frozen --all-packages
uv run pytest packs/journal/tests -q
uv run sola check
uv run sola serve
```

The checkout's workspace lock manages core and all packs together. Outside this
workspace, include the journal distribution alongside Solakit in the deployment
environment. Core wheel metadata has no dependency on journal or another pack.

```toml
enabled_tools = ["solakit.tools.files", "solakit.tools.repo_git", "sola_packs.journal"]

[repositories]
site = "/absolute/path/to/site"

[collections.dreams]
repository = "site"
directory = "_dreams"
filename = "{date}-{slug}.md"
# Optional literal format string; omitted uses the pack's front matter default.
# layout = "---\ntitle: {title}\ndate: {date}\n---\n\n{body}\n"

[profiles.dream]
tools = ["journal.create", "journal.list_recent", "journal.publish"]
workspace = ["collection:dreams"]
write = "workspace"
effects = ["push"]
```

Create accepts a lowercase hyphenated slug, title, body and optional `YYYY-MM-DD`
date (default: configured timezone). It never overwrites. Layout placeholders:
`date`, `slug`, `title`, `body`; `title` is JSON-quoted for safe YAML front matter.
Filename placeholders: `date`, `slug`, one flat file name only. Listing is ordered
by file modification time. Publish takes collection-relative filenames plus a
commit message, stages only those entries and publishes one commit.

Configure an existing clean Git worktree with an upstream branch and Git identity.
Publishing requires collection write access and `git.push`. It refuses a staged
index, pre-existing unpublished commits or an upstream already known to be ahead.
On push failure, the local commit remains and the tool reports its identity;
inspect the remote before manually reconciling/retrying. There is no force-push,
implicit rebase, auto-retry or rollback of committed evidence.

Packs are trusted Python code selected by operator config, not a sandbox for
untrusted package code. Profile grants control model-visible calls and the shared
services enforce their actual filesystem/repository arguments. No production
publishing or job activation is part of the included scratch tests.
