# Solakit blog pack

Astro Markdown posts and dreams, with create-only writing and one-call publishing.
Load `sola_packs.blog` explicitly. Core does not depend on this distribution.

```toml
enabled_tools = ["sola_packs.blog"]

[repositories]
site = "/absolute/path/to/blog"

[profiles.writer]
tools = ["blog.*"]
workspace = ["repo:site"]
write = "workspace"
effects = ["push"]
# resources = ["image:studio"] # Optional completed image-pack covers.
# tool_timeout_s = 300 # Per-stage Git/build bound, also subject to 600s total.
```

`blog.list_posts(repository, limit=10, offset=0)` and `blog.list_dreams` list
metadata by content date, newest first, including drafts. `blog.read_post` takes
a slug; `blog.read_dream` takes a YYYY-MM-DD date. Reads preserve literal evidence
and label it as external content. All paths are fixed beneath the named repository.

`blog.write_post(repository, operation_id, slug, title, description, body,
date=None, tags=[], draft=False, cover=None, publish=True)` writes
`src/content/blog/<slug>.md`. `blog.write_dream(repository, operation_id, body,
date=None, align=None, publish=True)` writes `src/content/dreams/YYYY-MM-DD.md`.
Dreams have only a date, body and optional left/center/right alignment; omission
uses the site's center default. One dream per date; existing files are never
overwritten. Dates default to now in the operator timezone. Explicit dates accept
YYYY-MM-DD or an ISO timestamp with a timezone offset. Body text is preserved.

By default a write runs `npm run build`, commits exactly the new entry and cover,
and pushes to the configured upstream. Requires whole-repository write reach and
the push effect. Node/npm and the site's dependencies must already be installed;
this tool does not install them. Repository build scripts are operator-trusted
code running with host-user privileges. No separate shell effect is required for
this fixed pack workflow. It requires a clean worktree/index and no known upstream
divergence. No pulls, force pushes, rebases or automatic push retries occur.

Set `publish=false` to save without building/committing/pushing. This needs only
write reach to the target directories. Later call
`blog.publish(repository, operation_id, kind="post"|"dream", identifier=...)`.
Only that entry and its local cover may be dirty. Publishing a draft still pushes
its `draft: true` source; visibility remains controlled by Astro.

Stable operation IDs are mandatory. Private append-only receipts under
`pack-operations/blog/` retain paths, build outcomes and commit identities.
Identical completed calls return the original result. Pending, interrupted, failed
or uncertain calls never automatically resume or publish again. Build failures
leave local files for inspection; publish them later with a deliberate new
`blog.publish` operation. Push failures retain the commit and require inspecting
the remote before manual reconciliation. Back up receipts with the evidence tree.
A successful push is not a claim that GitHub Actions deployment has completed.

For optional covers, install `solakit-packs-blog[image]` (the workspace installs
all packs). Generate with `image.generate`, inspect with `image.result`, then pass
`cover={"workflow":"studio","operation_id":"cover-1","image_index":0}` to
`blog.write_post`. Grant `image:studio` and configure `[images.studio]` in
`SOLA_PACKS_CONFIG`; see the image pack README. The blog writer does not generate
or wait for images. It verifies the completed operation and downloads its selected
output from the original configured endpoint without redirects/retries. Downloads
and normalized PNGs are limited to 20 MB and images to 40 megapixels.

Covers are created at `src/assets/blog-images/<slug>-cover.png` and frontmatter
uses `../../assets/blog-images/<slug>-cover.png`. Existing covers can be published
later only through safe relative references into that asset directory. Dreams
never accept covers. Without covers the image distribution is not required.

For cover generation, follow the site's guidance: dark muted palette with purple
`#a78bfa`, atmospheric imagery, 16:9 or 2:1 composition, and no text. No writing
style or poem-length rule is enforced by the pack.
