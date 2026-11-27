# Blog posts and dream poems

Sola can read and create content for the existing Astro blog without agent shell
access. The optional built-in service follows the checkout's content schema and
runs fixed `npm run build`, Git staging/commit, and push commands.

Configure the operator-owned TOML:

```toml
[blog]
repository = "~/workspace/blog"

[defaults]
workspace_roots = ["~/workspace/blog"]
tools = ["blog.*", "dreams.*"]

[defaults.budgets]
tool_timeout_seconds = 300
```

Merge these values into existing configuration; preserve other roots and grants.
The checkout must already exist and have dependencies installed, `git` and `npm`
on the server's PATH, Git author identity, an `origin` remote, and working push
credentials. The service pushes `HEAD` to `origin/main`. Installing or configuring
the service grants no tool authority. No live credentials belong in tool arguments.

## Tools

| Tool | Arguments and behavior |
| --- | --- |
| `blog.list` | `limit=20` (1–100), `offset=0`; newest content dates first, slug descending for ties. Includes drafts. Returns `entries` and `next_offset`. |
| `blog.read` | `slug`; returns metadata and the full Markdown body. |
| `blog.create` | Required `slug`, `title`, `description`, `body`; optional `tags=[]`, `date`, `cover_path`, `cover_artifact_id`, `auto_publish=false`. Never overwrites. |
| `blog.publish` | `slug`; sets only this blog's `draft` to false and publishes pending content. |
| `dreams.list` | Same pagination as blogs; lists existing dreams, including legacy suffix filenames. |
| `dreams.read` | `slug`; returns metadata and full poem body. |
| `dreams.create` | `body`, optional `date`; creates `YYYY-MM-DD.md`, then always publishes. No publish flag or separate dream-publish tool. |

Slugs contain lowercase letters/numbers separated by hyphens, up to 128 characters.
Dates accept ISO dates or timestamps. Dates without offsets use the configured
background timezone; timestamps with offsets are converted to that timezone.
Omitted dates use the current local time. Dream filenames use the resulting local
date. New poems require 3–15 nonempty lines and have date-only frontmatter.
Bodies are Markdown, without a separate frontmatter block. Existing content is
read as stored, including older poems that do not meet the new creation limits.

A blog without `auto_publish` is saved with `draft: true`, without a build, commit,
or push. Setting `auto_publish=true` creates it with `draft: false` and publishes.
The `blog.create` grant therefore authorizes optional publication; the
`dreams.create` grant always authorizes publication. Give read-only profiles only
the list/read tools. No generic file-write or shell grant is needed.

Schemas remain eager. Grant only the tools a profile needs; broad grants consume
more context, particularly alongside image attachments. Existing context checks
still fail explicitly when the configured window is too small.

Covers are optional PNGs: provide either a granted local `cover_path` or the
artifact ID returned by image generation. Generated covers must be linked by
verified receipt evidence in the same session, or the same run for ephemeral
work. PNGs use the existing image validator, 16 MiB limit and 64–2048-pixel bounds.
The service copies to `src/assets/blog-images/<slug>-cover.png` without overwriting
and sets the required relative `coverImage` path. It does not generate images.

## Publication and recovery

Publication builds the current checkout, commits pending Markdown in
`src/content/blog/` and `src/content/dreams/` plus files in
`src/assets/blog-images/`, then pushes. Other blogs retain their existing draft
flags, so committing them does not make them visible. Unrelated working-tree and
staged edits are left out of the new content commit.

**Every push includes all existing pending commits, including unrelated source
commits.** This is the operator-selected policy. The service does not restrict
pending commits, pull, merge, rebase, force-push, or retry automatically. An empty
content commit is skipped, but pending commits are still pushed. A successful
result includes the commit hash and confirms push completion; GitHub Pages build
and deployment happen separately.

Failure preserves local files and commits. Recoverable errors name the phase,
content path, and last known commit when available. A failed/unconfirmed Git
mutation can have an uncertain receipt; inspect before retrying. Cancellation
and timeout kill the owned subprocess group and retain normal runtime evidence.
Interrupted work is never automatically replayed. Builds validate the working
checkout, so unrelated uncommitted source edits can affect the build result.

Existing dates always reject dream creation, including identical retries after
failed publication. **Dream recovery is manual:** fix the build/push and publish
the preserved work outside these tools. Blog publication can be retried with
`blog.publish`. Existing posts, poems, and cover images are never silently replaced
by creation.

Calls use frozen/current tool grants, workspace roots, protected paths, run budgets
and effect receipts. Background writes require explicit job roots containing the
checkout. Existing consequential-tool quiet hours also apply to these tools,
including draft creation; the scheduler may defer the whole run. Tools create no
wake-ups. One lock serializes service calls; concurrent operator Git operations
still require normal coordination.

Listings scan at most 2,000 directory entries and 16 MiB of Markdown, with the
existing 1 MiB per-file bound. Model-facing results retain dispatcher truncation
and artifact behavior.
