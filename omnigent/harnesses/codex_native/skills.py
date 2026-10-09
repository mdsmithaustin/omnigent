from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import BinaryIO

import tomllib

from omnigent.errors import OmnigentError
from omnigent.inner.codex_executor import codex_skill_sources, select_codex_skill_dirs
from omnigent.spec.codex_plugin_skills import discover_codex_plugin_skills
from omnigent.spec.parser import _discover_skills, _parse_skill
from omnigent.spec.skill_sources import SkillSourceContext, _read_json
from omnigent.spec.types import SkillSpec
from omnigent.util.json_types import JsonObject


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


_SKILL_BLOCK_RE = re.compile(
    r"<skill>\s*<name>[^<>\n]+</name>\s*<path>[^<>\n]+</path>\s*[\s\S]*</skill>"
)
_SKILL_INSTRUCTIONS_KINDS = ["skills.selected_skill_instructions"]


def _find_codex_rollout(codex_home: Path, thread_id: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-fA-F-]+", thread_id):
        return None
    matches = [
        path
        for path in (codex_home / "sessions").glob(f"**/rollout-*-{thread_id}.jsonl")
        if path.is_file()
    ]
    return max(matches, key=lambda path: path.stat().st_mtime, default=None)


def _user_message_text(item: JsonObject) -> str:
    """
    Convert a Codex ``userMessage`` item into plain text.

    :param item: Codex ``userMessage`` item.
    :returns: Joined text content.
    """
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n\n".join(parts)


@dataclass
class _RolloutScan:
    """
    Parse position and the selected turn's findings for one rollout file.

    :param offset: Byte offset of the first unconsumed line.
    :param line_number: Zero-based index of that line.
    :param current_turn: Turn id of the latest turn-boundary row consumed.
    :param user_seen: Whether the current turn's user message row was consumed.
    :param turn_starts: First turn-boundary row of each turn as
        ``(offset, line_number)``, so a rescan can resume at a turn's start.
    :param turn_id: Turn the findings below belong to.
    :param user_texts: Texts of the turn's user messages.
    :param skills: Candidate skill rows of the turn, in file order.
    :param seen_ids: Row ids of the turn consumed so far; a ``before_id`` among
        them means the scan already passed its stop row.
    """

    offset: int = 0
    line_number: int = 0
    current_turn: object = None
    user_seen: bool = False
    turn_starts: dict[str, tuple[int, int]] = field(default_factory=dict)
    turn_id: str | None = None
    user_texts: set[str] = field(default_factory=set)
    skills: list[JsonObject] = field(default_factory=list)
    seen_ids: set[str] = field(default_factory=set)

    def fork(self) -> _RolloutScan:
        """Copy the mutable members so an unterminated tail row can be applied tentatively."""
        return replace(
            self,
            turn_starts=dict(self.turn_starts),
            user_texts=set(self.user_texts),
            skills=list(self.skills),
            seen_ids=set(self.seen_ids),
        )

    def begin_turn(self, turn_id: str) -> None:
        """Start collecting findings for *turn_id* from the current position."""
        self.turn_id = turn_id
        self.user_texts = set()
        self.skills = []
        self.seen_ids = set()

    def pending_skills(self) -> list[JsonObject]:
        """Return collected skill rows that were not typed by the user."""
        return [skill for skill in self.skills if _user_message_text(skill) not in self.user_texts]

    def apply(
        self, row: object, position: tuple[int, int], turn_id: str, before_id: object
    ) -> bool:
        """
        Fold one rollout row into the scan.

        :param row: Decoded JSON line, or ``None`` when it did not decode.
        :param position: ``(offset, line_number)`` of the row.
        :param turn_id: Turn being read.
        :param before_id: Item id at which to stop, or ``None`` to read the whole turn.
        :returns: ``True`` when the row ends the read.
        """
        if not isinstance(row, dict) or not isinstance(payload := row.get("payload"), dict):
            return False
        row_type = row.get("type")
        kind = payload.get("type")
        if row_type == "turn_context" or (row_type == "event_msg" and kind == "task_started"):
            next_turn = payload.get("turn_id")
            if next_turn != self.current_turn:
                self.user_seen = False
            if isinstance(next_turn, str):
                self.turn_starts.setdefault(next_turn, position)
            self.current_turn = next_turn
            return False
        if self.current_turn != turn_id:
            return False
        if row_type == "event_msg":
            event_turn = payload.get("turn_id")
            if event_turn is not None and event_turn != turn_id:
                return False
            event_item = payload.get("item")
            if kind == "user_message":
                raw_text = payload.get("message")
                if isinstance(raw_text, str):
                    self.user_texts.add(raw_text)
                    self.user_seen = True
            elif kind == "item_completed" and isinstance(event_item, dict):
                if event_item.get("type") in {"UserMessage", "userMessage"}:
                    self.user_texts.add(_user_message_text(event_item))
                    self.user_seen = True
                if before_id is not None and event_item.get("id") == before_id:
                    return True
                self._see_ids(event_item.get("id"))
            elif kind in {"task_complete", "turn_aborted"}:
                return True
            return False
        if row_type != "response_item":
            return False
        if before_id is not None and before_id in (payload.get("id"), payload.get("call_id")):
            return True
        self._see_ids(payload.get("id"), payload.get("call_id"))
        if not self.user_seen or kind != "message" or payload.get("role") != "user":
            return False
        metadata = payload.get("internal_chat_message_metadata_passthrough")
        if isinstance(metadata, dict):
            if metadata.get("turn_id", turn_id) != turn_id:
                return False
            kinds = metadata.get("content_item_kinds")
            if kinds is not None and kinds != _SKILL_INSTRUCTIONS_KINDS:
                return False
        content = payload.get("content")
        if not isinstance(content, list) or not content:
            return False
        if not all(
            isinstance(block, dict)
            and block.get("type") == "input_text"
            and isinstance(text := block.get("text"), str)
            and _SKILL_BLOCK_RE.fullmatch(text.strip())
            for block in content
        ):
            return False
        skill = dict(payload)
        if not isinstance(skill.get("id"), str) or not skill["id"]:
            skill["id"] = f"rollout-skill-{position[1]}"
        self.skills.append(skill)
        return False

    def _see_ids(self, *ids: object) -> None:
        self.seen_ids.update(item_id for item_id in ids if isinstance(item_id, str))


class RolloutSkillReader:
    """
    Reads the skill-instruction rows Codex appends to one thread's rollout.

    For an append-only rollout, successive reads of the same turn resume at
    the first unconsumed line, and the resolved rollout path is kept. Reading
    an earlier stop row or a previously indexed turn rescans that turn. File
    truncation or replacement resets the scan; in-place edits to consumed
    bytes are not detected.

    :param codex_home: The session's private ``CODEX_HOME``.
    :param thread_id: Codex thread id, e.g. ``"019e96aa-0be2-7343-8d3b-6f914d60936b"``.
    """

    def __init__(self, codex_home: Path, thread_id: str) -> None:
        self._codex_home = codex_home
        self._thread_id = thread_id
        self._lock = threading.Lock()
        self._path: Path | None = None
        self._file_id: tuple[int, int] | None = None
        self._size = 0
        self._scan = _RolloutScan()

    def read_turn_skills(self, turn_id: str, before_id: object) -> list[JsonObject]:
        """
        Return the turn's native skill rows that precede *before_id*.

        :param turn_id: Codex turn id, e.g. ``"turn-1"``.
        :param before_id: Item id (or call id) at which to stop, or ``None`` to
            read through the end of the turn.
        :returns: Skill ``response_item`` payloads, each with an ``id``; empty
            when no rollout exists yet.
        :raises OSError: If the rollout cannot be read.
        """
        with self._lock:
            stream = self._open()
            if stream is None:
                return []
            with stream:
                self._reset_if_replaced(stream)
                self._select_turn(turn_id, before_id)
                return self._advance(stream, turn_id, before_id)

    def _open(self) -> BinaryIO | None:
        if self._path is not None:
            try:
                return self._path.open("rb")
            except FileNotFoundError:
                self._path = None
        self._path = _find_codex_rollout(self._codex_home, self._thread_id)
        return None if self._path is None else self._path.open("rb")

    def _reset_if_replaced(self, stream: BinaryIO) -> None:
        stat = os.fstat(stream.fileno())
        file_id = (stat.st_dev, stat.st_ino)
        if file_id != self._file_id or stat.st_size < self._size:
            self._file_id = file_id
            self._scan = _RolloutScan()
        self._size = stat.st_size

    def _select_turn(self, turn_id: str, before_id: object) -> None:
        scan = self._scan
        if scan.turn_id == turn_id:
            passed_stop = before_id is not None and (
                not isinstance(before_id, str) or before_id in scan.seen_ids
            )
            if not passed_stop:
                return
        elif turn_id not in scan.turn_starts:
            scan.begin_turn(turn_id)
            return
        offset, line_number = scan.turn_starts.get(turn_id, (0, 0))
        self._scan = _RolloutScan(
            offset=offset, line_number=line_number, turn_starts=scan.turn_starts
        )
        self._scan.begin_turn(turn_id)

    def _advance(self, stream: BinaryIO, turn_id: str, before_id: object) -> list[JsonObject]:
        scan = self._scan
        stream.seek(scan.offset)
        offset, line_number = scan.offset, scan.line_number
        for line in stream:
            terminated = line.endswith(b"\n")
            # An unterminated tail may still be growing, so it is never committed.
            target = scan if terminated else scan.fork()
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                row = None
            if target.apply(row, (offset, line_number), turn_id, before_id):
                return target.pending_skills()
            offset += len(line)
            line_number += 1
            if terminated:
                scan.offset, scan.line_number = offset, line_number
            else:
                return target.pending_skills()
        return scan.pending_skills()
