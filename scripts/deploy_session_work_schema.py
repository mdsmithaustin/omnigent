"""Deploy the session work schema using a URI supplied through the environment."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from dataclasses import asdict

from omnigent_session_work_failure import (
    DEPLOYMENT_ERROR_MESSAGE,
    ExpectedSchemaFailure,
    SessionWorkFailureCapture,
    _combine_failures,
)


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
    invocation = SessionWorkFailureCapture("cli")
    streams = SessionWorkFailureCapture("cli")
    with streams:
        try:
            with (
                open(os.devnull, "w") as diagnostics,
                contextlib.redirect_stdout(diagnostics),
                contextlib.redirect_stderr(diagnostics),
                invocation,
            ):
                from omnigent.db.utils import deploy_session_work_schema

                result = deploy_session_work_schema(uri, role=args.role, target=args.target)
        except OSError:
            raise ExpectedSchemaFailure(DEPLOYMENT_ERROR_MESSAGE) from None
    failure = _combine_failures(invocation.failure, streams.failure)
    if failure is not None:
        payload: dict[str, object] = {"error": failure.message}
        if failure.diagnostics:
            payload["diagnostics"] = [asdict(item) for item in failure.diagnostics]
        print(json.dumps(payload), file=sys.stderr)
        return 1
    print(json.dumps(asdict(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
