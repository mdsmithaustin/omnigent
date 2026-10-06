import asyncio
import json

import pytest
from aiohttp import ClientSession, ClientTimeout, web

from scripts.verify_skill_command_recovery import requested_effect_marker, start_proxy


def test_gateway_selects_only_latest_user_command():
    marker = "EFFECT_a123"
    turn = {"role": "user", "content": [{"type": "text", "text": marker}]}
    trailer = {"role": "system", "content": "Fixture trailer"}
    tools = [{"name": "Bash"}]
    assert requested_effect_marker({"tools": tools, "messages": [turn, trailer]}) == marker
    assert requested_effect_marker({"tools": [], "messages": [turn, trailer]}) is None
    result = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call", "content": marker}],
    }
    assert requested_effect_marker({"tools": tools, "messages": [turn, result, trailer]}) is None
    assert (
        requested_effect_marker(
            {"tools": tools, "messages": [turn, {"role": "user", "content": "follow-up"}]}
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["reject", "lose_runner_reply"])
async def test_external_relay_records_attributed_faults(tmp_path, mode):
    claim = {"item_id": "a1" * 16, "fingerprint": "b2" * 32}
    public_body = {
        "queued": True,
        "item_id": claim["item_id"],
        "delivery": {
            "status": "accepted",
            "invocation_id": "c3" * 16,
            "fingerprint": claim["fingerprint"],
        },
    }
    observed = []
    connected = asyncio.Event()
    send = asyncio.Event()

    async def events(request):
        return web.json_response(public_body, status=202)

    async def tunnel(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        connected.set()
        await send.wait()
        await socket.send_json(
            {
                "kind": "request",
                "id": "request1",
                "method": "POST",
                "path": "/v1/sessions/x/events",
                "body": json.dumps(
                    {
                        "type": "message",
                        "content": [{"type": "input_text", "text": "EFFECT_a123"}],
                        "command_admission": claim,
                    }
                ),
            }
        )
        async for frame in socket:
            observed.append(json.loads(frame.data))
            if len(observed) == 3:
                await socket.send_json({"kind": "ping", "ts": 42})
                break
        await socket.close()
        return socket

    app = web.Application()
    app.router.add_post("/v1/sessions/x/events", events)
    app.router.add_get("/v1/runners/x/tunnel", tunnel)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    proxy, close = start_proxy(f"http://127.0.0.1:{port}", tmp_path)
    try:
        async with ClientSession() as client:
            (tmp_path / "fault.json").write_text(
                json.dumps({"mode": "lose_reply", "marker": "public-loss"})
            )
            with pytest.raises(TimeoutError):
                await client.post(
                    proxy + "/v1/sessions/x/events",
                    json={
                        "type": "slash_command",
                        "data": {"stable_id": "c3" * 16, "arguments": "public-loss"},
                    },
                    timeout=ClientTimeout(total=0.05),
                )
            (tmp_path / "fault.json").write_text(
                json.dumps({"mode": mode, "marker": "EFFECT_a123"})
            )
            async with client.ws_connect(proxy + "/v1/runners/x/tunnel") as socket:
                await asyncio.wait_for(connected.wait(), 3)
                send.set()
                if mode == "lose_runner_reply":
                    request = await socket.receive_json(timeout=3)
                    assert request["id"] == "request1"
                    await socket.send_json(
                        {"kind": "response.head", "id": "request1", "status": 202, "headers": []}
                    )
                    await socket.send_json(
                        {
                            "kind": "response.body",
                            "id": "request1",
                            "body": "{}",
                            "encoding": "utf-8",
                        }
                    )
                    await socket.send_json({"kind": "response.end", "id": "request1"})
                assert await socket.receive_json(timeout=3) == {"kind": "ping", "ts": 42}
            evidence = [
                json.loads(line)
                for line in (tmp_path / "fault-observed.jsonl").read_text().splitlines()
            ]
            assert evidence[0]["original_response"] == public_body
            assert evidence[0]["invocation_id"] == "c3" * 16
            assert evidence[0]["upstream_response_received"] is True
            assert evidence[0]["upstream_http_status"] == 202
            assert evidence[1]["claim"] == claim
            assert evidence[1]["request_id"] == "request1"
            assert observed[0]["status"] == (400 if mode == "reject" else 502)
            assert evidence[1]["forwarded_to_runner"] is (mode == "lose_runner_reply")
            if mode == "reject":
                assert json.loads(observed[1]["body"])["delivery"] == {
                    **claim,
                    "status": "rejected",
                }
                assert not (tmp_path / "forwards.jsonl").exists()
            else:
                assert evidence[1]["upstream_http_status"] == 202
                assert len((tmp_path / "forwards.jsonl").read_text().splitlines()) == 1
    finally:
        await asyncio.to_thread(close)
        await runner.cleanup()


@pytest.mark.parametrize("fault", ["none", "kill_failed", "detached_survivor", "observation_gap"])
def test_native_cleanup_requires_exact_close_and_detached_census(monkeypatch, fault):
    import subprocess
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import psutil

    from scripts.verify_skill_command_recovery import NativeOwnerCensus

    attribution = {
        "RECOVERY_PROOF_DIR": "/private/run",
        "CLAUDE_CONFIG_DIR": "/private/run/claude",
    }
    live = {101, 102, 103, 104}
    processes = {}
    for pid, born in [(101, 11), (102, 12), (103, 13), (104, 14)]:
        process = MagicMock(pid=pid)
        process.create_time.return_value = born
        process.cmdline.return_value = ["fixture", str(pid)]
        process.environ.return_value = attribution if pid in (103, 104) else {}
        process.children.return_value = [processes[103]] if 103 in processes else []
        process.is_running.side_effect = lambda pid=pid: pid in live
        process.status.return_value = psutil.STATUS_RUNNING
        process.info = {
            "pid": pid,
            "create_time": born,
            "uids": SimpleNamespace(real=__import__("os").getuid()),
        }
        processes[pid] = process
    processes[102].children.return_value = [processes[103]]
    monkeypatch.setattr(psutil, "Process", lambda pid: processes[pid])
    monkeypatch.setattr(
        psutil, "process_iter", lambda attrs: [processes[pid] for pid in sorted(live)]
    )
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda command, **kw: (
            "101\t102\t/private/run/tmux.sock"
            if "-t" in command
            else "101\t/private/run/tmux.sock"
        ),
    )

    def kill(command, **kwargs):
        assert command == ["tmux", "-S", "/private/run/tmux.sock", "kill-server"]
        live.clear()
        if fault == "detached_survivor":
            live.add(104)
        if fault == "observation_gap":
            processes[104].is_running.side_effect = psutil.AccessDenied(104)
        return subprocess.CompletedProcess(
            command,
            1 if fault == "kill_failed" else 0,
            stderr="injected close failure" if fault == "kill_failed" else "",
        )

    monkeypatch.setattr(subprocess, "run", kill)
    census = NativeOwnerCensus(attribution, started_at=10)
    census.capture_tmux(["tmux", "-S", "/private/run/tmux.sock"], "%1")
    census.close_tmux()
    summary = {"qualified": True}
    qualified = census.finish(summary, {})
    assert qualified is (fault == "none")
    assert summary["qualified"] is (fault == "none")
    assert {owner["pid"] for owner in summary["native_owners"]} == {101, 102, 103, 104}
    assert summary["owned_survivors"] == ([104] if fault == "detached_survivor" else [])
    if fault in ("kill_failed", "observation_gap"):
        assert any(
            "exited 1" in gap
            if fault == "kill_failed"
            else "Final process census failed for 104" in gap
            for gap in summary["cleanup_gaps"]
        )
