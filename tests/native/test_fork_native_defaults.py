"""Native default path isolation and the paired private-directory checks."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.mark.parametrize("data_override", [False, True])
def test_native_defaults_in_fresh_process(tmp_path: Path, data_override: bool) -> None:
    home = tmp_path / "home"
    temp = tmp_path / "temp"
    home.mkdir()
    temp.mkdir()
    env = os.environ.copy()
    for name in (
        "OMNIGENT_DATA_DIR",
        "OMNIGENT_HARNESS_TMP_PARENT",
        "OMNIGENT_CLAUDE_NATIVE_STATE_DIR",
        "OMNIGENT_CODEX_NATIVE_STATE_DIR",
        "OMNIGENT_OPENCODE_NATIVE_STATE_DIR",
    ):
        env.pop(name, None)
    env.update(HOME=str(home), TMPDIR=str(temp))
    if data_override:
        env["OMNIGENT_DATA_DIR"] = str(tmp_path / "selected-data")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib, json
from omnigent.harnesses.claude_native import bridge as claude
from omnigent.harnesses.prime_native import bridge as prime
from omnigent.native.admission import native_owner
from omnigent.runtime.harnesses.paths import harness_tmp_parent
paths = {}
for name in ('codex', 'pi', 'opencode', 'antigravity', 'cursor', 'devin',
             'goose', 'hermes', 'kimi', 'kiro', 'qwen'):
    module = importlib.import_module('omnigent.harnesses.' + name + '_native.bridge')
    accessor = getattr(module, 'bridge_dir_for_session_id', None)
    if accessor is None:
        accessor = module.bridge_dir_for_bridge_id
    paths[name] = str(accessor('same-session'))
paths['claude'] = str(claude.bridge_dir_for_conversation_id('same-session'))
paths['router'] = str(claude.subagent_router_bridge_root())
paths['acp'] = str(claude.acp_mcp_bridge_root())
paths['approval'] = str(claude.approval_wait_marker_path('same-session'))
paths['prime-main'] = str(prime.bridge_roots()[0])
paths['prime-compact'] = str(prime.bridge_roots()[1])
paths['admission'] = native_owner('same-session', 'unknown-provider').runtime
paths['harness-temp'] = str(harness_tmp_parent())
for name in ('claude', 'codex', 'opencode'):
    module = importlib.import_module('omnigent.harnesses.' + name + '_native.state')
    paths[name + '-state'] = str(getattr(module, '_' + name + '_native_state_root')())
print(json.dumps(paths))
""",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    actual = json.loads(result.stdout)
    digest = hashlib.sha256(b"same-session").hexdigest()[:32]
    uid = str(os.getuid())
    persistent = home / ".omnigent-mdsmithaustin"
    data = tmp_path / "selected-data" if data_override else persistent
    for name in ("codex", "pi", "opencode", "antigravity"):
        expected = persistent / f"{name}-native" / digest
        assert actual[name] == str(expected)
        assert expected != home / ".omnigent" / f"{name}-native" / digest
    for name in ("claude", "cursor", "devin", "goose", "hermes", "kimi", "kiro", "qwen"):
        expected = temp.resolve() / f"mdma-{uid}" / f"{name}-native" / digest
        assert Path(actual[name]).resolve() == expected
        assert expected != temp.resolve() / f"omnigent-{uid}" / f"{name}-native" / digest
    assert Path(actual["router"]).resolve() == temp.resolve() / f"mdma-{uid}" / "subagent-router"
    assert Path(actual["acp"]).resolve() == temp.resolve() / f"mdma-{uid}" / "acp-mcp"
    assert Path(actual["approval"]).parent.resolve() == (
        temp.resolve() / f"mdma-{uid}" / "claude-native" / "approval-waits"
    )
    assert actual["prime-main"] == str(data / "prime-native")
    assert actual["prime-compact"] == f"/tmp/mdp-{uid}"
    assert actual["harness-temp"] == f"/tmp/mdma-{uid}"
    assert actual["admission"] == str(
        persistent / "native-owners" / "unknown-provider" / "same-session"
    )
    for name in ("claude", "codex", "opencode"):
        assert actual[name + "-state"] == str(data / f"{name}-native")


@pytest.mark.parametrize("provider", ["codex", "pi", "opencode", "antigravity", "prime"])
@pytest.mark.parametrize("unsafe", ["safe", "symlink", "foreign-owner", "loose-mode"])
def test_persistent_bridge_ancestor_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, unsafe: str
) -> None:
    from omnigent.harnesses.claude_native import bridge as claude

    module = importlib.import_module(f"omnigent.harnesses.{provider}_native.bridge")
    parent = tmp_path / ".omnigent-mdsmithaustin"
    if provider == "prime":
        monkeypatch.setattr(module, "_DATA_ROOT", parent)
    else:
        monkeypatch.setattr(module, "_BRIDGE_ROOT", parent / f"{provider}-native")
    if unsafe == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o700)
        parent.symlink_to(outside, target_is_directory=True)
    else:
        parent.mkdir(mode=0o700)
    target = parent / f"{provider}-native" / "session"
    assert claude._trusted_parent_for_bridge_dir(target) == tmp_path
    if unsafe == "foreign-owner":
        monkeypatch.setattr(claude.os, "getuid", lambda: parent.stat().st_uid + 1)
    elif unsafe == "loose-mode":
        parent.chmod(0o777)
    if unsafe in {"symlink", "foreign-owner"}:
        with pytest.raises(RuntimeError, match=r"symlink|owned"):
            claude.ensure_secure_dir(target)
    else:
        claude.ensure_secure_dir(target)
        assert target.is_dir()
        assert stat.S_IMODE(parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_private_codex_sources_and_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.inner import codex_executor, codex_staging

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    external = tmp_path / ".codex"
    assert codex_executor._codex_home_config_source_from_env() == external
    private = tmp_path / ".omnigent-mdsmithaustin" / "codex-native" / "session" / "codex-home"
    private.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(private))
    assert codex_executor._codex_home_config_source_from_env() == external
    custom = tmp_path / "provider-home"
    custom.mkdir()
    (custom / "auth.json").write_text("provider sentinel")
    (private / "auth.json").symlink_to(custom / "auth.json")
    assert codex_executor._codex_home_config_source_from_env() == custom
    monkeypatch.setenv("CODEX_HOME", "relative-provider-home")
    assert codex_executor._codex_home_config_source_from_env() == Path("relative-provider-home")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    staging = codex_staging.codex_home_staging_root()
    assert staging == tmp_path.resolve() / f"mdma-codex-homes-{os.getuid()}"
    wrapped = Path(tempfile.mkdtemp(prefix=codex_staging.CODEX_HOME_PREFIX, dir=staging))
    assert wrapped.name.startswith("mdma-codex-home-")
    monkeypatch.setenv("CODEX_HOME", str(wrapped))
    assert codex_executor._codex_home_config_source_from_env() == external
    skills = Path(tempfile.mkdtemp(prefix=codex_staging.CODEX_SKILLS_PREFIX, dir=tmp_path))
    assert skills.name.startswith("mdma-codex-skills-")
    assert codex_staging.prepare_codex_skills_dir(skills) == skills.resolve()


def test_prime_constructed_socket_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.harnesses.prime_native import bridge

    main = tmp_path / ("long-root-" * 12)
    monkeypatch.setattr(bridge, "_DATA_ROOT", main)
    paths = bridge.runtime_paths("same-session")
    uid = str(os.getuid())
    assert paths.root.parent == Path(f"/tmp/mdp-{uid}")
    socket = paths.temp_dir / f"prime-agent-{uid}" / ("worker-" + "0" * 25 + ".sock")
    assert len(str(socket.resolve()).encode()) < 100
    assert bridge.runtime_paths("same-session").root == paths.root
    monkeypatch.setattr(bridge, "_DATA_ROOT", main / "other-installation")
    assert bridge.runtime_paths("same-session").root != paths.root
    monkeypatch.setenv(bridge.PRIME_NATIVE_BRIDGE_DIR_ENV_VAR, "relative/prime")
    assert bridge.executor_bridge_dir() == Path("relative/prime")


def test_temp_selectors_and_secondary_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.host.jcode_databricks import _session_jcode_home
    from omnigent.runner.tool_dispatch import _runner_default_os_env_cwd
    from omnigent.runtime.harnesses import paths

    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.harness_tmp_parent({"OMNIGENT_HARNESS_TMP_PARENT": "relative/root"}) == Path(
        "relative/root"
    )
    assert (
        paths.harness_tmp_parent({"OMNIGENT_HARNESS_TMP_PARENT": "~/selected"})
        == tmp_path / "selected"
    )
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    assert paths.harness_tmp_parent({}) == tmp_path / "mdma"
    monkeypatch.delenv("OMNIGENT_HARNESS_TMP_PARENT", raising=False)
    digest = hashlib.sha256(b"same-session").hexdigest()[:32]
    assert _session_jcode_home("same-session") == tmp_path / "mdma-jcode-run" / digest
    monkeypatch.setenv("OMNIGENT_HARNESS_TMP_PARENT", str(tmp_path / "selected"))
    assert _session_jcode_home("same-session") == tmp_path / "selected" / "mdma-jcode-run" / digest
    monkeypatch.delenv("OMNIGENT_RUNNER_OS_ENV_ROOT", raising=False)
    assert _runner_default_os_env_cwd("same/session") == str(
        tmp_path / "mdma-runner-os-envs" / "same_session" / "workspace"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMNIGENT_RUNNER_OS_ENV_ROOT", "relative")
    assert _runner_default_os_env_cwd("same/session") == "relative/same_session/workspace"
    monkeypatch.setenv("OMNIGENT_RUNNER_OS_ENV_ROOT", "")
    assert _runner_default_os_env_cwd("same/session") == "same_session/workspace"


def test_model_probe_default_keeps_external_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.harnesses.codex_native import app_server

    monkeypatch.setenv("HOME", str(tmp_path))
    source = tmp_path / "provider-home"
    source.mkdir()
    (source / "auth.json").write_text("provider sentinel")
    monkeypatch.setenv("CODEX_HOME", str(source))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "selected-data"))
    home = app_server._probe_codex_home([])
    assert home.parent == tmp_path / ".omnigent-mdsmithaustin" / "cache" / "codex-model-probe"
    assert (home / "auth.json").resolve() == source / "auth.json"
    assert (source / "auth.json").read_text() == "provider sentinel"


@pytest.mark.parametrize("provider", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("selected", ["relative/state", "~/selected-state"])
def test_launch_state_selectors_keep_literal_paths(
    monkeypatch: pytest.MonkeyPatch, provider: str, selected: str
) -> None:
    module = importlib.import_module(f"omnigent.harnesses.{provider}_native.state")
    monkeypatch.setenv(f"OMNIGENT_{provider.upper()}_NATIVE_STATE_DIR", selected)
    root = getattr(module, f"_{provider}_native_state_root")()
    assert root == Path(selected)
