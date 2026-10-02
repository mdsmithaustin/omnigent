from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from omnigent.spec.parser import _discover_skills
from omnigent.spec.skill_sources import (
    SkillSourceContext,
    _claude_user_dir,
    _enabled_plugin_keys,
    _enabled_plugin_settings_files,
    _plugin_install_paths,
    _read_json,
)
from omnigent.spec.types import SkillSpec


def native_skill_invocation(
    selected: SkillSpec,
    context: SkillSourceContext,
    launch_args: tuple[str, ...] = (),
) -> str | None:
    """Return a command only when its sole candidate is the selected file."""
    if selected.skill_dir is None or not (selected.skill_dir / "SKILL.md").is_file():
        return None
    if any(
        arg.split("=", 1)[0]
        in {
            "--settings",
            "--setting-sources",
            "--plugin-dir",
            "--plugin-url",
            "--add-dir",
            "--disable-slash-commands",
            "--safe-mode",
            "--bare",
        }
        for arg in launch_args
    ):
        return None
    ctx = replace(context, is_native=True)
    candidates: list[tuple[str, Path]] = []
    if ctx.skills_filter != "none":
        roots: set[Path] = {_claude_user_dir(ctx) / "skills"}
        if ctx.roots:
            cwd = ctx.roots[0].resolve()
            roots.update(parent / ".claude" / "skills" for parent in (cwd, *cwd.parents))
        for root in roots:
            for skill in _discover_skills(root, skipped=[]):
                if skill.skill_dir is not None and skill.user_invocable:
                    candidates.append((skill.skill_dir.name, skill.skill_dir / "SKILL.md"))

    plugins = list(_plugin_install_paths(ctx, _enabled_plugin_keys(ctx)).values())
    if ctx.skills_filter == "none":
        plugins = []
    if ctx.bundle_dir is not None and (ctx.bundle_dir / "skills").is_dir():
        plugins.append(ctx.bundle_dir)
    for plugin in plugins:
        manifest = _read_json(plugin / ".claude-plugin" / "plugin.json")
        namespace = manifest.get("name") if manifest else None
        if not isinstance(namespace, str) or not namespace:
            continue
        for skill in _discover_skills(plugin / "skills", skipped=[]):
            if skill.skill_dir is not None and skill.user_invocable:
                command = (
                    skill.name
                    if skill.name.startswith(f"{namespace}:")
                    else f"{namespace}:{skill.name}"
                )
                candidates.append((command, skill.skill_dir / "SKILL.md"))

    selected_path = (selected.skill_dir / "SKILL.md").resolve()
    if ":" not in selected.name and selected.name != selected.skill_dir.name:
        return None
    commands = {name for name, path in candidates if path.resolve() == selected_path}
    if selected.name in commands:
        commands = {selected.name}
    if len(commands) != 1:
        return None
    command = commands.pop()
    if ":" not in command:
        overrides: dict[str, object] = {}
        for settings in _enabled_plugin_settings_files(ctx):
            data = _read_json(settings)
            if settings.exists() and data is None:
                return None
            if data is not None:
                configured = data.get("skillOverrides", {})
                if not isinstance(configured, dict):
                    return None
                overrides.update(configured)
        if overrides.get(command) == "off":
            return None
    from omnigent.harnesses.claude_native.bridge import (
        _CLAUDE_CLI_DROPPED_COMMANDS,
        _CLAUDE_NATIVE_ALLOWED_USER_SLASH_COMMANDS,
    )

    if command in _CLAUDE_CLI_DROPPED_COMMANDS | _CLAUDE_NATIVE_ALLOWED_USER_SLASH_COMMANDS:
        return None
    paths = {path.resolve() for name, path in candidates if name == command}
    if paths != {selected_path}:
        return None
    return f"/{command}"
