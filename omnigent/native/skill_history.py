from __future__ import annotations

from collections.abc import Sequence

from omnigent.util.json_types import JsonObject


def missing_skill_expansions(items: Sequence[JsonObject]) -> list[str]:
    """Find completed native commands with no subsequent CLI meta context."""
    pending: str | None = None
    missing: list[str] = []
    for item in items:
        if item.get("type") == "compaction":
            pending = None
            missing.clear()
        elif item.get("type") == "slash_command":
            command = item.get("native_invocation")
            pending = command if isinstance(command, str) else None
        elif item.get("type") == "message":
            if item.get("role") == "user":
                if item.get("is_meta") is True and not item.get("content"):
                    continue
                pending = None
            elif item.get("role") == "assistant" and pending is not None:
                missing.append(pending)
                pending = None
    return missing
