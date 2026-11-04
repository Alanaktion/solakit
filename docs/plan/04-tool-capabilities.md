# Phase 4: Tool registry and enforced capabilities

Status: complete

Depends on: phase 3

- Add configured module discovery, the decorator API, Pydantic argument schemas, `ToolContext`, and structured results/errors.
- Validate profile membership and declared write scopes at startup and again before dispatch.
- Enforce actual filesystem/repository access inside generic tools; manifest validation alone cannot constrain arbitrary paths.
- Implement timeouts, explicit output truncation, call logging, and nonblocking execution.
- Start with fixture tools and narrowly scoped file/repository primitives.

**Exit check:** Forbidden calls never execute; invalid scope grants prevent startup; a fixture module becomes available through configuration alone.

## Completion record

Implemented module discovery, decorator manifests, strict Pydantic arguments,
ToolContext, startup/dispatch grants, separate capabilities and filesystem roots,
timeouts, truncation, redacted logs, and nonblocking execution. Added bounded
files.read, create-only files.write, and named repo_git.status primitives.
Prompt inspection now exposes registry schemas.
Validated with 15 passing tests including drop-in discovery, denied execution,
startup rejection, path traversal/symlink refusal, timeout and error handling;
Ruff passes. Trusted plugin and worker timeout limitations are documented.
