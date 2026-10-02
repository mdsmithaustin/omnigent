from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.claude_native.skills import native_skill_invocation as claude_command
from omnigent.harnesses.codex_native.skills import native_skill_invocation as codex_command
from omnigent.spec.parser import _parse_skill
from omnigent.spec.skill_sources import SkillSourceContext
from tests.runner.test_skills import _make_app, _skill_md


def _skill(root: Path, folder: str = "review", name: str = "review"):
    path = root / folder / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(_skill_md(name, "Read the sentinel."))
    return _parse_skill(path)


@pytest.mark.parametrize(
    "harness,folder,expected",
    [
        ("claude-native", ".claude", {"native_invocation": "/review first request"}),
        ("claude-sdk", ".claude", None),
        ("codex-native", ".agents", {"native_invocation": "$review first request"}),
    ],
)
@pytest.mark.asyncio
async def test_runner_resolution_uses_native_capability(
    tmp_path, monkeypatch, harness, folder, expected
):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    workspace = tmp_path / "workspace"
    _skill(workspace / folder / "skills")
    from omnigent.harnesses.codex_native import bridge

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridge")
    if harness == "codex-native":
        bridge.write_bridge_state(
            bridge.bridge_dir_for_bridge_id("conv_native"),
            bridge.CodexNativeBridgeState(
                session_id="conv_native",
                socket_path="ws://127.0.0.1:1",
                thread_id="native_test",
                codex_home=str(home / ".codex"),
            ),
        )
    app = _make_app(tmp_path / "bundle", [], "all", workspace=workspace, harness=harness)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        response = await client.post(
            "/v1/sessions/conv_native/skills/resolve",
            json={"name": "review", "arguments": "first request"},
        )
    assert response.status_code == 200, response.text
    if expected is not None:
        assert response.json() == expected
    else:
        assert "body for review" in response.json()["meta_text"]
        assert "first request" in response.json()["meta_text"]


@pytest.mark.parametrize(
    "vendor,folder,command", [("claude", ".claude", "/review"), ("codex", ".agents", "$review")]
)
def test_exact_file_selection_rejects_ambiguity_and_accepts_symlink(
    tmp_path, vendor, folder, command
):
    home = tmp_path / "home"
    cwd = tmp_path / "workspace"
    selected = _skill(cwd / folder / "skills")
    ctx = SkillSourceContext(roots=(cwd,), home=home, skills_filter="all", bundle_dir=None)
    resolve = claude_command if vendor == "claude" else codex_command
    assert resolve(selected, ctx) == command
    shadow = _skill(home / folder / "skills")
    assert resolve(selected, ctx) is None
    (shadow.skill_dir / "SKILL.md").unlink()
    shadow.skill_dir.rmdir()
    shadow.skill_dir.symlink_to(selected.skill_dir, target_is_directory=True)
    assert resolve(selected, ctx) == command


def test_claude_bundle_uses_manifest_namespace_and_rejects_wrong_root(tmp_path):
    bundle = tmp_path / "bundle"
    selected = _skill(bundle / "skills")
    ctx = SkillSourceContext(
        roots=(tmp_path / "workspace",),
        home=tmp_path / "home",
        skills_filter="none",
        bundle_dir=bundle,
    )
    assert claude_command(selected, ctx) is None
    manifest = bundle / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir()
    manifest.write_text('{"name":"actual-plugin"}')
    assert claude_command(selected, ctx) == "/actual-plugin:review"
    outside = _skill(tmp_path / "elsewhere")
    assert claude_command(outside, ctx) is None
    (selected.skill_dir / "SKILL.md").unlink()
    assert claude_command(selected, ctx) is None


def test_codex_requires_materialized_bundle_and_enabled_name(tmp_path):
    selected = _skill(tmp_path / "bundle" / "skills", folder="different-folder")
    codex_home = tmp_path / "codex-home"
    ctx = SkillSourceContext(
        roots=(tmp_path / "workspace",),
        home=tmp_path / "home",
        skills_filter="all",
        bundle_dir=tmp_path / "bundle",
        codex_home=codex_home,
    )
    assert codex_command(selected, ctx) is None
    skills_dir = codex_home / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "different-folder").symlink_to(selected.skill_dir, target_is_directory=True)
    assert codex_command(selected, ctx) == "$review"
    (codex_home / "config.toml").write_text(
        f'[[skills.config]]\npath = "{selected.skill_dir / "SKILL.md"}"\nenabled = false\n'
    )
    assert codex_command(selected, ctx) is None


def test_missing_native_expansion_is_reported_only_for_completed_active_history():
    from omnigent.native.skill_history import missing_skill_expansions

    command = {"type": "slash_command", "native_invocation": "$review first"}
    reply = {"type": "message", "role": "assistant", "content": []}
    expansion = {
        "type": "message",
        "role": "user",
        "is_meta": True,
        "content": [{"type": "input_text", "text": "Original skill instructions"}],
    }
    assert missing_skill_expansions([command, reply]) == ["$review first"]
    assert missing_skill_expansions([command, expansion, reply]) == []
    assert missing_skill_expansions([command]) == []
    assert missing_skill_expansions([command, reply, {"type": "compaction"}]) == []
    receipt = {"type": "message", "role": "user", "is_meta": True, "content": []}
    assert missing_skill_expansions([command, receipt, reply]) == ["$review first"]


@pytest.mark.parametrize("name", ["café-review", "HOME", "Path", "TMPDIR"])
def test_codex_rejects_names_not_loaded_by_dollar_mentions(tmp_path, name):
    home = tmp_path / "home"
    selected = _skill(home / ".agents" / "skills", name=name)
    ctx = SkillSourceContext(
        roots=(tmp_path / "work",), home=home, skills_filter="all", bundle_dir=None
    )
    assert codex_command(selected, ctx) is None


def test_codex_plain_skill_remains_native_with_installed_plugins(tmp_path):
    home = tmp_path / "home"
    selected = _skill(home / ".agents" / "skills")
    (home / ".codex" / "plugins" / "cache").mkdir(parents=True)
    ctx = SkillSourceContext(
        roots=(tmp_path / "work",), home=home, skills_filter="all", bundle_dir=None
    )
    assert codex_command(selected, ctx) == "$review"


@pytest.mark.parametrize("manifest_dir", [".claude-plugin", ".codex-plugin"])
def test_codex_staged_plugin_skill_keeps_paste_when_name_is_qualified(tmp_path, manifest_dir):
    bundle = tmp_path / "bundle"
    selected = _skill(bundle / "skills")
    manifest = bundle / manifest_dir / "plugin.json"
    manifest.parent.mkdir()
    manifest.write_text('{"name":"orchard-plugin"}')
    home = tmp_path / "home"
    staged = home / ".codex" / "skills" / "review"
    staged.parent.mkdir(parents=True)
    staged.symlink_to(selected.skill_dir, target_is_directory=True)
    ctx = SkillSourceContext(
        roots=(tmp_path / "work",), home=home, skills_filter="all", bundle_dir=bundle
    )
    assert codex_command(selected, ctx) is None


@pytest.mark.parametrize("flag", ["--safe-mode", "--plugin-url", "--bare"])
def test_claude_declines_customization_disabling_or_extra_plugin_flags(tmp_path, flag):
    home = tmp_path / "home"
    selected = _skill(home / ".claude" / "skills")
    ctx = SkillSourceContext(
        roots=(tmp_path / "work",), home=home, skills_filter="all", bundle_dir=None
    )
    assert claude_command(selected, ctx, (flag,)) is None


@pytest.mark.asyncio
async def test_codex_first_skill_uses_launch_home_before_thread_bridge_exists(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from omnigent.harnesses.codex_native import bridge

    home = tmp_path / "home"
    selected = _skill(home / ".agents" / "skills")
    launch_home = tmp_path / "private-codex-home"
    launch_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridge")
    app = _make_app(
        tmp_path / "bundle", [], "all", workspace=tmp_path / "work", harness="codex-native"
    )
    instance = SimpleNamespace(
        env={"CODEX_HOME": str(launch_home)}, is_alive=AsyncMock(return_value=True)
    )
    registry = SimpleNamespace(get=lambda *_: instance)
    monkeypatch.setattr(app.state.session_resource_registry, "_terminal_registry", registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        first = await client.post("/v1/sessions/conv_cold/skills/resolve", json={"name": "review"})
        assert first.status_code == 200, first.text
        assert first.json() == {"native_invocation": "$review"}
        (launch_home / "config.toml").write_text(
            f'[[skills.config]]\npath = "{selected.skill_dir / "SKILL.md"}"\nenabled = false\n'
        )
        disabled = await client.post(
            "/v1/sessions/conv_cold/skills/resolve", json={"name": "review"}
        )
        assert disabled.status_code == 200, disabled.text
        assert "meta_text" in disabled.json()


def test_empty_native_receipt_does_not_enter_runner_or_runtime_prompt():
    from omnigent.entities.conversation import ConversationItem, MessageData
    from omnigent.runner.session_history import build_session_history
    from omnigent.runtime.prompt import history_to_input_items

    receipt = ConversationItem(
        id="receipt",
        type="message",
        status="completed",
        response_id="turn",
        created_at=0,
        data=MessageData(role="user", content=[], is_meta=True),
    )
    assert history_to_input_items([receipt]) == []

    async def persist(*_):
        raise AssertionError("conversion must not persist items")

    history = build_session_history(
        _background_tasks=set(),
        _last_server_item_id={},
        _persist_cancellation_items=persist,
        _session_histories={},
        _session_spec_cache={},
        server_client=httpx.AsyncClient(),
    )
    assert history.convert_raw_items_to_input([receipt.to_api_dict()]) == []


@pytest.mark.parametrize("name", ["help", "compact", "fork"])
def test_claude_builtin_command_cannot_be_selected_as_a_native_skill(tmp_path, name):
    cwd = tmp_path / "workspace"
    selected = _skill(cwd / ".claude" / "skills", folder=name, name=name)
    ctx = SkillSourceContext(
        roots=(cwd,), home=tmp_path / "home", skills_filter="all", bundle_dir=None
    )
    ordinary = _skill(cwd / ".claude" / "skills", folder="ordinary", name="ordinary")
    assert claude_command(ordinary, ctx) == "/ordinary"
    assert claude_command(selected, ctx) is None


@pytest.mark.parametrize("scope", ["user", "project", "local"])
def test_claude_disabled_skill_override_keeps_paste_path(tmp_path, scope):
    cwd = tmp_path / "workspace"
    home = tmp_path / "home"
    selected = _skill(cwd / ".claude" / "skills")
    ctx = SkillSourceContext(roots=(cwd,), home=home, skills_filter="all", bundle_dir=None)
    directory = home / ".claude" if scope == "user" else cwd / ".claude"
    directory.mkdir(parents=True, exist_ok=True)
    settings = directory / ("settings.local.json" if scope == "local" else "settings.json")
    settings.write_text('{"skillOverrides":{"review":"off"}}')
    assert claude_command(selected, ctx) is None
    settings.write_text('{"skillOverrides":{"review":"user-invocable-only"}}')
    assert claude_command(selected, ctx) == "/review"


def test_claude_qualified_plugin_name_is_not_prefixed_twice(tmp_path):
    bundle = tmp_path / "bundle"
    selected = _skill(bundle / "skills", name="actual-plugin:review")
    ctx = SkillSourceContext(
        roots=(tmp_path / "workspace",),
        home=tmp_path / "home",
        skills_filter="none",
        bundle_dir=bundle,
    )
    manifest = bundle / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir()
    manifest.write_text('{"name":"actual-plugin"}')
    assert claude_command(selected, ctx) == "/actual-plugin:review"


def test_claude_directory_command_cannot_select_another_frontmatter_alias(tmp_path):
    cwd = tmp_path / "workspace"
    root = cwd / ".claude" / "skills"
    selected = _skill(root, folder="alpha", name="beta")
    actual = _skill(root, folder="beta", name="gamma")
    ctx = SkillSourceContext(
        roots=(cwd,), home=tmp_path / "home", skills_filter="all", bundle_dir=None
    )
    assert claude_command(selected, ctx) is None
    assert claude_command(actual, ctx) is None
