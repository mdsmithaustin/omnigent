from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BUILDER = Path(__file__).resolve().parents[2] / "scripts/build_fork_release.sh"


@pytest.mark.parametrize("acceptance_exit", [0, 7])
def test_wheel_coexistence_is_required_before_build_completion(tmp_path, acceptance_exit):
    checkout = tmp_path / "checkout"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    shutil.copy(BUILDER, scripts / BUILDER.name)
    (checkout / ".github").mkdir()
    (checkout / ".github/fork-release.json").write_text(
        json.dumps({"version": "0.16.1+mdsmithaustin.2", "upstream": {"tag": "v0.16.1"}})
    )
    (scripts / "fork_release.py").write_text(
        "import json\n"
        "def inventory_ui(root): return {}\n"
        "def write_json(path, value): path.write_text(json.dumps(value))\n"
        "def inspect_artifacts(*args): pass\n"
    )
    (scripts / "normalize_uv_lock_registry.py").touch()
    (scripts / "verify_fork_coexistence.py").write_text(
        "import argparse, json, os\n"
        "from pathlib import Path\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--upstream-python', required=True)\n"
        "p.add_argument('--fork-python', required=True)\n"
        "p.add_argument('--evidence', type=Path, required=True)\n"
        "p.add_argument('--require-ui', action='store_true')\n"
        "args = p.parse_args()\n"
        "args.evidence.mkdir(parents=True, exist_ok=False)\n"
        "receipt = vars(args) | {'invoked_python': os.environ['INVOKED_PYTHON']}\n"
        "(args.evidence / 'receipt.json').write_text(json.dumps(receipt, default=str))\n"
        "raise SystemExit(int(os.environ['ACCEPTANCE_EXIT']))\n"
    )
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "python").symlink_to(sys.executable)
    for name in ("pnpm", "uvx"):
        path = tools / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    uv = tools / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, tarfile\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['UV_LOG'], 'a') as log:\n"
        "    log.write(json.dumps(args) + '\\n')\n"
        "if args[0] == 'venv':\n"
        "    python = Path(args[-1]) / 'bin/python'\n"
        "    python.parent.mkdir(parents=True)\n"
        "    python.write_text('#!' + sys.executable + '\\n'\n"
        "        'import os, sys\\n'\n"
        "        'if any(a.endswith(\"/smoke.py\") for a in sys.argv[1:]): sys.exit(0)\\n'\n"
        "        'os.environ[\"INVOKED_PYTHON\"] = sys.argv[0]\\n'\n"
        "        'os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\\n')\n"
        "    python.chmod(0o755)\n"
        "elif args[0] == 'build':\n"
        "    dist = Path(args[args.index('--out-dir') + 1])\n"
        "    (dist / 'fork.whl').touch()\n"
        "    project = Path(os.environ['FIXTURE_PROJECT'])\n"
        "    with tarfile.open(dist / 'fork.tar.gz', 'w:gz') as archive:\n"
        "        archive.add(project, arcname='fork')\n"
        "elif '--outdir' in args:\n"
        "    (Path(args[args.index('--outdir') + 1]) / 'fork.whl').touch()\n"
    )
    uv.chmod(0o755)
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").touch()
    output = tmp_path / "output"
    log = tmp_path / "uv.jsonl"
    result = subprocess.run(
        ["bash", str(scripts / BUILDER.name), "a" * 40, str(output)],
        env=os.environ
        | {
            "PATH": str(tools) + os.pathsep + os.environ["PATH"],
            "ACCEPTANCE_EXIT": str(acceptance_exit),
            "UV_LOG": str(log),
            "FIXTURE_PROJECT": str(project),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == acceptance_exit, result.stdout + result.stderr
    receipt = json.loads((output / "evidence/coexistence/receipt.json").read_text())
    fork_python = Path(receipt["fork_python"])
    upstream_python = Path(receipt["upstream_python"])
    assert fork_python.parent.parent.name == "wheels"
    assert upstream_python.parent.parent.name == "upstream"
    assert upstream_python.parents[2] == fork_python.parents[2]
    assert receipt["invoked_python"] == str(fork_python)
    assert receipt["require_ui"] is True
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert ["venv", "--python", "3.12", str(upstream_python.parent.parent)] in commands
    assert ["pip", "install", "--python", str(upstream_python), "omnigent==0.16.1"] in commands
    assert not fork_python.exists()
    assert not upstream_python.exists()
    if acceptance_exit:
        assert "Built and checked eight distributions" not in result.stdout
        assert not any(command[-1].endswith("/sdists") for command in commands)
    else:
        assert "Built and checked eight distributions" in result.stdout
        assert any(command[-1].endswith("/sdists") for command in commands)
