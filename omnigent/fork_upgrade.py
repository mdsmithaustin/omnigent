"""Install a newer fork release into its own venv and switch to it.

Preparation (download, verify, install, version check) changes nothing the
user runs. Only a :class:`PreparedVenv` can be switched to, and the switch
keeps the previous venv so it stays available for rollback.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from omnigent.cli_invocation import cli_invocation
from omnigent.fork_releases import ForkRelease
from omnigent.host.service import InstalledHostService
from omnigent.installation_defaults import RELEASE_REPOSITORY

FORK_DISTRIBUTIONS = ("omnigent", "omnigent_client", "omnigent_ui_sdk", "omnigent_slack")
_EVIDENCE = ("SHA256SUMS", "certificate.json")
_DOWNLOAD_PREFIX = f"https://github.com/{RELEASE_REPOSITORY}/releases/download/"
_PYTHON = "3.12"
# Local-mode service enable: an empty --server skips the config's `server:` key.
_LOCAL_ENABLE = ("host", "enable", "--server", "", "--non-interactive")
_SECRET_MARKERS = ("TOKEN", "SECRET", "KEY", "PASSWORD")

Run = Callable[..., subprocess.CompletedProcess[str]]
Download = Callable[[str, Path], None]


class ForkUpgradeError(RuntimeError):
    """A fork upgrade step failed; the message says what was left unchanged."""


class ServiceState(enum.Enum):
    ABSENT = "no host service is installed"
    CURRENT_LOCAL = "the host service runs this install in local mode"
    CURRENT_REMOTE = "the host service runs this install against a remote server"
    TARGET = "the host service already runs the new release"
    OTHER = "the host service runs another omnigent install"
    UNREADABLE = "the host service definition could not be read"


@dataclass(frozen=True)
class ServicePlan:
    state: ServiceState
    program: str | None = None
    environment: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ForkUpgradePlan:
    current_version: str
    release: ForkRelease
    downloads: Mapping[str, str]
    venv: Path
    previous_venv: Path
    launcher: Path
    service: ServicePlan

    @property
    def version(self) -> str:
        return str(self.release.version)


@dataclass(frozen=True)
class PreparedVenv:
    """A verified install of the plan's release; only :func:`prepare_fork_upgrade` makes one."""

    plan: ForkUpgradePlan
    reused: bool


def wheel_names(version: str) -> tuple[str, ...]:
    """The four wheel asset names of fork release *version*."""
    return tuple(f"{name}-{version}-py3-none-any.whl" for name in FORK_DISTRIBUTIONS)


def fork_venv(home: Path, version: str) -> Path:
    """Where fork release *version* is installed for the user at *home*."""
    return home / ".local/share/omnigent-fork" / version


def fork_launcher(home: Path) -> Path:
    """The fork's own entry point, kept apart from an upstream ``omni``/``omnigent``."""
    return home / ".local/bin/omnigent-fork"


def upgrade_command(*, home: Path, prefix: Path) -> str:
    """The upgrade command for the install at *prefix*, via the fork launcher if it runs it."""
    launcher = fork_launcher(home)
    runs_here = launcher.exists() and launcher.resolve().parent.parent == prefix.resolve()
    entry = launcher.name if runs_here else str(prefix / "bin/omnigent")
    return f"{cli_invocation(name=entry)} upgrade"


def _venv_root(program: str) -> Path:
    return Path(program).parent.parent.resolve()


def classify_service(
    installed: InstalledHostService | None, *, current_prefix: Path, target_venv: Path
) -> ServicePlan:
    """Decide what the upgrade may do with the user's host service."""
    if installed is None or not installed.command:
        return ServicePlan(ServiceState.ABSENT)
    program = installed.command[0]
    root = _venv_root(program)
    if root == target_venv.resolve():
        state = ServiceState.TARGET
    elif root != current_prefix.resolve():
        state = ServiceState.OTHER
    elif "--local" in installed.command:
        state = ServiceState.CURRENT_LOCAL
    else:
        state = ServiceState.CURRENT_REMOTE
    return ServicePlan(state, program, dict(installed.environment))


def _is_release_asset_url(url: str, tag: str, name: str) -> bool:
    # GitHub percent-encodes the "+" of a fork version in browser_download_url.
    return unquote(url) == f"{_DOWNLOAD_PREFIX}{tag}/{name}"


def plan_fork_upgrade(
    release: ForkRelease,
    *,
    current_version: str,
    home: Path,
    current_prefix: Path,
    service: ServicePlan,
) -> ForkUpgradePlan:
    """Resolve where *release* installs and which of its assets to fetch."""
    version = str(release.version)
    downloads: dict[str, str] = {}
    for name in (*wheel_names(version), *_EVIDENCE):
        url = release.assets.get(name)
        if url is None:
            raise ForkUpgradeError(f"Release {release.tag} has no {name} asset.")
        if not _is_release_asset_url(url, release.tag, name):
            raise ForkUpgradeError(
                f"Refusing {name} from {url}: not the {name} asset of "
                f"{RELEASE_REPOSITORY} release {release.tag}."
            )
        downloads[name] = url
    return ForkUpgradePlan(
        current_version=current_version,
        release=release,
        downloads=downloads,
        venv=fork_venv(home, version),
        previous_venv=current_prefix,
        launcher=fork_launcher(home),
        service=service,
    )


def verify_release_assets(directory: Path, version: str) -> list[Path]:
    """Check the downloaded wheels against SHA256SUMS and certificate.json.

    Mirrors the parts of ``scripts/fork_release.py verify`` that hold without
    the source checkout: the certificate's version and artifact hashes.
    """
    try:
        certificate = json.loads((directory / "certificate.json").read_bytes())
        supported = certificate["schema"] == 1 and certificate["repository"] == RELEASE_REPOSITORY
        certified_version = certificate["source"]["pin"]["version"]
        listed = "".join(f"{a['sha256']}  {a['filename']}\n" for a in certificate["artifacts"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ForkUpgradeError(f"certificate.json is malformed: {exc!r}") from exc
    if not supported:
        raise ForkUpgradeError("certificate.json is not a supported fork release certificate.")
    if certified_version != version:
        raise ForkUpgradeError(
            f"certificate.json names v{certified_version}, expected v{version}."
        )
    sums_text = (directory / "SHA256SUMS").read_text()
    if sums_text != listed:
        raise ForkUpgradeError("SHA256SUMS differs from certificate.json.")
    sums = {
        name: digest
        for digest, _, name in (line.partition("  ") for line in sums_text.splitlines())
    }
    wheels = []
    for name in wheel_names(version):
        if name not in sums:
            raise ForkUpgradeError(f"SHA256SUMS has no entry for {name}.")
        path = directory / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != sums[name]:
            raise ForkUpgradeError(f"{name} does not match SHA256SUMS.")
        wheels.append(path)
    return wheels


def _run(
    args: Sequence[str], env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [str(arg) for arg in args], capture_output=True, text=True, check=False, env=env
        )
    except OSError as exc:
        return subprocess.CompletedProcess(list(args), 127, "", str(exc))


def _download(url: str, destination: Path) -> None:
    import httpx

    with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as response:
        response.raise_for_status()
        with destination.open("wb") as handle:
            for chunk in response.iter_bytes():
                handle.write(chunk)


def _installed_version(run: Run, venv: Path) -> str | None:
    result = run([venv / "bin/omnigent", "--version"])
    words = result.stdout.split()
    return (
        words[1]
        if result.returncode == 0 and words[:1] == ["omnigent"] and len(words) > 1
        else None
    )


def _checked(run: Run, args: Sequence[str | Path], what: str) -> None:
    result = run(args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ForkUpgradeError(f"{what} failed: {detail}")


def prepare_fork_upgrade(
    plan: ForkUpgradePlan,
    *,
    download: Download = _download,
    run: Run = _run,
    which: Callable[[str], str | None] = shutil.which,
) -> PreparedVenv:
    """Install the verified release into ``plan.venv`` without touching anything in use."""
    if plan.venv.exists():
        found = _installed_version(run, plan.venv)
        if found == plan.version:
            return PreparedVenv(plan, reused=True)
        held = f"omnigent {found}" if found else "no working omnigent"
        raise ForkUpgradeError(
            f"{plan.venv} already exists and holds {held}. "
            "Move it aside and run the upgrade again."
        )
    uv = which("uv")
    if uv is None:
        raise ForkUpgradeError(
            "Installing a fork release needs uv on PATH: https://docs.astral.sh/uv/"
        )
    with tempfile.TemporaryDirectory(prefix="omnigent-fork-") as scratch:
        directory = Path(scratch)
        for name, url in plan.downloads.items():
            try:
                download(url, directory / name)
            except Exception as exc:
                raise ForkUpgradeError(f"Downloading {name} failed: {exc}") from exc
        wheels = verify_release_assets(directory, plan.version)
        try:
            _checked(run, [uv, "venv", "--python", _PYTHON, plan.venv], "uv venv")
            _checked(
                run,
                [uv, "pip", "install", "--python", plan.venv / "bin/python", *wheels],
                "uv pip install",
            )
            found = _installed_version(run, plan.venv)
            if found != plan.version:
                raise ForkUpgradeError(
                    f"The new venv reports omnigent {found}, expected {plan.version}."
                )
        except BaseException:
            # This run created the directory, so removing it lets a rerun start clean.
            shutil.rmtree(plan.venv, ignore_errors=True)
            raise
    return PreparedVenv(plan, reused=False)


def _carried_assignments(environment: Mapping[str, str]) -> str:
    return " ".join(
        f"{key}={shlex.quote(value)}"
        for key, value in sorted(environment.items())
        if key.startswith("OMNIGENT_") and not any(m in key for m in _SECRET_MARKERS)
    )


def _local_enable_command(omnigent: Path, environment: Mapping[str, str]) -> str:
    enable = f"{omnigent} {shlex.join(_LOCAL_ENABLE)}"
    carried = _carried_assignments(environment)
    return f"{carried} {enable}" if carried else enable


def rollback_commands(plan: ForkUpgradePlan) -> list[str]:
    """The shell commands that return to the previous venv."""
    old = plan.previous_venv / "bin/omnigent"
    commands = []
    if plan.service.state is ServiceState.CURRENT_LOCAL:
        commands += [
            f"{plan.venv / 'bin/omnigent'} host disable",
            _local_enable_command(old, plan.service.environment),
        ]
    return [*commands, f"ln -sfn {old} {plan.launcher}"]


def describe_plan(plan: ForkUpgradePlan) -> list[str]:
    service = plan.service
    action = (
        "re-point it to the new venv in local mode"
        if service.state is ServiceState.CURRENT_LOCAL
        else "leave it unchanged"
    )
    return [
        f"Install v{plan.version} from {plan.release.html_url}",
        f"  venv:     {plan.venv}",
        f"  launcher: {plan.launcher} -> {plan.venv / 'bin/omnigent'}",
        f"  service:  {service.state.value}; {action}",
        f"  kept for rollback: {plan.previous_venv}",
    ]


def _move_launcher(launcher: Path, target: Path) -> None:
    launcher.parent.mkdir(parents=True, exist_ok=True)
    staged = launcher.with_name(f".{launcher.name}.{os.getpid()}.tmp")
    staged.unlink(missing_ok=True)
    staged.symlink_to(target)
    os.replace(staged, launcher)


def switch_to_fork_venv(
    prepared: PreparedVenv,
    *,
    stop_server: Callable[[], None],
    run: Run = _run,
    environ: Mapping[str, str] = os.environ,
    echo: Callable[[str], None] = print,
) -> None:
    """Stop the server, re-point a managed host service, then move the launcher."""
    plan = prepared.plan
    old, new = plan.previous_venv / "bin/omnigent", plan.venv / "bin/omnigent"
    stop_server()
    service = plan.service
    if service.state is ServiceState.CURRENT_LOCAL:
        # Re-enable with the service's own environment, not this shell's.
        env = {**{k: environ[k] for k in ("HOME",) if k in environ}, **service.environment}
        # launchd refuses to bootstrap a label that is still loaded, so disable first.
        if run([old, "host", "disable"], env=env).returncode != 0:
            raise ForkUpgradeError("Disabling the host service failed; nothing was switched.")
        if run([new, *_LOCAL_ENABLE], env=env).returncode != 0:
            restored = run([old, *_LOCAL_ENABLE], env=env).returncode == 0
            outcome = (
                "re-enabled the previous service"
                if restored
                else "the host service is now disabled. Re-enable it with: "
                + _local_enable_command(old, service.environment)
            )
            raise ForkUpgradeError(
                f"Enabling the host service from {plan.venv} failed; {outcome}. "
                f"The launcher still points at {old}."
            )
        echo(f"Re-pointed the host service to {plan.venv} (local mode).")
    else:
        echo(f"Left the host service unchanged: {service.state.value}.")
    _move_launcher(plan.launcher, new)
    echo(f"{plan.launcher} -> {new}")
