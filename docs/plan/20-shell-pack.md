# General shell tools — moved to core

Status: implemented as built-in tools; supersedes the original separate pack.

`shell` / `solakit.tools.shell` provides literal `shell.exec` argv and `/bin/sh` `shell.run`,
enabled by config at startup and call time. An explicit shell.exec capability and
filesystem write roots authorize the working directory. This grants host-user
command execution, not a filesystem sandbox; only explicitly configured profile grants authorize it. No per-command approval framework is introduced.

Output streams are bounded/drained. Requested timeout is capped by 300 seconds
and the profile budget; timeout/cancellation kills and waits for the process group.
Exit, truncation and timeout states are visible, with nonzero status as tool error.
Environment inheritance is reduced to basic path/home/locale variables; this is
credential hygiene, not isolation from host-user-readable files.

Scratch tests cover literal argv, pipelines, nonzero exit/stderr, path/capability
and profile denials, environment hygiene, large output and descendant cleanup
on timeout/cancellation. No production shell workflow or live job was activated.

The implementation and its regression tests now ship in the core distribution,
using `solakit.services.process` with no optional-pack imports. The retired shell
package and pack-owned process helper are removed. Existing deployments switch
module name and remove the optional shell dependency; tool names/grants are stable.
See [migration](../operations/shell.md).
