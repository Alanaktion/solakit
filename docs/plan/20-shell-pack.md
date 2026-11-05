# General shell pack

Status: locally implemented as the separately authorized low-level pack.

`sola_packs.shell` provides literal `shell.exec` argv and `/bin/sh` `shell.run`,
maintenance-only at startup and call time. An explicit shell.exec capability and
filesystem write roots authorize the working directory. This grants host-user
command execution, not a filesystem sandbox; it must not leak into untrusted
inbox/journal/letters profiles. No per-command approval framework is introduced.

Output streams are bounded/drained. Requested timeout is capped by 300 seconds
and the profile budget; timeout/cancellation kills and waits for the process group.
Exit, truncation and timeout states are visible, with nonzero status as tool error.
Environment inheritance is reduced to basic path/home/locale variables; this is
credential hygiene, not isolation from host-user-readable files.

Scratch tests cover literal argv, pipelines, nonzero exit/stderr, path/capability
and profile denials, environment hygiene, large output and descendant cleanup
on timeout/cancellation. No production shell workflow or live job was activated.
