# Built-in shell tools

Load `shell` or `solakit.tools.shell` in core `enabled_tools`. No optional pack,
common support package, sidecar or extra dependency is required. Process execution
lives in the generic `solakit.services.process` service.

- `shell.exec(argv, directory=".", timeout_s=30)` executes literal argv without a
  shell (pipes, redirects, globs and `~` are not interpreted).
- `shell.run(command, directory=".", timeout_s=30)` executes `/bin/sh -c`,
  supporting pipelines, redirection and ordinary shell scripting.

`directory` defaults to `.`, which resolves against `[server].cwd` (or the server
process directory) and must still be inside write roots. Prefer granting only
`shell.run` (the recommended `ops` toolset does); grant `shell.exec` where literal
argv safety matters.

Access is controlled by profile tool/effect and working-directory grants.
Any profile may receive these grants; no profile name is hard-coded:

```toml
enabled_tools = ["shell"]

[profiles.interactive]
tools = ["shell.exec", "shell.run"]
workspace = ["path:/absolute/working/root"]
write = "workspace"
effects = ["shell"]
tool_timeout_s = 120
```

The working directory must be inside explicit write roots. **Commands themselves
run with the host user's privileges; the cwd check is not a filesystem sandbox.**
Choose which profiles receive shell access in configuration; there is no
hard-coded profile-name restriction. These tools do not introduce per-command
approval dialogs.

Timeout is capped at 300 seconds and the shorter profile budget. Both output
streams are bounded and drained; timeout/cancellation kills the process group and
waits for cleanup. Exit code, stderr, truncation flags and timeout state are
reported. Standard input is closed. Ordinary children that keep output pipes open
remain inside the timeout; intentionally detached host processes are not an OS
sandbox guarantee. Timed-out commands may already have made changes—inspect before
retrying. Nonzero exit is reported as a tool error, not a successful action.

The child receives a small environment (PATH/HOME/locale/TMPDIR/TERM), not inherited
model/memory/mail API variables. This is credential hygiene, not isolation from
files accessible to the host user. There is no interactive TTY or background job
manager. Tests run harmless scratch commands and verify literal argv, pipelines,
nonzero exits, output bounds, timeouts/cancellation and profile/cwd denials.

## Migration from the former pack

Replace `sola_packs.shell` with `shell` (or `solakit.tools.shell`) in enabled_tools,
and remove `solakit-packs-shell` from deployment dependencies. Tool names,
effect/write-tier grants and arguments are unchanged. The former maintenance-only
restriction is removed; profile tool grants now determine access. The
retired pack is no longer maintained; there is no core dependency on it.

Process output caps count decoded Unicode characters independently for stdout and
stderr. UTF-8 decoding spans read boundaries, so truncation never splits a valid
multibyte character. The `stdout_complete` and `stderr_complete` flags report
whether the entire stream fit. Caller-provided environment mappings are copied
before the service supplies its default PATH.
