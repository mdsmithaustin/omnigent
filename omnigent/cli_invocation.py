"""How to spell an ``omnigent`` command back to the user.

When a deployment wraps the CLI (e.g. ``isaac omni``) it sets
``OMNIGENT_WRAPPER_COMMAND`` and refuses naked ``omnigent`` calls (see the
wrapper guard in :mod:`omnigent.cli`). Followup hints that tell the user to run
a command must then name the wrapper (``isaac omni stop``) rather than the
naked binary (``omnigent stop``), or they suggest exactly the command the guard
rejects. :func:`cli_invocation` centralizes that spelling: hints interpolate it
in place of the leading ``omnigent``/``omni`` token so every one honors the
configured wrapper. Fork launchers also keep their own name in these hints.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from collections.abc import Mapping

from omnigent.fork_releases import is_fork_version
from omnigent.version import VERSION

DEFAULT_CLI_NAME = "omnigent"
WRAPPER_COMMAND_ENV = "OMNIGENT_WRAPPER_COMMAND"


def cli_invocation(*, name: str = DEFAULT_CLI_NAME, env: Mapping[str, str] | None = None) -> str:
    """Return the wrapper or launcher for a follow-up command."""
    if env is None:
        env = os.environ
    wrapper = (env.get(WRAPPER_COMMAND_ENV) or "").strip()
    if wrapper:
        return wrapper
    if name in {"omni", "omnigent"} and sys.argv:
        launcher = sys.argv[0]
        basename = os.path.basename(launcher)
        if basename in {"omni-fork", "omnigent-fork"}:
            if os.path.dirname(launcher) and shutil.which(basename) != os.path.abspath(launcher):
                return shlex.quote(os.path.abspath(launcher))
            return basename
        if (
            basename in {"omni", "omnigent"}
            and os.path.dirname(launcher)
            and is_fork_version(VERSION)
        ):
            return shlex.quote(os.path.abspath(launcher))
    return name
