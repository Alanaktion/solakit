# Journal pack: collections

Status: independently packaged and locally scratch-tested.

`packs/journal` builds `solakit-packs-journal`, exporting `sola_packs.journal`.
Core installation does not install it. One implementation supplies
`journal.create`, `journal.list_recent`, and `journal.publish`, parameterized by
collection alias. Existing repository aliases remain authoritative.

Create validates canonical dates/slugs, formats the configured layout and creates
exclusively. List uses bounded metadata, ordered by modification time. Publish
commits only named matching entry files through the scoped Git service and requires
`git.push`. No shell primitive appears in the model transcript.

Exit checks passed with a local bare remote: two collections, identical code,
no overwrite, correctly quoted front matter, cross-collection traversal denial,
capability denial, and one named-file commit/push. Existing live memory and
profile/pilot acceptance gates remain pending. No production jobs enabled.
