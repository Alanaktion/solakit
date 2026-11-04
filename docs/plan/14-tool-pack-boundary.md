# Tool-pack boundary and shared services

Status: locally implemented. Prerequisite for phase 9 domain work.

1. Dotted-path loading: `solakit.tools.files` and `sola_packs.journal` are ordinary
   explicit Python module imports; bare `files` remains built-in shorthand. No
   entry-point discovery or auto-enabling. Imported packs are operator-trusted code.
2. `ToolContext` exposes Python filesystem/Git services. Built-in tools are thin
   wrappers; packs compose services within one tool call. No domain imports or
   branches in core execution. Registry dispatch supplies read-only runtime state
   through a composition-root callback, with no loop changes.
3. Named collection config references existing repository aliases. Collection
   scopes resolve only to their configured subdirectory, including symlink checks.
   Filename/layout placeholders are validated; default front matter lives in the
   journal pack, not the core. No special blog/dream/poem config classes.

Verification: existing regression suite, external temporary-package import,
shared-service permission tests, and separately installed journal contract tests.
Core wheel/source distribution exclude packs; the pack has its own wheel and core
API dependency. Moving `packs/journal` to another checkout requires no core edit.
