from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.claude_native import bridge as claude_bridge
from omnigent.harnesses.codex_native import bridge as codex_bridge
from omnigent.harnesses.codex_native.main import _find_codex_rollout
from tests.e2e.test_host_claude_native_e2e import _workspace_trusted_in_claude_config
from tests.e2e.test_host_codex_native_e2e import (
    _assistant_text,
    _online_host_id,
    _poll_for_assistant_marker,
    _spawn_host_daemon,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_NATIVE_SKILLS") != "1",
    reason="Set OMNIGENT_E2E_NATIVE_SKILLS=1 for real authenticated native skill turns",
)


def _native_usage(transcript: Path, harness: str) -> dict[str, dict]:
    calls = {}
    for line_number, line in enumerate(transcript.read_text().splitlines(), 1):
        record = json.loads(line)
        payload = record.get("message" if harness == "claude" else "payload", {})
        if harness == "claude" and record.get("type") == "assistant" and payload.get("usage"):
            usage = payload["usage"]
            calls[payload["id"]] = {"line": line_number, **usage}
        elif harness == "codex" and payload.get("type") == "token_count" and payload.get("info"):
            info = payload["info"]
            key = json.dumps(info["total_token_usage"], sort_keys=True)
            calls[key] = {"line": line_number, **info["last_token_usage"]}
    return calls


def _summarize_usage(calls: list[dict], harness: str) -> dict:
    assert calls, "Native transcript contains no new API usage for the invocation"
    output = sum(call.get("output_tokens", 0) for call in calls)
    if harness == "claude":
        read = sum(call.get("cache_read_input_tokens", 0) for call in calls)
        created = sum(call.get("cache_creation_input_tokens", 0) for call in calls)
        uncached = sum(call.get("input_tokens", 0) for call in calls)
        processed = read + created + uncached
    else:
        processed = sum(call.get("input_tokens", 0) for call in calls)
        read = sum(call.get("cached_input_tokens", 0) for call in calls)
        created = sum(call.get("cache_write_input_tokens", 0) for call in calls)
        uncached = processed - read - created
    return {
        "input_tokens_processed": processed,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": created,
        "uncached_input_tokens": uncached,
        "new_input_plus_cache_creation": uncached + created,
        "output_tokens": output,
        "api_calls": calls,
    }


def _assert_no_tools_after(transcript: Path, harness: str, query: str) -> None:
    records = [json.loads(line) for line in transcript.read_text().splitlines()]
    payloads = [
        record.get("message" if harness == "claude" else "payload", {}) for record in records
    ]
    query_positions = []
    for index, payload in enumerate(payloads):
        content = payload.get("content", "")
        text = (
            content
            if isinstance(content, str)
            else "\n".join(block.get("text", "") for block in content if isinstance(block, dict))
        )
        if text == query:
            query_positions.append(index)
    assert query_positions, "Native transcript does not contain the recall request"
    for payload in payloads[query_positions[-1] + 1 :]:
        assert payload.get("type") not in {"function_call", "custom_tool_call", "local_shell_call"}
        content = payload.get("content", [])
        if isinstance(content, list):
            assert not any(block.get("type") == "tool_use" for block in content)


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_native_skill_invocation(live_server, http_client, tmp_path, monkeypatch, harness):
    if shutil.which(harness) is None:
        pytest.skip(f"{harness} CLI is not installed")
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for native session terminals")
    if not (Path.home() / ".agents" / "skills" / "poteto-mode" / "SKILL.md").is_file():
        pytest.skip("poteto-mode must be installed under ~/.agents/skills")
    output = Path(os.environ.get("NATIVE_SKILL_OUTPUT", str(tmp_path / "artifacts"))) / harness
    output.mkdir(parents=True, exist_ok=True)
    assert not any(output.iterdir()), f"Choose an empty artifact directory: {output}"
    terminal_args = json.loads(os.environ.get("NATIVE_SKILL_TERMINAL_ARGS", "[]"))
    assert isinstance(terminal_args, list) and all(isinstance(arg, str) for arg in terminal_args)
    workspace = tmp_path / "orchard"
    workspace.mkdir()
    resume_marker = "PERSIST_" + uuid.uuid4().hex[:12]
    fork_marker = "FORK_" + uuid.uuid4().hex[:12]
    probe_dir = workspace / ".claude" / "skills" / "orchard-notes"
    probe_dir.mkdir(parents=True)
    probe_dir.joinpath("SKILL.md").write_text(
        "---\nname: orchard-notes\ndescription: Reply style for orchard status.\n"
        "disable-model-invocation: true\n---\n"
        f"When the user says orchard status, reply with exactly {resume_marker}. "
        f"When the user says orchard ledger, reply with exactly {fork_marker}. "
        "Do not use tools.\n"
    )
    claude_config = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    if (
        harness == "claude"
        and not (claude_config / "skills" / "poteto-mode" / "SKILL.md").is_file()
    ):
        (workspace / ".claude" / "skills" / "poteto-mode").symlink_to(
            Path.home() / ".agents" / "skills" / "poteto-mode", target_is_directory=True
        )
    if harness == "codex":
        source_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        temporary_home = tmp_path / "codex-config"
        temporary_home.mkdir()
        for name in ("auth.json", "config.toml", "plugins", "skills"):
            source = source_home / name
            target = temporary_home / name
            if name == "skills":
                target.mkdir()
                if source.is_dir():
                    for skill in source.iterdir():
                        if skill.name not in {"poteto-mode", "orchard-notes"}:
                            (target / skill.name).symlink_to(skill, target_is_directory=True)
            elif source.exists():
                target.symlink_to(source, target_is_directory=source.is_dir())
        (temporary_home / "skills" / "poteto-mode").symlink_to(
            Path.home() / ".agents" / "skills" / "poteto-mode", target_is_directory=True
        )
        (temporary_home / "skills" / "orchard-notes").symlink_to(
            probe_dir, target_is_directory=True
        )
        monkeypatch.setenv("CODEX_HOME", str(temporary_home))
    native_routing = (
        "native_skill_routing"
        in {value.strip() for value in os.environ.get("OMNIGENT_FEATURES", "").split(",")}
        and os.environ.get("NATIVE_SKILL_BASELINE") != "1"
    )
    result = {
        "native_skill_routing_enabled": native_routing,
        "harness": harness,
        "terminal_launch_args": terminal_args,
        "native_cli_version": subprocess.check_output([harness, "--version"], text=True).strip(),
        "skill_sha256": hashlib.sha256(
            (Path.home() / ".agents" / "skills" / "poteto-mode" / "SKILL.md").read_bytes()
        ).hexdigest(),
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "source_diff_sha256": hashlib.sha256(
            subprocess.check_output(["git", "diff", "HEAD", "--", "omnigent"])
        ).hexdigest(),
        "source_dirty_files": subprocess.check_output(
            ["git", "diff", "--name-only", "HEAD", "--", "omnigent"], text=True
        ).splitlines(),
        "workspace": str(workspace),
        "server_url": live_server,
        "data_dir": os.environ.get("OMNIGENT_DATA_DIR"),
        "turns": [],
        "resume_marker": resume_marker,
        "fork_marker": fork_marker,
    }
    session_id = None
    bridge_dir = None
    daemon = None
    source_state = None

    def save():
        (output / "measurement.json").write_text(json.dumps(result, indent=2))

    def snapshot(label):
        response = http_client.get(
            f"/v1/sessions/{session_id}/items", params={"limit": 1000, "order": "asc"}
        )
        response.raise_for_status()
        (output / f"{label}-omnigent-items.json").write_text(json.dumps(response.json(), indent=2))
        response = http_client.get(f"/v1/sessions/{session_id}")
        response.raise_for_status()
        (output / f"{label}-session.json").write_text(json.dumps(response.json(), indent=2))
        resources = http_client.get(f"/v1/sessions/{session_id}/resources").json()
        (output / f"{label}-resources.json").write_text(json.dumps(resources, indent=2))
        transcript = None
        if harness == "claude":
            transcript = claude_bridge.read_transcript_path(bridge_dir)
            result["native_session_id"] = claude_bridge.read_claude_session_id(bridge_dir)
        else:
            state = codex_bridge.read_bridge_state(bridge_dir)
            if state:
                result["native_session_id"] = state.thread_id
                result["codex_home"] = state.codex_home
                transcript = _find_codex_rollout(Path(state.codex_home), state.thread_id)
        if transcript and transcript.exists():
            result["native_transcript_path"] = str(transcript)
            shutil.copyfile(transcript, output / f"{label}-native.jsonl")
        result.setdefault("snapshots", {})[label] = {
            "session_id": session_id,
            "native_session_id": result.get("native_session_id"),
            "native_transcript_path": str(transcript) if transcript else None,
        }
        tmux_file = bridge_dir / "tmux.json"
        if tmux_file.exists():
            info = json.loads(tmux_file.read_text())
            result["tmux"] = info
        else:
            terminal = next(
                (r for r in resources.get("data", []) if r.get("type") == "terminal"), None
            )
            metadata = terminal.get("metadata", {}) if terminal else {}
            if metadata.get("tmux_socket") and metadata.get("tmux_target"):
                result["tmux"] = {
                    "socket_path": metadata["tmux_socket"],
                    "tmux_target": metadata["tmux_target"],
                }
        if result.get("tmux"):
            info = result["tmux"]
            pane = subprocess.run(
                [
                    "tmux",
                    "-S",
                    info["socket_path"],
                    "capture-pane",
                    "-p",
                    "-t",
                    info["tmux_target"],
                    "-S",
                    "-1000",
                ],
                capture_output=True,
                text=True,
            )
            (output / f"{label}-pane.txt").write_text(pane.stdout)
        save()

    with _workspace_trusted_in_claude_config(workspace):
        try:
            daemon = _spawn_host_daemon(tmp_path=tmp_path, live_server=live_server)
            result["daemon_pid"] = daemon.pid
            result["daemon_log"] = str(tmp_path / "host-daemon.log")
            host_id = _online_host_id(http_client)
            agents = http_client.get("/v1/agents").json()["data"]
            agent_id = next(a["id"] for a in agents if a["name"] == f"{harness}-native-ui")
            session_body = {
                "agent_id": agent_id,
                "host_id": host_id,
                "workspace": str(workspace),
            }
            if terminal_args:
                session_body["terminal_launch_args"] = terminal_args
            response = http_client.post("/v1/sessions", json=session_body, timeout=60)
            response.raise_for_status()
            session_id = response.json()["id"]
            result["session_id"] = session_id
            bridge_module = claude_bridge if harness == "claude" else codex_bridge
            bridge_dir = bridge_module.bridge_dir_for_bridge_id(session_id)
            result["bridge_dir"] = str(bridge_dir)
            save()
            previous_usage = set()
            for index in (1, 2, 3):
                marker = f"ORCHARD_{min(index, 2)}"
                arguments = (
                    "For this request only, do not use tools or delegate. "
                    f"Reply with exactly {marker}."
                )
                previous = http_client.get(
                    f"/v1/sessions/{session_id}/items", params={"limit": 1000, "order": "asc"}
                ).json()["data"]
                previous_ids = {item.get("id") for item in previous}
                payload = {
                    "type": "slash_command",
                    "data": {"kind": "skill", "name": "poteto-mode", "arguments": arguments},
                }
                row = {"invocation": index, "request": payload, "started_at": time.time()}
                result["turns"].append(row)
                save()
                response = http_client.post(
                    f"/v1/sessions/{session_id}/events", json=payload, timeout=60
                )
                row["status_code"] = response.status_code
                row["event_response"] = response.json()
                save()
                response.raise_for_status()
                deadline = time.monotonic() + 240
                reply = ""
                while time.monotonic() < deadline:
                    current = http_client.get(
                        f"/v1/sessions/{session_id}/items", params={"limit": 1000, "order": "asc"}
                    ).json()["data"]
                    reply = "\n".join(
                        _assistant_text(item)
                        for item in current
                        if item.get("id") not in previous_ids
                    )
                    if marker in reply:
                        break
                    time.sleep(0.5)
                row["reply"] = reply
                row["completed_at"] = time.time()
                time.sleep(3)
                snapshot(f"turn-{index}")
                assert marker in reply
                usage = _native_usage(output / f"turn-{index}-native.jsonl", harness)
                row["usage"] = _summarize_usage(
                    [call for key, call in usage.items() if key not in previous_usage], harness
                )
                previous_usage = set(usage)
                save()
                saved_items = json.loads(
                    (output / f"turn-{index}-omnigent-items.json").read_text()
                )["data"]
                commands = [
                    item
                    for item in saved_items
                    if item.get("type") == "slash_command" and item.get("name") == "poteto-mode"
                ]
                assert len(commands) == index, (
                    "Native forwarder duplicated the visible skill command"
                )
                native_records = (output / f"turn-{index}-native.jsonl").read_text()
                assert "# Poteto mode" in native_records, "Native transcript has no skill body"
                if native_routing:
                    assert "<user_request>" not in native_records, (
                        "Omnigent expanded the skill before native delivery"
                    )
                else:
                    assert "<user_request>" in native_records, (
                        "Disabled native routing did not deliver the pasted skill"
                    )
            assert result.get("native_transcript_path"), "Native transcript was not captured"
            response = http_client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "slash_command",
                    "data": {
                        "kind": "skill",
                        "name": "orchard-notes",
                        "arguments": "Reply exactly READY_ORCHARD.",
                    },
                },
                timeout=60,
            )
            response.raise_for_status()
            _poll_for_assistant_marker(
                http_client, session_id=session_id, marker="READY_ORCHARD", timeout=240
            )
            time.sleep(3)
            snapshot("before-resume")
            before_resume_items = json.loads(
                (output / "before-resume-omnigent-items.json").read_text()
            )["data"]
            if native_routing:
                native_expansions = [
                    item
                    for item in before_resume_items
                    if item.get("type") == "message"
                    and item.get("role") == "user"
                    and item.get("is_meta") is True
                    and item.get("content")
                ]
                expansion_text = "\n".join(
                    block.get("text", "")
                    for item in native_expansions
                    for block in item["content"]
                    if isinstance(block, dict)
                )
                result["stored_native_expansion_ids"] = [item["id"] for item in native_expansions]
                save()
                assert "# Poteto mode" in expansion_text
                assert resume_marker in expansion_text and fork_marker in expansion_text, (
                    "Native skill expansions were not persisted before cold resume"
                )
            fork_cutoff = next(
                item["response_id"]
                for item in reversed(before_resume_items)
                if "READY_ORCHARD" in _assistant_text(item)
            )
            info = result["tmux"]
            subprocess.run(
                ["tmux", "-S", info["socket_path"], "kill-server"],
                check=True,
                capture_output=True,
            )
            time.sleep(3)
            response = http_client.patch(
                f"/v1/sessions/{session_id}", json={"runner_id": ""}, timeout=60
            )
            response.raise_for_status()
            response = http_client.post(
                f"/v1/hosts/{host_id}/runners",
                json={"session_id": session_id, "workspace": str(workspace)},
                timeout=120,
            )
            result["resume_launch"] = response.json()
            save()
            response.raise_for_status()
            response = http_client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "orchard status. Do not use tools or read files.",
                            }
                        ],
                    },
                },
                timeout=60,
            )
            response.raise_for_status()
            result["resume_reply"] = _poll_for_assistant_marker(
                http_client, session_id=session_id, marker=resume_marker, timeout=240
            )
            time.sleep(3)
            snapshot("after-resume")
            _assert_no_tools_after(
                output / "after-resume-native.jsonl",
                harness,
                "orchard status. Do not use tools or read files.",
            )
            source_state = {
                key: result.get(key)
                for key in ("native_session_id", "native_transcript_path", "codex_home")
            }
            info = result["tmux"]
            subprocess.run(
                ["tmux", "-S", info["socket_path"], "kill-server"],
                check=True,
                capture_output=True,
            )
            time.sleep(3)
            result["fork_cutoff_response_id"] = fork_cutoff
            if os.environ.get("NATIVE_SKILL_COLD_FORK") == "1":
                native_path = Path(result["native_transcript_path"])
                backup_path = output / "source-transcript-before-fork.jsonl"
                shutil.move(str(native_path), backup_path)
                result["removed_source_transcript_for_fork"] = str(native_path)
            response = http_client.post(
                f"/v1/sessions/{session_id}/fork",
                json={"title": "Orchard ledger", "up_to_response_id": fork_cutoff},
                timeout=60,
            )
            response.raise_for_status()
            session_id = response.json()["id"]
            bridge_dir = bridge_module.bridge_dir_for_bridge_id(session_id)
            result["fork_session_id"] = session_id
            save()
            response = http_client.post(
                f"/v1/hosts/{host_id}/runners",
                json={"session_id": session_id, "workspace": str(workspace)},
                timeout=120,
            )
            response.raise_for_status()
            response = http_client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "orchard ledger. Do not use tools or read files.",
                            }
                        ],
                    },
                },
                timeout=60,
            )
            response.raise_for_status()
            result["fork_reply"] = _poll_for_assistant_marker(
                http_client, session_id=session_id, marker=fork_marker, timeout=240
            )
            time.sleep(3)
            snapshot("fork")
            _assert_no_tools_after(
                output / "fork-native.jsonl",
                harness,
                "orchard ledger. Do not use tools or read files.",
            )
            fork_items = json.loads((output / "fork-omnigent-items.json").read_text())["data"]
            assert result["resume_reply"] not in {_assistant_text(item) for item in fork_items}, (
                "Fork included a response after the requested cutoff"
            )
            result["fork_native_transcript_path"] = result.get("native_transcript_path")
            result["fork_native_session_id"] = result.get("native_session_id")
            result.update(source_state)
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if session_id:
                try:
                    snapshot("final")
                    if source_state is not None:
                        result.update(source_state)
                except (OSError, ValueError, KeyError, httpx.HTTPError) as exc:
                    result["snapshot_error"] = f"{type(exc).__name__}: {exc}"
                info = result.get("tmux")
                if info:
                    subprocess.run(
                        ["tmux", "-S", info["socket_path"], "kill-server"], capture_output=True
                    )
            if daemon:
                daemon.send_signal(signal.SIGTERM)
                try:
                    daemon.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5)
            save()
