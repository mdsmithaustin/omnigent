"""Tests for :mod:`omnigent.fork_upgrade` (installing a newer fork release)."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from packaging.version import Version

from omnigent.fork_releases import ForkRelease
from omnigent.fork_upgrade import (
    ForkUpgradeError,
    ServicePlan,
    ServiceState,
    classify_service,
    plan_fork_upgrade,
    prepare_fork_upgrade,
    rollback_commands,
    switch_to_fork_venv,
    verify_release_assets,
)
from omnigent.host.service import InstalledHostService

TARGET = "0.18.0+mdsmithaustin.3"
CURRENT = "0.18.0+mdsmithaustin.2"
TAG = "mdsmithaustin/v0.18.0-r3"
WHEELS = tuple(
    f"{name}-{TARGET}-py3-none-any.whl"
    for name in ("omnigent", "omnigent_client", "omnigent_ui_sdk", "omnigent_slack")
)
_DOWNLOAD = (
    "https://github.com/mdsmithaustin/omnigent/releases/download/mdsmithaustin%2Fv0.18.0-r3"
)


def _release(names: Sequence[str] = (*WHEELS, "SHA256SUMS", "certificate.json")) -> ForkRelease:
    return ForkRelease(
        version=Version(TARGET),
        tag=TAG,
        html_url=f"https://github.com/mdsmithaustin/omnigent/releases/tag/{TAG}",
        assets={name: f"{_DOWNLOAD}/{name}" for name in names},
    )


def _write_release(directory: Path, *, version: str = TARGET) -> None:
    """Write a release's wheels, SHA256SUMS and certificate like the publisher does."""
    directory.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for name in (*WHEELS, f"omnigent-{TARGET}.tar.gz"):
        data = f"bytes of {name}".encode()
        (directory / name).write_bytes(data)
        artifacts.append({"filename": name, "sha256": hashlib.sha256(data).hexdigest()})
    (directory / "SHA256SUMS").write_text(
        "".join(f"{a['sha256']}  {a['filename']}\n" for a in artifacts)
    )
    (directory / "certificate.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "repository": "mdsmithaustin/omnigent",
                "source": {"pin": {"version": version}},
                "artifacts": artifacts,
            }
        )
    )


def _plan(home: Path, service: ServicePlan | None = None) -> object:
    return plan_fork_upgrade(
        _release(),
        current_version=CURRENT,
        home=home,
        current_prefix=home / "old-venv",
        service=service or ServicePlan(ServiceState.ABSENT),
    )


class _Machine:
    """Fake downloads and subprocesses that record what the upgrade did."""

    def __init__(self, source: Path, *, fail: str | None = None) -> None:
        self.source = source
        self.fail = fail
        self.calls: list[tuple[str, ...]] = []
        self.envs: list[Mapping[str, str] | None] = []
        self.downloads: list[str] = []

    def download(self, url: str, destination: Path) -> None:
        self.downloads.append(url)
        destination.write_bytes((self.source / url.rsplit("/", 1)[1]).read_bytes())

    def run(
        self, args: Sequence[str], env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        call = tuple(str(arg) for arg in args)
        self.calls.append(call)
        self.envs.append(env)
        if self.fail is not None and self.fail in " ".join(call):
            return subprocess.CompletedProcess(call, 1, "", f"{self.fail} failed")
        if call[:2] == ("uv", "venv"):
            (Path(call[-1]) / "bin").mkdir(parents=True)
        elif call[:3] == ("uv", "pip", "install"):
            (Path(call[4]).parent / "omnigent").write_text(TARGET)
        elif call[-1] == "--version":
            program = Path(call[0])
            if not program.exists():
                return subprocess.CompletedProcess(call, 127, "", "not found")
            return subprocess.CompletedProcess(
                call, 0, f"omnigent {program.read_text()} (abc, built now)\n", ""
            )
        return subprocess.CompletedProcess(call, 0, "", "")


def _prepare(plan: object, machine: _Machine, *, uv: str | None = "uv") -> object:
    return prepare_fork_upgrade(
        plan, download=machine.download, run=machine.run, which=lambda _name: uv
    )


# ---------------------------------------------------------------- plan


def test_plan_targets_versioned_venv_and_fork_launcher(tmp_path: Path) -> None:
    plan = _plan(tmp_path)

    assert plan.venv == tmp_path / ".local/share/omnigent-fork" / TARGET
    assert plan.launcher == tmp_path / ".local/bin/omnigent-fork"
    assert plan.previous_venv == tmp_path / "old-venv"
    assert sorted(plan.downloads) == sorted([*WHEELS, "SHA256SUMS", "certificate.json"])


@pytest.mark.parametrize(
    ("release", "message"),
    [
        (_release((*WHEELS[:3], "SHA256SUMS", "certificate.json")), "omnigent_slack-"),
        (_release((*WHEELS, "SHA256SUMS")), "certificate.json"),
        (
            ForkRelease(
                Version(TARGET),
                TAG,
                "https://example.test",
                {
                    **_release().assets,
                    "SHA256SUMS": "https://evil.example/SHA256SUMS",
                },
            ),
            "https://evil.example/SHA256SUMS",
        ),
    ],
)
def test_plan_rejects_incomplete_or_foreign_assets(
    tmp_path: Path, release: ForkRelease, message: str
) -> None:
    with pytest.raises(ForkUpgradeError, match=message):
        plan_fork_upgrade(
            release,
            current_version=CURRENT,
            home=tmp_path,
            current_prefix=tmp_path / "old-venv",
            service=ServicePlan(ServiceState.ABSENT),
        )


@pytest.mark.parametrize(
    ("command", "state"),
    [
        (None, ServiceState.ABSENT),
        (("/h/old-venv/bin/python", "-m", "omnigent.host.service_entry", "--local"), "local"),
        (
            (
                "/h/old-venv/bin/python",
                "-m",
                "omnigent.host.service_entry",
                "--server",
                "https://x",
            ),
            ServiceState.CURRENT_REMOTE,
        ),
        (
            (f"/h/.local/share/omnigent-fork/{TARGET}/bin/python", "-m", "x", "--local"),
            ServiceState.TARGET,
        ),
        (("/opt/elsewhere/bin/python", "-m", "x", "--local"), ServiceState.OTHER),
    ],
)
def test_classify_service_by_interpreter_and_mode(
    command: tuple[str, ...] | None, state: ServiceState | str
) -> None:
    installed = (
        None
        if command is None
        else InstalledHostService(command=command, environment={"OMNIGENT_FEATURES": "a"})
    )

    plan = classify_service(
        installed,
        current_prefix=Path("/h/old-venv"),
        target_venv=Path(f"/h/.local/share/omnigent-fork/{TARGET}"),
    )

    expected = ServiceState.CURRENT_LOCAL if state == "local" else state
    assert plan.state is expected


# ---------------------------------------------------------------- verify


def test_verify_accepts_matching_release(tmp_path: Path) -> None:
    _write_release(tmp_path)

    wheels = verify_release_assets(tmp_path, TARGET)

    assert [wheel.name for wheel in wheels] == list(WHEELS)


def _corrupt_wheel(directory: Path) -> None:
    (directory / WHEELS[1]).write_bytes(b"tampered")


def _drop_sums_entry(directory: Path) -> None:
    certificate = json.loads((directory / "certificate.json").read_text())
    certificate["artifacts"] = [a for a in certificate["artifacts"] if a["filename"] != WHEELS[2]]
    (directory / "certificate.json").write_text(json.dumps(certificate))
    sums = (directory / "SHA256SUMS").read_text().splitlines(keepends=True)
    (directory / "SHA256SUMS").write_text("".join(s for s in sums if WHEELS[2] not in s))


def _certificate_hash_differs(directory: Path) -> None:
    certificate = json.loads((directory / "certificate.json").read_text())
    certificate["artifacts"][0]["sha256"] = "0" * 64
    (directory / "certificate.json").write_text(json.dumps(certificate))


def _certificate_version_differs(directory: Path) -> None:
    _write_release(directory, version="0.18.0+mdsmithaustin.9")


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        (_corrupt_wheel, re.escape(f"{WHEELS[1]} does not match SHA256SUMS")),
        (_drop_sums_entry, re.escape(f"SHA256SUMS has no entry for {WHEELS[2]}")),
        (_certificate_hash_differs, "SHA256SUMS differs from certificate.json"),
        (_certificate_version_differs, "certificate.json names v0.18.0\\+mdsmithaustin.9"),
    ],
)
def test_verify_rejects_each_mismatch(tmp_path: Path, tamper: object, message: str) -> None:
    _write_release(tmp_path)
    tamper(tmp_path)

    with pytest.raises(ForkUpgradeError, match=message):
        verify_release_assets(tmp_path, TARGET)


# ---------------------------------------------------------------- prepare


def test_prepare_installs_verified_wheels_into_new_venv(tmp_path: Path) -> None:
    _write_release(tmp_path / "release")
    machine = _Machine(tmp_path / "release")
    plan = _plan(tmp_path / "home")

    prepared = _prepare(plan, machine)

    assert prepared.reused is False
    assert sorted(url.rsplit("/", 1)[1] for url in machine.downloads) == sorted(
        [*WHEELS, "SHA256SUMS", "certificate.json"]
    )
    python = str(plan.venv / "bin/python")
    assert machine.calls[0] == ("uv", "venv", "--python", "3.12", str(plan.venv))
    assert machine.calls[1][:5] == ("uv", "pip", "install", "--python", python)
    assert [Path(p).name for p in machine.calls[1][5:]] == list(WHEELS)
    assert machine.calls[2] == (str(plan.venv / "bin/omnigent"), "--version")
    assert len(machine.calls) == 3


def test_prepare_reuses_existing_venv_of_target_version(tmp_path: Path) -> None:
    machine = _Machine(tmp_path / "release")
    plan = _plan(tmp_path / "home")
    (plan.venv / "bin").mkdir(parents=True)
    (plan.venv / "bin/omnigent").write_text(TARGET)

    prepared = _prepare(plan, machine, uv=None)

    assert prepared.reused is True
    assert machine.downloads == []
    assert machine.calls == [(str(plan.venv / "bin/omnigent"), "--version")]


def test_prepare_refuses_existing_venv_of_another_version(tmp_path: Path) -> None:
    machine = _Machine(tmp_path / "release")
    plan = _plan(tmp_path / "home")
    (plan.venv / "bin").mkdir(parents=True)
    (plan.venv / "bin/omnigent").write_text("0.18.0+mdsmithaustin.1")

    with pytest.raises(ForkUpgradeError, match=re.escape("holds omnigent 0.18.0+mdsmithaustin.1")):
        _prepare(plan, machine)

    assert (plan.venv / "bin/omnigent").read_text() == "0.18.0+mdsmithaustin.1"
    assert machine.downloads == []


def test_prepare_requires_uv_before_downloading(tmp_path: Path) -> None:
    machine = _Machine(tmp_path / "release")
    plan = _plan(tmp_path / "home")

    with pytest.raises(ForkUpgradeError, match="needs uv"):
        _prepare(plan, machine, uv=None)

    assert machine.downloads == []
    assert not plan.venv.exists()


def test_prepare_verification_failure_creates_nothing(tmp_path: Path) -> None:
    _write_release(tmp_path / "release")
    _corrupt_wheel(tmp_path / "release")
    machine = _Machine(tmp_path / "release")
    plan = _plan(tmp_path / "home")

    with pytest.raises(ForkUpgradeError, match="does not match SHA256SUMS"):
        _prepare(plan, machine)

    assert not plan.venv.exists()
    assert machine.calls == []


def test_prepare_install_failure_removes_its_own_venv_so_a_rerun_converges(
    tmp_path: Path,
) -> None:
    _write_release(tmp_path / "release")
    plan = _plan(tmp_path / "home")

    with pytest.raises(ForkUpgradeError, match="pip install failed"):
        _prepare(plan, _Machine(tmp_path / "release", fail="pip install"))
    assert not plan.venv.exists()

    assert _prepare(plan, _Machine(tmp_path / "release")).reused is False


# ---------------------------------------------------------------- switch

_SERVICE_ENV = {"OMNIGENT_FEATURES": "native_skill_routing", "PATH": "/usr/bin"}


def _current_service(home: Path) -> ServicePlan:
    return ServicePlan(
        ServiceState.CURRENT_LOCAL,
        program=str(home / "old-venv/bin/python"),
        environment=_SERVICE_ENV,
    )


def _prepared(tmp_path: Path, service: ServicePlan) -> object:
    _write_release(tmp_path / "release")
    home = tmp_path / "home"
    return _prepare(_plan(home, service), _Machine(tmp_path / "release"))


def test_switch_repoints_current_local_service_then_launcher(tmp_path: Path) -> None:
    home = tmp_path / "home"
    prepared = _prepared(tmp_path, _current_service(home))
    plan = prepared.plan
    plan.launcher.parent.mkdir(parents=True)
    plan.launcher.symlink_to(plan.previous_venv / "bin/omnigent")
    events: list[str] = []
    machine = _Machine(tmp_path / "release")

    switch_to_fork_venv(
        prepared,
        stop_server=lambda: events.append("stop"),
        run=machine.run,
        environ={"HOME": str(home), "OMNIGENT_FEATURES": "stale", "OMNIGENT_STRAY": "shell"},
        echo=events.append,
    )

    assert events[0] == "stop"
    old, new = plan.previous_venv / "bin/omnigent", plan.venv / "bin/omnigent"
    assert machine.calls == [
        (str(old), "host", "disable"),
        (str(new), "host", "enable", "--server", "", "--non-interactive"),
    ]
    assert machine.envs[1] == {"HOME": str(home), **_SERVICE_ENV}
    assert plan.launcher.readlink() == new


@pytest.mark.parametrize(
    "service",
    [
        ServicePlan(ServiceState.ABSENT),
        ServicePlan(ServiceState.OTHER, program="/opt/elsewhere/bin/python"),
        ServicePlan(ServiceState.CURRENT_REMOTE, program="/h/old-venv/bin/python"),
    ],
)
def test_switch_leaves_unmanaged_service_alone(tmp_path: Path, service: ServicePlan) -> None:
    prepared = _prepared(tmp_path, service)
    machine = _Machine(tmp_path / "release")
    echoed: list[str] = []

    switch_to_fork_venv(
        prepared, stop_server=lambda: None, run=machine.run, environ={}, echo=echoed.append
    )

    assert machine.calls == []
    assert "Left the host service unchanged" in "\n".join(echoed)
    assert prepared.plan.launcher.readlink() == prepared.plan.venv / "bin/omnigent"


def test_switch_restores_old_service_when_new_enable_fails(tmp_path: Path) -> None:
    home = tmp_path / "home"
    prepared = _prepared(tmp_path, _current_service(home))
    plan = prepared.plan
    machine = _Machine(tmp_path / "release", fail=f"{plan.venv}/bin/omnigent host enable")

    with pytest.raises(ForkUpgradeError, match="re-enabled the previous service"):
        switch_to_fork_venv(
            prepared, stop_server=lambda: None, run=machine.run, environ={}, echo=print
        )

    old = str(plan.previous_venv / "bin/omnigent")
    assert machine.calls[-1] == (old, "host", "enable", "--server", "", "--non-interactive")
    assert not plan.launcher.exists()


def test_rollback_commands_name_old_venv_in_local_mode(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = _plan(home, _current_service(home))

    commands = rollback_commands(plan)

    old, new = plan.previous_venv / "bin/omnigent", plan.venv / "bin/omnigent"
    assert commands == [
        f"{new} host disable",
        f"OMNIGENT_FEATURES=native_skill_routing {old} host enable --server '' --non-interactive",
        f"ln -sfn {old} {plan.launcher}",
    ]
