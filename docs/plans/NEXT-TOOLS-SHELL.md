# Literal command execution

Add one interactive escape hatch for operator-requested local commands. Use
the [shared tool plan](README.md#follow-up-tool-plans). Domain tools remain
the direct route for files, Git, correspondence, and search.

## Tool call

`shell.exec(argv, cwd=None)` runs one program with literal arguments. `argv`
includes the executable. Omitted `cwd` uses the first permitted workspace
root. A relative `cwd` is resolved under that root. Return `exit_code`,
`stdout`, `stderr` and truncation flags on success. A known nonzero exit is a
failed tool outcome with the exit code and a short redacted diagnostic. Describe
the call as “Run a local program with literal arguments in a granted directory.”
No implicit shell expansion, stdin, TTY, background invocation or per-call
environment and timeout options.

## Implementation

- Require `[shell] enabled = true`, an explicit `shell.exec` profile grant,
  and interactive origin. A wildcard alone must not enable this tool. Preserve
  the explicit grant through profile inheritance and admission checks at both
  schema disclosure and dispatch. Use the existing tool and run time budgets.
- Verify that `cwd` is an existing canonical directory inside the admitted
  and current roots, with no symlink traversal or managed path access. Treat
  the directory check as a launch condition. The executable runs with the
  server account's filesystem and network privileges. Document that explicitly
  in configuration. Use OS isolation for deployments that need a narrower
  execution boundary.
- Spawn with `asyncio.create_subprocess_exec`, a small environment with a
  trusted PATH and required locale/temp settings, and closed stdin. Bound argv
  size, drain both streams concurrently with byte caps, and close and reap the
  owned process group on timeout or cancellation. Share process lifecycle code
  with Git only if both use the same behavior. Keep environment and authority
  policy at the callers. Ordinary children are managed through the group.
- Record the durable intent before launch. Confirm the command's exit when
  observed. This does not verify any individual file write or network effect.
  After timeout or cancellation following launch, mark the operation uncertain
  and advise inspecting the workspace before another attempt. A spawn failure
  is distinguishable from an interrupted process. Never automatically retry.

## Acceptance

Run harmless fixture commands that exercise literal metacharacters, working
directories, environment redaction, output caps, exit failures, and long-lived
children. Verify denial with missing opt-in, wildcard-only grants, revoked
grants, scheduled origin, and symlinked or protected directories. Cancel a
running fixture and check that its process group was reaped and the receipt
retains the correct uncertainty. Run a model task that uses one command to
inspect a disposable project, then reports the observed result.
