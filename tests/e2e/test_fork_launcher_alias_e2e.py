from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from packaging.version import Version

from omnigent.fork_releases import ForkRelease
from omnigent.fork_upgrade import (
    ForkUpgradePlan,
    PreparedVenv,
    ServicePlan,
    ServiceState,
    rollback_commands,
    switch_to_fork_venv,
)


def test_fork_short_launcher_runs_updated_and_rolled_back_install(tmp_path: Path) -> None:
    previous, target = tmp_path / "previous", tmp_path / "target"
    launcher = tmp_path / "bin" / "omnigent-fork"
    for venv, version in (
        (previous, "0.21.0+mdsmithaustin.1"),
        (target, "0.21.0+mdsmithaustin.2"),
    ):
        command = venv / "bin" / "omnigent"
        command.parent.mkdir(parents=True)
        command.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(version)}\n")
        command.chmod(0o755)
    launcher.parent.mkdir()
    launcher.symlink_to(previous / "bin" / "omnigent")
    plan = ForkUpgradePlan(
        current_version="0.21.0+mdsmithaustin.1",
        release=ForkRelease(Version("0.21.0+mdsmithaustin.2"), "r2", "", {}),
        downloads={},
        venv=target,
        previous_venv=previous,
        launcher=launcher,
        service=ServicePlan(ServiceState.ABSENT),
    )

    switch_to_fork_venv(PreparedVenv(plan, reused=True), stop_server=lambda: None)

    alias = launcher.with_name("omni-fork")
    assert subprocess.check_output([alias, "--version"], text=True).strip() == plan.version
    subprocess.run(shlex.split(rollback_commands(plan)[0]), check=True)
    assert subprocess.check_output([alias, "--version"], text=True).strip() == plan.current_version
