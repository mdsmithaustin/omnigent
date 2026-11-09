"""Tests for :mod:`omnigent.cli_invocation`.

Followup hints must name the configured wrapper (``isaac omni stop``) when
``OMNIGENT_WRAPPER_COMMAND`` is set, and fall back to the naked binary token
otherwise so default output is unchanged.
"""

from __future__ import annotations

import sys

import pytest

from omnigent.cli_invocation import DEFAULT_CLI_NAME, WRAPPER_COMMAND_ENV, cli_invocation


def test_defaults_to_omnigent_when_wrapper_unset() -> None:
    assert cli_invocation(env={}) == "omnigent"
    assert DEFAULT_CLI_NAME == "omnigent"


def test_preserves_omni_alias_when_wrapper_unset() -> None:
    # A hint that spells the short ``omni`` alias keeps it verbatim.
    assert cli_invocation(name="omni", env={}) == "omni"


def test_wrapper_command_overrides_both_names() -> None:
    env = {WRAPPER_COMMAND_ENV: "isaac omni"}
    assert cli_invocation(env=env) == "isaac omni"
    assert cli_invocation(name="omni", env=env) == "isaac omni"


def test_blank_wrapper_command_is_ignored() -> None:
    assert cli_invocation(env={WRAPPER_COMMAND_ENV: "   "}) == "omnigent"
    assert cli_invocation(env={WRAPPER_COMMAND_ENV: ""}) == "omnigent"


def test_wrapper_command_is_stripped() -> None:
    assert cli_invocation(env={WRAPPER_COMMAND_ENV: "  isaac omni  "}) == "isaac omni"


def test_reads_process_environment_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WRAPPER_COMMAND_ENV, "isaac omni")
    assert cli_invocation() == "isaac omni"
    monkeypatch.delenv(WRAPPER_COMMAND_ENV, raising=False)
    assert cli_invocation() == "omnigent"


def test_rendered_hint_names_the_wrapper() -> None:
    env = {WRAPPER_COMMAND_ENV: "isaac omni"}
    assert f"Run `{cli_invocation(env=env)} stop`" == "Run `isaac omni stop`"
    assert f"Run `{cli_invocation(env={})} stop`" == "Run `omnigent stop`"


@pytest.mark.parametrize("launcher", ["omni-fork", "omnigent-fork"])
def test_fork_hint_uses_invoked_alias(monkeypatch: pytest.MonkeyPatch, launcher: str) -> None:
    monkeypatch.setattr(sys, "argv", [f"/home/user/.local/bin/{launcher}"])
    assert cli_invocation(env={}) == launcher
    assert cli_invocation(name="omni", env={}) == launcher
    assert cli_invocation(name="/other/venv/bin/omnigent", env={}) == "/other/venv/bin/omnigent"
    assert cli_invocation(env={WRAPPER_COMMAND_ENV: "isaac omni"}) == "isaac omni"


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.21.0", "omnigent"),
        ("0.21.0+mdsmithaustin.1", "'/path with spaces/bin/omnigent'"),
    ],
)
def test_generic_launcher_uses_absolute_path_for_fork_builds(
    monkeypatch: pytest.MonkeyPatch, version: str, expected: str
) -> None:
    monkeypatch.setattr(sys, "argv", ["/path with spaces/bin/omnigent"])
    monkeypatch.setattr("omnigent.cli_invocation.VERSION", version)
    assert cli_invocation(env={}) == expected
