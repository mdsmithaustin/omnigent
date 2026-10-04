"""Deploy the session work schema using a URI supplied through the environment."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from dataclasses import asdict


class DeploymentParser(argparse.ArgumentParser):
    def error(self, message):
        del message
        self.exit(2, '{"error": "Invalid deployment arguments."}\n')


def main() -> int:
    parser = DeploymentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=("split-ap", "shared"))
    parser.add_argument("--target", required=True, choices=("mm1a2b3c4d5e",))
    parser.add_argument("--uri-env", required=True)
    args = parser.parse_args()
    uri = os.environ.get(args.uri_env)
    if not uri:
        print('{"error": "Database URI environment variable is missing."}', file=sys.stderr)
        return 2
    try:
        # Driver and Alembic diagnostics can include connection credentials.
        with (
            open(os.devnull, "w") as diagnostics,
            contextlib.redirect_stdout(diagnostics),
            contextlib.redirect_stderr(diagnostics),
        ):
            from omnigent.db.utils import (
                SessionWorkDeploymentError,
                deploy_session_work_schema,
            )

            try:
                result = deploy_session_work_schema(uri, role=args.role, target=args.target)
            except SessionWorkDeploymentError as exc:
                error = str(exc)
            else:
                error = None
    except Exception:  # noqa: BLE001 - the operator must not print raw diagnostics
        error = "Database schema deployment failed."
    if error is not None:
        print(json.dumps({"error": error}), file=sys.stderr)
        return 1
    print(json.dumps(asdict(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
