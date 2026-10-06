from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from omnigent.models import model_catalog_store
from omnigent.onboarding import provider_config
from omnigent.onboarding.sandboxes.base import render_host_config_write_command
from omnigent.onboarding.sandboxes.bootstrap import set_sandbox_host_name
from omnigent.repl._event_tape import open_event_log
from omnigent.runner import identity as runner_identity
from omnigent.runner._entry import _runner_config_path
from omnigent.runtime.workflow import _load_global_auth
from omnigent.server import admin_list, dictation, server_config
from omnigent.telemetry.client import _config_telemetry_disabled


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (
        "OMNIGENT_DATA_DIR",
        "OMNIGENT_CONFIG_HOME",
        "OMNIGENT_CONFIG",
        "OMNIGENT_ADMIN_CREDENTIALS_PATH",
        "OMNIGENT_ADMIN_LIST_PATH",
        "OMNIGENT_DICTATION_MODEL_DIR",
        "OMNIGENT_DICTATION_PUNCT_DIR",
        "OMNIGENT_RUNNER_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _run_python(home: Path, source: str) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("OMNIGENT_") and key != "PYTHONPATH"
    }
    env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        env=env,
        cwd=home,
        capture_output=True,
        text=True,
        check=True,
    )


def test_catalog_and_runner_writes_leave_upstream_files_untouched(isolated_home: Path) -> None:
    upstream = isolated_home / ".omnigent"
    upstream_runner = upstream / "runners" / "runner_id"
    upstream_runner.parent.mkdir(parents=True)
    upstream_runner.write_text("runner_upstream\n")
    catalog = upstream / "cache" / "model-catalogs" / "claude-native-same.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text('{"models":[{"id":"upstream"}]}')
    original_catalog = catalog.read_bytes()
    rows = [{"id": "fork-model"}]

    model_catalog_store.write_catalog("claude-native", "same", rows)
    runner_id = runner_identity.get_stable_runner_id()

    fork = isolated_home / ".omnigent-mdsmithaustin"
    assert model_catalog_store.catalog_path("claude-native", "same") == (
        fork / "cache" / "model-catalogs" / "claude-native-same.json"
    )
    assert model_catalog_store.read_catalog("claude-native", "same") == rows
    assert (fork / "runners" / "runner_id").read_text() == runner_id
    assert runner_id.startswith("runner_")
    assert runner_identity.get_stable_runner_id() == runner_id
    assert upstream_runner.read_text() == "runner_upstream\n"
    assert catalog.read_bytes() == original_catalog


def test_data_override_does_not_select_runner_identity(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", "~/custom-data")
    rows = [{"id": "override-model"}]
    model_catalog_store.write_catalog("claude-native", "override", rows)
    assert model_catalog_store.catalog_path("claude-native", "override") == (
        isolated_home / "custom-data" / "cache" / "model-catalogs" / "claude-native-override.json"
    )
    assert model_catalog_store.read_catalog("claude-native", "override") == rows
    runner_id = runner_identity.get_stable_runner_id()
    assert (isolated_home / ".omnigent-mdsmithaustin" / "runners" / "runner_id").read_text() == (
        runner_id
    )
    selected = isolated_home / "explicit-runner-id"
    selected.write_text("runner_explicit\n")
    assert runner_identity.load_or_create_runner_id(selected) == "runner_explicit"
    monkeypatch.setenv("OMNIGENT_RUNNER_ID", "runner_environment")
    assert runner_identity.get_stable_runner_id() == "runner_environment"


@pytest.mark.parametrize("config_home", [None, "selected-config"])
def test_config_readers_ignore_upstream_config(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, config_home: str | None
) -> None:
    upstream = isolated_home / ".omnigent" / "config.yaml"
    upstream.parent.mkdir()
    upstream.write_text("auth:\n  type: api_key\n  api_key: upstream\ntelemetry: true\n")
    config_dir = isolated_home / (config_home or ".omnigent-mdsmithaustin")
    config_dir.mkdir()
    config = config_dir / "config.yaml"
    config.write_text("auth:\n  type: api_key\n  api_key: fork\ntelemetry: false\n")
    if config_home:
        monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_dir))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(isolated_home / "unrelated-data"))

    assert provider_config._config_path() == str(config)
    assert _runner_config_path() == config
    auth = _load_global_auth()
    assert auth is not None
    assert auth.api_key == "fork"
    assert _config_telemetry_disabled() is True
    assert "api_key: upstream" in upstream.read_text()


def test_config_override_keeps_owner_expansion_rules(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", "~/selected-config")
    assert provider_config._config_path() == "~/selected-config/config.yaml"
    assert _runner_config_path() == isolated_home / "selected-config" / "config.yaml"


def test_server_config_inherits_admin_default_and_keeps_explicit_selectors(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = isolated_home / ".omnigent" / "config.yaml"
    upstream.parent.mkdir()
    upstream.write_text("session_title_instructions: upstream\n")
    assert server_config.resolve_config_path() is None
    fork = isolated_home / ".omnigent-mdsmithaustin"
    fork.mkdir()
    config = fork / "config.yaml"
    config.write_text("session_title_instructions: fork\n")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(isolated_home / "data-selector"))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(isolated_home / "config-selector"))
    assert admin_list.resolve_data_dir() == fork
    assert admin_list.resolve_admin_list_path() == fork / "admins"
    assert server_config.load_server_config()["session_title_instructions"] == "fork"
    monkeypatch.setenv("OMNIGENT_ADMIN_CREDENTIALS_PATH", "/selected/admin-credentials")
    assert admin_list.resolve_data_dir() == Path("/selected")
    monkeypatch.setenv("OMNIGENT_ADMIN_LIST_PATH", "/roster/admins")
    assert admin_list.resolve_admin_list_path() == Path("/roster/admins")
    monkeypatch.setenv("OMNIGENT_CONFIG", str(upstream))
    assert server_config.load_server_config()["session_title_instructions"] == "upstream"


def test_dictation_and_debug_defaults_keep_existing_selectors(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(isolated_home / "data-selector"))
    monkeypatch.setenv("OMNIGENT_DICTATION_MODEL_ROOT", str(isolated_home / "script-selector"))
    fork = isolated_home / ".omnigent-mdsmithaustin"
    assert dictation._asr_dir() == fork / "models" / "dictation" / "asr"
    assert dictation._punct_dir() == fork / "models" / "dictation" / "punct"
    assert open_event_log("same/session") == fork / "debug" / "events-same_session.jsonl"
    monkeypatch.setenv("OMNIGENT_DICTATION_MODEL_DIR", "~/asr-override")
    monkeypatch.setenv("OMNIGENT_DICTATION_PUNCT_DIR", "~/punct-override")
    assert dictation._asr_dir() == isolated_home / "asr-override"
    assert dictation._punct_dir() == isolated_home / "punct-override"


def test_import_time_defaults_keep_timing_and_update_cache_isolation(tmp_path: Path) -> None:
    upstream = tmp_path / ".omnigent" / ".update_check.json"
    upstream.parent.mkdir()
    upstream.write_text("upstream-cache")
    upstream_config = upstream.parent / "config.yaml"
    upstream_config.write_text("host:\n  name: upstream\n  host_id: upstream-id\n")
    _run_python(
        tmp_path,
        """
        import os
        from pathlib import Path
        from omnigent.host import identity
        from omnigent import update_check
        home = Path.home()
        expected = home / '.omnigent-mdsmithaustin' / 'config.yaml'
        assert identity.host_config_path() == expected
        created = identity.load_or_create_host_identity()
        assert len(created.host_id) == 32
        assert identity.load_or_create_host_identity().host_id == created.host_id
        explicit = home / 'explicit.yaml'
        assert identity.host_config_path(explicit) == explicit
        os.environ['HOME'] = str(home / 'later-home')
        assert identity.host_config_path() == expected
        update_check._write_cache(update_check._CacheEntry(1.0, 3, head_sha='fork-sha'))
        os.environ['OMNIGENT_CONFIG_HOME'] = str(home / 'config-override')
        assert identity.host_config_path() == home / 'config-override' / 'config.yaml'
        """,
    )
    cache = tmp_path / ".omnigent-mdsmithaustin" / ".update_check.json"
    assert json.loads(cache.read_text())["head_sha"] == "fork-sha"
    assert upstream.read_text() == "upstream-cache"
    host = yaml.safe_load((cache.parent / "config.yaml").read_text())["host"]
    assert len(host["host_id"]) == 32
    assert host["name"] != "upstream"
    assert upstream_config.read_text() == "host:\n  name: upstream\n  host_id: upstream-id\n"


def test_databricks_temporary_config_preserves_provider_file(tmp_path: Path) -> None:
    provider = tmp_path / ".databrickscfg"
    provider.write_text("[external]\nhost = https://provider.example\n")
    _run_python(
        tmp_path,
        """
        import os
        import sys
        from pathlib import Path
        from types import ModuleType, SimpleNamespace
        from omnigent.cli_config import _isolated_databricks_cfg
        internal_beta = ModuleType('omnigent.onboarding.internal_beta')
        internal_beta.DEFAULT_PROFILES = [SimpleNamespace(name='external')]
        sys.modules[internal_beta.__name__] = internal_beta
        home = Path.home()
        try:
            with _isolated_databricks_cfg():
                selected = Path(os.environ['DATABRICKS_CONFIG_FILE'])
                assert selected.parent == home / '.omnigent-mdsmithaustin'
                assert 'https://provider.example' in selected.read_text()
                raise RuntimeError('cancel setup')
        except RuntimeError as error:
            assert str(error) == 'cancel setup'
        assert not selected.exists()
        """,
    )
    assert provider.read_text() == "[external]\nhost = https://provider.example\n"


@pytest.mark.parametrize("writer", ["injected", "bootstrap"])
@pytest.mark.parametrize("config_home", [None, "target config"])
def test_generated_writers_use_target_home_and_config_selector(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer: str,
    config_home: str | None,
) -> None:
    controller_home = isolated_home / "controller"
    controller_home.mkdir()
    monkeypatch.setenv("HOME", str(controller_home))
    target_home = isolated_home / "target home"
    target_home.mkdir()
    upstream = target_home / ".omnigent" / "config.yaml"
    upstream.parent.mkdir()
    upstream.write_text("host:\n  name: upstream\n  host_id: upstream-id\n")
    target_config = target_home / (config_home or ".omnigent-mdsmithaustin")
    target_config.mkdir()
    config = target_config / "config.yaml"
    config.write_text("host:\n  name: previous\n  host_id: target-id\nuser_key: retained\n")
    target_env = os.environ.copy()
    target_env["HOME"] = str(target_home)
    target_env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + target_env["PATH"]
    if config_home:
        target_env["OMNIGENT_CONFIG_HOME"] = str(target_config)
    else:
        target_env.pop("OMNIGENT_CONFIG_HOME", None)
    name = "target 'quoted' \"name\" $HOME `literal` \\ slash"
    commands: list[str] = []
    if writer == "injected":
        commands.append(render_host_config_write_command({"server": "https://fork.example"}))
    else:
        launcher = SimpleNamespace(run=lambda _sandbox_id, command: commands.append(command))
        set_sandbox_host_name(launcher, "sandbox", name)
    command = commands[0]
    assert str(controller_home) not in command
    subprocess.run(["sh", "-c", command], env=target_env, check=True, capture_output=True)
    loaded = yaml.safe_load(config.read_text())
    assert loaded["user_key"] == "retained"
    assert loaded["host"]["host_id"] == "target-id"
    if writer == "injected":
        assert loaded["server"] == "https://fork.example"
        marker = json.loads((target_config / ".injected_host_config.json").read_text())
        assert marker == {"server": "https://fork.example"}
    else:
        assert loaded["host"]["name"] == name
    assert upstream.read_text() == "host:\n  name: upstream\n  host_id: upstream-id\n"
    assert list(controller_home.iterdir()) == []


@pytest.mark.parametrize("override", [False, True])
def test_dictation_downloader_matches_server_default_and_keeps_override(
    isolated_home: Path, override: bool
) -> None:
    root = (
        isolated_home / "selected models"
        if override
        else isolated_home / ".omnigent-mdsmithaustin" / "models" / "dictation"
    )
    for sub in ("asr", "punct"):
        (root / sub).mkdir(parents=True)
        (root / sub / "sentinel").write_text(sub)
    script = Path(__file__).resolve().parents[1] / "scripts" / "fetch-dictation-models.sh"
    env = os.environ.copy()
    env.pop("OMNIGENT_DICTATION_MODEL_ROOT", None)
    if override:
        env["OMNIGENT_DICTATION_MODEL_ROOT"] = str(root)
    result = subprocess.run(
        ["bash", str(script)], env=env, check=True, capture_output=True, text=True
    )
    assert f">> dictation models ready under {root}" in result.stdout
    assert "skipping streaming ASR" in result.stdout
    assert "skipping punctuation" in result.stdout
    assert (root / "asr" / "sentinel").read_text() == "asr"
    assert (root / "punct" / "sentinel").read_text() == "punct"
