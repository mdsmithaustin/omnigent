from __future__ import annotations

import json
import os
from pathlib import Path

from omnigent.harnesses.codex_native.skills import RolloutSkillReader

_THREAD = "0192abcd-0000-7000-8000-000000000001"


def _skill_text(body: str) -> str:
    return f"<skill>\n<name>orchard</name>\n<path>/x/SKILL.md</path>\n{body}\n</skill>"


def _skill_row(item_id: str, body: str) -> dict:
    block = {"type": "input_text", "text": _skill_text(body)}
    return {
        "type": "response_item",
        "payload": {"type": "message", "id": item_id, "role": "user", "content": [block]},
    }


def _turn_rows(turn: str) -> list[dict]:
    return [
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "$orchard hi"}},
    ]


def _reply_row(item_id: str) -> dict:
    return {"type": "response_item", "payload": {"type": "function_call", "call_id": item_id}}


def _append(path: Path, rows: list[dict]) -> None:
    with path.open("a") as stream:
        stream.write("".join(json.dumps(row) + "\n" for row in rows))


def _ids(skills: list[dict]) -> list[str]:
    return [skill["id"] for skill in skills]


def _rollout(tmp_path: Path) -> tuple[Path, RolloutSkillReader]:
    path = (
        tmp_path
        / "sessions"
        / "2026"
        / "10"
        / "02"
        / f"rollout-2026-10-02T00-00-00-{_THREAD}.jsonl"
    )
    path.parent.mkdir(parents=True)
    return path, RolloutSkillReader(tmp_path, _THREAD)


def test_appended_chunks_are_read_incrementally(tmp_path: Path) -> None:
    path, reader = _rollout(tmp_path)
    _append(path, _turn_rows("t1"))
    skill_offset = path.stat().st_size
    _append(path, [_skill_row("skill-a", "A")])
    consumed_size = path.stat().st_size - skill_offset
    assert _ids(reader.read_turn_skills("t1", None)) == ["skill-a"]

    with path.open("r+b") as stream:
        stream.seek(skill_offset)
        stream.write(b"!" * (consumed_size - 1) + b"\n")
    _append(path, [_reply_row("call-1"), _skill_row("skill-b", "B")])
    assert _ids(reader.read_turn_skills("t1", None)) == ["skill-a", "skill-b"]


def test_stop_row_already_passed_rescans_the_turn(tmp_path: Path) -> None:
    path, reader = _rollout(tmp_path)
    _append(
        path,
        [
            *_turn_rows("t1"),
            _skill_row("skill-a", "A"),
            _reply_row("call-1"),
            _skill_row("skill-b", "B"),
        ],
    )
    assert _ids(reader.read_turn_skills("t1", None)) == ["skill-a", "skill-b"]
    assert _ids(reader.read_turn_skills("t1", "call-1")) == ["skill-a"]
    assert _ids(reader.read_turn_skills("t1", None)) == ["skill-a", "skill-b"]


def test_next_turn_reads_only_its_own_rows(tmp_path: Path) -> None:
    path, reader = _rollout(tmp_path)
    _append(path, [*_turn_rows("t1"), _skill_row("skill-a", "A")])
    assert _ids(reader.read_turn_skills("t1", None)) == ["skill-a"]

    _append(path, [*_turn_rows("t2"), _skill_row("skill-c", "C")])
    assert _ids(reader.read_turn_skills("t2", None)) == ["skill-c"]
    assert _ids(reader.read_turn_skills("t1", None)) == ["skill-a"]


def test_truncated_rollout_is_reparsed_from_the_start(tmp_path: Path) -> None:
    path, reader = _rollout(tmp_path)
    _append(path, [*_turn_rows("t1"), _skill_row("skill-a", "A"), _skill_row("skill-b", "B")])
    assert _ids(reader.read_turn_skills("t1", None)) == ["skill-a", "skill-b"]

    path.write_text("")
    _append(path, [*_turn_rows("t1"), _skill_row("skill-z", "Z")])
    assert _ids(reader.read_turn_skills("t1", None)) == ["skill-z"]


def test_atomically_replaced_larger_rollout_is_reparsed_from_the_start(tmp_path: Path) -> None:
    path, reader = _rollout(tmp_path)
    _append(path, [*_turn_rows("t1"), _skill_row("skill-a", "A")])
    assert _ids(reader.read_turn_skills("t1", None)) == ["skill-a"]

    replacement = path.with_suffix(".replacement")
    _append(
        replacement,
        [*_turn_rows("t1"), _skill_row("skill-z", "Z"), _skill_row("skill-y", "Y")],
    )
    assert replacement.stat().st_size > path.stat().st_size
    os.replace(replacement, path)

    assert _ids(reader.read_turn_skills("t1", None)) == ["skill-z", "skill-y"]


def test_missing_rollout_or_unsafe_thread_id_yields_no_skills(tmp_path: Path) -> None:
    assert RolloutSkillReader(tmp_path, _THREAD).read_turn_skills("t1", None) == []

    path, reader = _rollout(tmp_path)
    _append(path, [*_turn_rows("t1"), _skill_row("skill-a", "A")])
    assert _ids(reader.read_turn_skills("t1", None)) == ["skill-a"]
    assert RolloutSkillReader(tmp_path, "*").read_turn_skills("t1", None) == []
