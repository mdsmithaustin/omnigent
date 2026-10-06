import re
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from omnigent.repl._repl import _SessionsChatReplAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unknown", "rejected", "timeout"])
async def test_skill_admission_does_not_become_native_completion(monkeypatch, outcome):
    client = MagicMock()
    client._base_url = "http://skill-recovery.test"
    adapter = _SessionsChatReplAdapter(client, "agent", session_id="ab" * 16, attach_only=True)
    monkeypatch.setattr(adapter, "_ensure_session", AsyncMock(return_value="ab" * 16))
    monkeypatch.setattr(adapter, "_recover_runner_if_needed", AsyncMock())
    monkeypatch.setattr(adapter, "_bind_runner_if_needed", AsyncMock())
    if outcome == "timeout":
        client.sessions.post_event = AsyncMock(side_effect=httpx.ReadTimeout("lost response"))
        error = httpx.ReadTimeout
    else:

        async def reply(_session_id, payload):
            return {
                "queued": False,
                "delivery": {
                    "status": outcome,
                    "invocation_id": payload["data"]["stable_id"],
                    "fingerprint": "cd" * 32,
                },
            }

        client.sessions.post_event = AsyncMock(side_effect=reply)
        error = RuntimeError
    events = []
    with pytest.raises(error):
        async for event in adapter.send_skill_slash_command("review", "hello"):
            events.append(event)
    payload = client.sessions.post_event.call_args.args[1]
    identity = payload["data"]["stable_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", identity)
    assert events == []
    assert adapter._is_streaming is False
    from omnigent.repl._skill_commands import SkillCommandJournal

    entries = SkillCommandJournal(client._base_url, "ab" * 16).submissions()
    saved = next(entry for entry in entries if entry.event.data.stable_id == identity)
    assert saved.event.model_dump(exclude_none=True) == payload
    assert (saved.delivery.status if saved.delivery else "unknown") == (
        "unknown" if outcome == "timeout" else outcome
    )
    client.sessions.post_event.assert_awaited_once()


@pytest.mark.parametrize(
    ("status", "historical", "label"),
    [
        ("unknown", False, "Admission unknown"),
        ("accepted", False, "Admitted; completion not confirmed"),
        ("rejected", False, "Rejected before admission"),
        ("accepted", True, "Historical copy"),
    ],
)
def test_history_shows_admission_and_identity(status, historical, label):
    from omnigent.repl._repl import _render_slash_command_history_item

    host = MagicMock()
    _render_slash_command_history_item(
        {
            "type": "slash_command",
            "name": "review",
            "arguments": "hello",
            "delivery": {
                "invocation_id": "ab" * 16,
                "fingerprint": "cd" * 32,
                "status": status,
                "historical": historical,
            },
        },
        host,
        MagicMock(muted="dim"),
    )
    rendered = "\n".join(str(call.args[0]) for call in host.output.call_args_list)
    assert label in rendered
    assert "ab" * 16 in rendered


@pytest.mark.asyncio
async def test_repl_explicit_recovery_checks_saved_identity_without_sending():
    from omnigent.repl._repl import COMMANDS

    client = MagicMock()
    client._base_url = "http://skill-recovery.test"
    client.sessions.list_items = AsyncMock(
        return_value=[
            {
                "id": "ef" * 16,
                "type": "slash_command",
                "name": "review",
                "arguments": "hello",
                "delivery": {
                    "invocation_id": "ab" * 16,
                    "fingerprint": "cd" * 32,
                    "status": "unknown",
                },
            }
        ]
    )
    client.sessions.post_event = AsyncMock()
    host = MagicMock()
    adapter = _SessionsChatReplAdapter(client, "agent", session_id="12" * 16, attach_only=True)
    assert "/recover-skill" in COMMANDS
    await COMMANDS["/recover-skill"][1]("ab" * 16, adapter, client, host, MagicMock(muted="dim"))
    rendered = "\n".join(str(call.args[0]) for call in host.output.call_args_list)
    assert "Admission unknown" in rendered
    assert "ab" * 16 in rendered
    client.sessions.post_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_repl_restart_recovers_original_submission_with_no_saved_claim(
    tmp_path, monkeypatch
):
    from omnigent.repl._repl import COMMANDS

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    client = MagicMock()
    client._base_url = "http://skill-recovery.test"
    client.sessions.post_event = AsyncMock(side_effect=httpx.ReadTimeout("lost response"))
    client.sessions.list_items = AsyncMock(return_value=[])
    original = _SessionsChatReplAdapter(client, "agent", session_id="ab" * 16, attach_only=True)
    monkeypatch.setattr(original, "_ensure_session", AsyncMock(return_value="ab" * 16))
    monkeypatch.setattr(original, "_recover_runner_if_needed", AsyncMock())
    monkeypatch.setattr(original, "_bind_runner_if_needed", AsyncMock())
    with pytest.raises(httpx.ReadTimeout):
        async for _ in original.send_skill_slash_command("review", "original arguments"):
            pass
    payload = client.sessions.post_event.call_args.args[1]
    restarted = _SessionsChatReplAdapter(client, "agent", session_id="ab" * 16, attach_only=True)
    host = MagicMock()
    await COMMANDS["/recover-skill"][1]("", restarted, client, host, MagicMock(muted="dim"))
    rendered = "\n".join(str(call.args[0]) for call in host.output.call_args_list)
    assert "/review original arguments" in rendered
    assert payload["data"]["stable_id"] in rendered
    assert "Admission unknown" in rendered
    assert "Nothing was resent" in rendered
    client.sessions.post_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_repl_historical_recovery_is_display_only(tmp_path, monkeypatch):
    from omnigent.repl._repl import COMMANDS

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    client = MagicMock()
    client._base_url = "http://skill-recovery.test"
    client.sessions.list_items = AsyncMock(
        return_value=[
            {
                "id": "claim",
                "type": "slash_command",
                "name": "review",
                "arguments": "hello",
                "delivery": {
                    "invocation_id": "ab" * 16,
                    "fingerprint": "cd" * 32,
                    "status": "accepted",
                    "historical": True,
                },
            }
        ]
    )
    client.sessions.post_event = AsyncMock()
    host = MagicMock()
    adapter = _SessionsChatReplAdapter(client, "agent", session_id="12" * 16, attach_only=True)
    await COMMANDS["/recover-skill"][1]("ab" * 16, adapter, client, host, MagicMock(muted="dim"))
    rendered = "\n".join(str(call.args[0]) for call in host.output.call_args_list)
    assert "Historical copy" in rendered
    assert "Historical copies cannot be recovered" in rendered
    client.sessions.post_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_repl_history_failure_is_not_a_missing_claim(tmp_path, monkeypatch):
    from omnigent.repl._repl import COMMANDS

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    client = MagicMock()
    client._base_url = "http://skill-recovery.test"
    client.sessions.list_items = AsyncMock(side_effect=httpx.ReadTimeout("history unavailable"))
    client.sessions.post_event = AsyncMock()
    host = MagicMock()
    adapter = _SessionsChatReplAdapter(client, "agent", session_id="12" * 16, attach_only=True)
    await COMMANDS["/recover-skill"][1]("ab" * 16, adapter, client, host, MagicMock(muted="dim"))
    rendered = "\n".join(str(call.args[0]) for call in host.output.call_args_list)
    assert "Could not check skill admission: history unavailable" in rendered
    assert "No saved claim" not in rendered
    client.sessions.post_event.assert_not_awaited()
