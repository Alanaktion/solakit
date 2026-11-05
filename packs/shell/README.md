# Low-level shell pack

`solakit-packs-shell`, loaded as `sola_packs.shell`; install alongside core and
`solakit-packs-common`. No sidecar configuration is required.

- `shell.exec(argv, directory, timeout_s=30)` executes literal argv without a shell.
- `shell.run(command, directory, timeout_s=30)` executes `/bin/sh -c`, supporting
  pipelines, redirection and ordinary shell scripting.

Both tools are **maintenance-only**, including startup/profile validation and
call-time checks. Enable them deliberately:

```toml
enabled_tools = ["sola_packs.shell"]

[profiles.maintenance]
tools = ["shell.exec", "shell.run"]
write_scopes = ["path:/absolute/working/root"]
capabilities = ["shell.exec"]
tool_timeout_s = 120
```

The working directory must be inside explicit write roots. **Commands themselves
run with the host user's privileges; the cwd check is not a filesystem sandbox.**
Use this for operator maintenance, not inbox, journal, letters or untrusted-input
profiles. This pack does not introduce per-command approval dialogs.

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
