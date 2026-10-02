from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import tomllib

from omnigent.errors import OmnigentError
from omnigent.inner.codex_executor import codex_skill_sources, select_codex_skill_dirs
from omnigent.spec.codex_plugin_skills import discover_codex_plugin_skills
from omnigent.spec.parser import _discover_skills, _parse_skill
from omnigent.spec.skill_sources import SkillSourceContext, _read_json
from omnigent.spec.types import SkillSpec


def _has_plugin_namespace(path: Path) -> bool:
    for parent in path.parents:
        for directory in (".codex-plugin", ".claude-plugin"):
            manifest = _read_json(parent / directory / "plugin.json")
            if manifest and isinstance(manifest.get("name"), str) and manifest["name"]:
                return True
    return False


def _agent_skill_roots(ctx: SkillSourceContext) -> list[Path]:
    roots = [ctx.home / ".agents" / "skills", Path("/etc/codex/skills")]
    if ctx.roots:
        cwd = ctx.roots[0].resolve()
        for parent in (cwd, *cwd.parents):
            roots.append(parent / ".agents" / "skills")
            if (parent / ".git").exists():
                break
    return roots


def discover_native_skills(ctx: SkillSourceContext) -> list[SkillSpec]:
    sources = codex_skill_sources(ctx.bundle_dir, ctx.home, codex_home=ctx.codex_home)
    skills: list[SkillSpec] = []
    for name, path in select_codex_skill_dirs(ctx.skills_filter, sources).items():
        try:
            skills.append(replace(_parse_skill(path / "SKILL.md"), name=name))
        except (OmnigentError, OSError):
            continue
    if ctx.skills_filter != "none":
        for root in _agent_skill_roots(ctx):
            skills.extend(
                skill
                for skill in _discover_skills(root, skipped=[])
                if ctx.skills_filter == "all" or skill.name in ctx.skills_filter
            )
    skills.extend(
        discover_codex_plugin_skills(
            ctx.codex_home or ctx.home / ".codex",
            ctx.skills_filter,
            cwd=ctx.roots[0] if ctx.roots else None,
        )
    )
    return skills


def native_skill_invocation(
    selected: SkillSpec,
    context: SkillSourceContext,
    launch_args: tuple[str, ...] = (),
) -> str | None:
    """Only use a dollar command with one enabled, physically exposed file."""
    if selected.skill_dir is None or not (selected.skill_dir / "SKILL.md").is_file():
        return None
    try:
        native_name = _parse_skill(selected.skill_dir / "SKILL.md").name
    except (OmnigentError, OSError):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", native_name) or native_name.upper() in {
        "PATH",
        "HOME",
        "USER",
        "SHELL",
        "PWD",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "TERM",
        "XDG_CONFIG_HOME",
    }:
        return None
    if any(
        arg.split("=", 1)[0] in {"--config", "--profile"}
        or (arg.startswith(("-c", "-p")) and not arg.startswith("--"))
        for arg in launch_args
    ):
        return None
    codex_home = context.codex_home or context.home / ".codex"
    config_path = codex_home / "config.toml"
    try:
        config = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    except (OSError, ValueError):
        return None
    skills_config = config.get("skills", {})
    if not isinstance(skills_config, dict):
        return None
    entries = skills_config.get("config", [])
    if not isinstance(entries, list):
        return None
    disabled = {
        Path(entry["path"]).expanduser().resolve()
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("enabled") is False
        and isinstance(entry.get("path"), str)
    }
    candidates: set[Path] = set()
    for root in [
        codex_home / "skills",
        codex_home / "skills" / ".system",
        *_agent_skill_roots(context),
    ]:
        for skill in _discover_skills(root, skipped=[]):
            if skill.name == native_name and skill.skill_dir is not None:
                path = (skill.skill_dir / "SKILL.md").resolve()
                if path not in disabled:
                    if _has_plugin_namespace(path) or _has_plugin_namespace(
                        skill.skill_dir / "SKILL.md"
                    ):
                        return None
                    candidates.add(path)
    if candidates != {(selected.skill_dir / "SKILL.md").resolve()}:
        return None
    return f"${native_name}"
