#!/usr/bin/env python3
"""Check owner Stop and reader fork against an existing installed native session."""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target-agent", required=True)
    parser.add_argument("--owner-headers", type=Path, required=True)
    parser.add_argument("--reader-headers", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--expected-stop", default="verified")
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=False)
    owner = json.loads(args.owner_headers.read_text())
    reader = json.loads(args.reader_headers.read_text())
    origin = {
        name: importlib.util.find_spec(name).origin for name in ("omnigent", "omnigent_client")
    }
    artifact = {
        "python": sys.executable,
        "origins": origin,
        "distributions": {
            name: importlib.metadata.version(name)
            for name in ("omnigent", "omnigent-client", "filelock")
        },
        "server": args.server,
        "source": args.source,
        "target_agent": args.target_agent,
    }
    (args.evidence / "artifact.json").write_text(json.dumps(artifact, indent=2))
    for path in origin.values():
        if not path or "site-packages" not in Path(path).parts:
            raise RuntimeError(f"Runtime did not resolve an installed wheel: {path}")

    def request(name, method, path, headers, body=None):
        data = json.dumps(body).encode() if body is not None else None
        action = Request(
            args.server.rstrip("/") + path,
            data=data,
            headers={**headers, "Content-Type": "application/json"},
            method=method,
        )
        try:
            response = urlopen(action, timeout=45)
        except HTTPError as failure:
            response = failure
        with response:
            status = response.status
            payload = json.loads(response.read())
        (args.evidence / f"{name}.json").write_text(
            json.dumps(
                {"method": method, "path": path, "status": status, "response": payload}, indent=2
            )
        )
        return status, payload

    source_path = f"/v1/sessions/{args.source}"
    status, source = request("source", "GET", source_path, owner)
    assert status == 200, source
    assert source["agent_id"] != args.target_agent, "Target must be a different selected agent."
    status, blocked = request(
        "before-stop-fork", "POST", source_path + "/fork", reader, {"agent_id": args.target_agent}
    )
    assert status == 409, blocked
    assert "explicit Stop" in json.dumps(blocked), blocked
    status, denied = request(
        "reader-stop",
        "POST",
        source_path + "/events",
        reader,
        {"type": "stop_session", "data": {}},
    )
    assert status == 403, denied
    status, stopped = request(
        "owner-stop", "POST", source_path + "/events", owner, {"type": "stop_session", "data": {}}
    )
    assert status == 202, stopped
    assert stopped["native_stop"]["outcome"] == args.expected_stop, stopped
    status, snapshot = request("stopped-source", "GET", source_path, reader)
    assert status == 200, snapshot
    assert snapshot["native_stop"]["outcome"] == "verified", snapshot
    status, fork = request(
        "reader-fork", "POST", source_path + "/fork", reader, {"agent_id": args.target_agent}
    )
    assert status == 201, fork
    assert fork["id"] != args.source, fork
    status, destination = request("destination", "GET", f"/v1/sessions/{fork['id']}", reader)
    assert status == 200, destination
    assert destination["agent_id"] == args.target_agent, destination
    (args.evidence / "result.json").write_text(
        json.dumps(
            {"outcome": "passed", "source": args.source, "destination": fork["id"]}, indent=2
        )
    )
    print(f"PASS owner Stop and reader different-agent fork: {args.evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
