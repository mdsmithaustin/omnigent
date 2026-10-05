"""
REPL approval-flow e2e test -- sessions API variant (mock LLM).

Sessions-API parallel of ``test_repl_approval_e2e.py``. Spawns
``omnigent run <yaml>`` under pexpect and drives approval CUJs
through the ``/v1/sessions`` path.

All tests use the mock LLM server. ``OPENAI_BASE_URL`` in the
subprocess env is pointed at the mock server, and responses are
pre-configured before each pexpect interaction.

Usage::

    python -m pytest tests/e2e/test_repl_sessions_approval_e2e.py -v --timeout=120
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ASK_DEMO_YAML = _REPO_ROOT / "tests" / "resources" / "agents" / "ask-demo" / "ask-demo.yaml"
_FIXTURES_DIR = _REPO_ROOT / "tests" / "_fixtures" / "agents"
_TOOL_GATE_DIR = _FIXTURES_DIR / "e2e-tool-gate"
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences before substring search."""
    return _ANSI_RE.sub("", text)


def _build_repl_env(mock_llm_server_url: str, tmp_home: Path) -> dict[str, str]:
    """Build the pexpect environment dict for REPL spawning.

    Points ``OPENAI_BASE_URL`` at the mock LLM server so the spawned
    ``omnigent run`` subprocess uses mock responses.
    """
    from tests.e2e.omnigent._pexpect_harness import ensure_repl_test_theme_env

    sdk_paths = [
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
    ]
    existing_pp = os.environ.get("PYTHONPATH", "")
    merged_pp = (
        os.pathsep.join([*sdk_paths, existing_pp]) if existing_pp else os.pathsep.join(sdk_paths)
    )

    config_home = tmp_home / ".omnigent"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        "auto_open_conversation: false\ntui:\n  theme: dark\n",
    )

    real_databrickscfg = Path.home() / ".databrickscfg"
    env = {
        **os.environ,
        "OPENAI_API_KEY": "mock-key",
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "HOME": str(tmp_home),
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "DATABRICKS_CONFIG_FILE": str(real_databrickscfg),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
        "PYTHONPATH": merged_pp,
        "TERM": "xterm-256color",
        "LINES": "40",
        "COLUMNS": "120",
        "PROMPT_TOOLKIT_NO_CPR": "1",
    }
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE", "CLAUDECODE", "CODEX", "DATABRICKS_TOKEN"):
        env.pop(k, None)
    return ensure_repl_test_theme_env(env)


def _spawn_sessions_repl(
    yaml_path: Path,
    env: dict[str, str],
    *,
    timeout: int = 120,
) -> Any:
    """Spawn ``omnigent run`` under a PTY (sessions API is default)."""
    return pexpect.spawn(
        sys.executable,
        [
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            "--no-session",
        ],
        env=env,
        cwd=str(_REPO_ROOT),
        encoding="utf-8",
        codec_errors="replace",
        timeout=timeout,
        dimensions=(40, 120),
    )


def _spawn_repl_with_args(
    yaml_path: Path,
    env: dict[str, str],
    *,
    extra_args: list[str] | None = None,
    timeout: int = 120,
) -> Any:
    """Spawn ``omnigent run`` with caller-supplied CLI args."""
    args = [
        "-m",
        "omnigent",
        "run",
        str(yaml_path),
        "--no-session",
    ]
    if extra_args:
        args.extend(extra_args)
    return pexpect.spawn(
        sys.executable,
        args,
        env=env,
        cwd=str(_REPO_ROOT),
        encoding="utf-8",
        codec_errors="replace",
        timeout=timeout,
        dimensions=(40, 120),
    )


def _wait_for_prompt_ready(child: Any, timeout: float = 60.0) -> None:
    """Wait for prompt_toolkit's live input loop to reach idle state.

    The welcome banner can contain a prompt-shaped ``❯`` before the input
    loop starts reading.  The ready toolbar is emitted by the live loop, so
    waiting for it prevents the first keystroke from being dropped.
    """
    child.expect(r"·\s*ready", timeout=timeout)


def _read_pending(child: Any, seconds: float = 0.3) -> str:
    """Non-blocking read of buffered output, ANSI-stripped."""
    with contextlib.suppress(pexpect.EOF):
        child.expect(pexpect.TIMEOUT, timeout=seconds)
    captured = child.before or ""
    if isinstance(captured, bytes):
        captured = captured.decode("utf-8", errors="replace")
    return _strip_ansi(captured)


def _clean_exit(child: Any) -> None:
    """Best-effort clean exit of the REPL."""
    try:
        child.sendcontrol("d")
        child.expect(pexpect.EOF, timeout=10)
    except pexpect.ExceptionPexpect:
        pass
    if child.isalive():
        child.terminate(force=True)


@pytest.fixture(scope="module")
def repl_env(
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, str]:
    """Build the env dict for REPL spawning with mock LLM."""
    tmp_home = tmp_path_factory.mktemp("repl_sessions_home")
    return _build_repl_env(mock_llm_server_url, tmp_home)


def _configure_simple_response(mock_llm_server_url: str) -> None:
    """Configure mock to return a simple text response."""
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Hello! I am a friendly assistant. How can I help you today?"}],
        key="default",
    )


def _configure_multi_turn_responses(mock_llm_server_url: str, count: int = 2) -> None:
    """Configure mock to return multiple simple text responses."""
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": f"Response {i + 1}: I am happy to help with your request."}
            for i in range(count)
        ],
        key="default",
    )


# ── CUJ 1: Single approval allows LLM response ─────────


def test_sessions_single_approval_allows_llm_response(
    repl_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """Sessions API variant: approval prompt surfaces, user types
    ``y``, LLM reply renders.
    """
    reset_mock_llm(mock_llm_server_url)
    _configure_simple_response(mock_llm_server_url)

    from tests.e2e._approval_diagnostics import ApprovalDiagnostics

    diagnostics = ApprovalDiagnostics(tmp_path, Path(repl_env["OMNIGENT_DATA_DIR"]))
    child = _spawn_sessions_repl(
        _ASK_DEMO_YAML, {**repl_env, "OMNIGENT_SESSIONS_ADAPTER_DEBUG": "1"}
    )
    diagnostics.attach(child)
    try:
        _wait_for_prompt_ready(child)
        child.send("Hello\r")
        child.expect("approval required", timeout=30)
        child.send("y\r")
        child.expect("approved", timeout=10)

        buffered = _read_pending(child, seconds=5.0)
        buffered += _read_pending(child, seconds=3.0)
        assert re.search(r"[A-Za-z]{3,}", buffered), (
            f"No LLM response after approval.\nBuffer:\n{buffered[:800]}"
        )
    except pexpect.EOF:
        buf = _strip_ansi(child.before or "")
        pytest.fail(f"REPL exited early. Full buffer:\n{buf[-2000:]}")
    finally:
        try:
            diagnostics.capture(child, timeout=isinstance(sys.exc_info()[1], pexpect.TIMEOUT))
        except Exception as error:
            diagnostics.failure(error)
        _clean_exit(child)
        try:
            diagnostics.write_pty()
        except Exception as error:
            diagnostics.failure(error)
        diagnostics.write_metadata()


# ── CUJ 2: Refusal shows deny sentinel ──────────────────


def test_sessions_refusal_shows_deny_sentinel(
    repl_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """Sessions API variant: user refuses -> deny sentinel appears."""
    reset_mock_llm(mock_llm_server_url)
    # Even though the user refuses, the mock must have something queued
    # in case the agent still gets a turn after denial.
    _configure_simple_response(mock_llm_server_url)

    child = _spawn_sessions_repl(_ASK_DEMO_YAML, repl_env)
    try:
        _wait_for_prompt_ready(child)
        child.send("Hello\r")
        child.expect("approval required", timeout=30)
        child.send("n\r")
        child.expect("refused", timeout=10)

        buffered = _read_pending(child, seconds=5.0)
        assert "DENIED" in buffered.upper() or "refused" in buffered.lower(), (
            f"No deny sentinel after refusal.\nBuffer:\n{buffered[:800]}"
        )
    finally:
        _clean_exit(child)


# ── CUJ 3: Multi-turn fires approval each turn ──────────


def test_sessions_two_turns_fires_one_approval_per_turn(
    repl_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """Sessions API variant: each turn produces exactly one
    approval prompt.
    """
    reset_mock_llm(mock_llm_server_url)
    _configure_multi_turn_responses(mock_llm_server_url, count=2)

    child = _spawn_sessions_repl(_ASK_DEMO_YAML, repl_env)
    try:
        _wait_for_prompt_ready(child)

        # Turn 1.
        child.send("First message\r")
        child.expect("approval required", timeout=30)
        child.send("y\r")
        child.expect("approved", timeout=10)
        _read_pending(child, seconds=5.0)

        # Turn 2.
        child.send("Second message\r")
        child.expect("approval required", timeout=30)
        child.send("y\r")
        child.expect("approved", timeout=10)
        buffered = _read_pending(child, seconds=5.0)
        assert re.search(r"[A-Za-z]{3,}", buffered), (
            f"No reply after second-turn approval.\nBuffer:\n{buffered[:800]}"
        )
    finally:
        _clean_exit(child)


# ── CUJ 4: Approve-always caches for session ────────────


def test_sessions_approve_always_caches_for_later_turns(
    repl_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """Sessions API variant: ``a`` (approve always) on first turn
    suppresses the prompt on the second turn.
    """
    reset_mock_llm(mock_llm_server_url)
    _configure_multi_turn_responses(mock_llm_server_url, count=2)

    child = _spawn_sessions_repl(_ASK_DEMO_YAML, repl_env)
    try:
        _wait_for_prompt_ready(child)

        # Turn 1: approve always.
        child.send("First\r")
        child.expect("approval required", timeout=30)
        child.send("a\r")
        child.expect("approved always", timeout=10)
        _read_pending(child, seconds=5.0)

        # Turn 2: should auto-approve (no prompt).
        child.send("Second\r")
        buffered = _read_pending(child, seconds=8.0)
        approval_count = buffered.count("approval required")
        assert approval_count == 0, (
            f"Approval prompt appeared after approve-always. "
            f"Count={approval_count}\nBuffer:\n{buffered[:800]}"
        )
        assert re.search(r"[A-Za-z]{3,}", buffered), (
            f"No auto-approved reply.\nBuffer:\n{buffered[:800]}"
        )
    finally:
        _clean_exit(child)


# ── CUJ 5: Tool call approval ───────────────────────────


def test_sessions_tool_call_approval_allows_tool(
    repl_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """Sessions API variant: tool-phase approval surfaces,
    user approves, tool runs.
    """
    import json

    tool_gate_yaml = _TOOL_GATE_DIR / "e2e-tool-gate.yaml"
    if not tool_gate_yaml.exists():
        pytest.skip(f"Fixture {tool_gate_yaml} not found")

    reset_mock_llm(mock_llm_server_url)
    # Mock: first call the echo tool, second produce final text.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_1",
                        "name": "echo",
                        "arguments": json.dumps({"message": "Use the tool"}),
                    },
                ],
            },
            {"text": "The echo tool returned: [ECHO] Use the tool"},
        ],
        key="default",
    )

    child = _spawn_sessions_repl(tool_gate_yaml, repl_env)
    try:
        _wait_for_prompt_ready(child, timeout=60)
        child.send("Use the tool\r")
        child.expect("approval required", timeout=30)
        child.send("y\r")
        child.expect("approved", timeout=10)
        buffered = _read_pending(child, seconds=8.0)
        assert re.search(r"[A-Za-z]{3,}", buffered), (
            f"No response after tool approval.\nBuffer:\n{buffered[:800]}"
        )
    finally:
        _clean_exit(child)


# ── CUJ 6: Default flag uses sessions API ─────────────────


def _write_simple_agent_yaml(directory: Path) -> Path:
    """Write a simple agent YAML with no policies (no approval)."""
    yaml_path = directory / "simple_hello.yaml"
    yaml_path.write_text(
        "name: simple_hello\n"
        "executor:\n"
        "  model: gpt-4o\n"
        "prompt: >-\n"
        "  You are a friendly assistant. Respond in exactly one short sentence.\n",
    )
    return yaml_path


def test_sessions_default_flag_works(
    repl_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """Spawns the REPL through the default sessions path and verifies
    the agent responds through the sessions API.
    """
    yaml_path = _write_simple_agent_yaml(tmp_path)

    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Hello there, nice to meet you today!"}],
        key="default",
    )

    child = _spawn_repl_with_args(yaml_path, repl_env)
    try:
        _wait_for_prompt_ready(child, timeout=60)
        child.send("Say hello in exactly five words\r")

        buffered = _read_pending(child, seconds=10.0)
        buffered += _read_pending(child, seconds=5.0)

        assert re.search(r"[A-Za-z]{3,}", buffered), (
            f"No LLM response rendered -- sessions-API default may "
            f"not be active.\nBuffer:\n{buffered[:800]}"
        )
    except pexpect.EOF:
        buf = _strip_ansi(child.before or "")
        pytest.fail(f"REPL exited early (default sessions flag). Full buffer:\n{buf[-2000:]}")
    finally:
        _clean_exit(child)


def test_approval_diagnostics_redacts_split_pty_secrets(tmp_path: Path) -> None:
    import json

    from tests.e2e._approval_diagnostics import ApprovalDiagnostics, _PtyWriter

    capture = ApprovalDiagnostics(tmp_path, tmp_path / "data")
    writer = _PtyWriter(capture, "read")
    writer.write("ready\nAuthorization: Bear")
    writer.write("er secret-header\ncontent_preview: private-message\n")
    writer.write("provider: private-provider\nconfig: private-config\nenv: private-env\n")
    writer.write("raw_bundle: private-bundle\napi_key: secret-key\napproval required\n")
    _PtyWriter(capture, "send").write("Hello\r")
    capture.write_pty()
    records = [
        json.loads(line) for line in (capture.directory / "pty.log").read_text().splitlines()
    ]
    output = "".join(record["text"] for record in records)
    assert "ready\n" in output
    assert "approval required\n" in output
    assert "Hello\r" in output
    assert len(records) == 5
    assert all(record["utc"] and record["elapsed"] >= 0 for record in records)
    for secret in (
        "secret-header",
        "private-message",
        "private-provider",
        "private-config",
        "private-env",
        "private-bundle",
        "secret-key",
    ):
        assert secret not in output


def test_approval_diagnostics_projects_only_session_metadata() -> None:
    from tests.e2e._approval_diagnostics import session_projection

    assert session_projection(
        {
            "id": "conv_owned",
            "status": "running",
            "runner_id": "runner_owned",
            "content_preview": "private",
            "provider": "private",
            "config": {"token": "private"},
            "pending_elicitations": [
                {
                    "elicitation_id": "elicit_owned",
                    "type": "elicitation.requested",
                    "params": {
                        "phase": "REQUEST",
                        "policy_name": "ask_all",
                        "message": "private",
                        "content_preview": "private",
                        "requestedSchema": {"secret": "private"},
                    },
                }
            ],
        }
    ) == {
        "session_id": "conv_owned",
        "status": "running",
        "runner_id": "runner_owned",
        "pending_elicitations": [
            {
                "elicitation_id": "elicit_owned",
                "type": "elicitation.requested",
                "phase": "REQUEST",
                "policy_name": "ask_all",
            }
        ],
    }
    assert session_projection({}) == {
        "session_id": "unknown",
        "status": "unknown",
        "runner_id": "unknown",
        "pending_elicitations": "unknown",
    }
    assert session_projection({"id": {"token": "secret"}})["session_id"] == "unknown"
    assert session_projection({"status": "sk-private-key"})["status"] == "unknown"


def test_approval_diagnostics_failure_preserves_timeout(tmp_path: Path, monkeypatch: Any) -> None:
    import json
    from types import SimpleNamespace

    from tests.e2e._approval_diagnostics import ApprovalDiagnostics

    capture = ApprovalDiagnostics(tmp_path, tmp_path / "data")

    def fail_process(pid: int) -> None:
        raise RuntimeError("secret exception payload")

    monkeypatch.setattr("tests.e2e._approval_diagnostics.psutil.Process", fail_process)
    original = pexpect.TIMEOUT("original banner deadline")
    with pytest.raises(pexpect.TIMEOUT) as raised:
        try:
            raise original
        finally:
            capture.capture(SimpleNamespace(pid=123), timeout=True)
    assert raised.value is original
    receipt = json.loads((capture.directory / "capture.log").read_text())
    assert receipt["capture_error"] == "RuntimeError"
    assert receipt["timeout"] is True
    assert "secret exception payload" not in (capture.directory / "capture.log").read_text()


def test_approval_diagnostics_retains_only_owned_logs(tmp_path: Path, monkeypatch: Any) -> None:
    import json
    from types import SimpleNamespace

    from tests.e2e._approval_diagnostics import ApprovalDiagnostics

    data = tmp_path / "data"
    logs = data / "logs"
    logs.mkdir(parents=True)
    owned = logs / "owned.log"
    owned.write_text("INFO 10-04 12:00:00.000 source function | token=private-token\n")
    (logs / "other-case.log").write_text("other case must never be copied\n")
    process = SimpleNamespace(
        pid=123,
        children=lambda **kwargs: [],
        open_files=lambda: [SimpleNamespace(path=str(owned))],
    )
    monkeypatch.setattr("tests.e2e._approval_diagnostics.psutil.Process", lambda pid: process)
    capture = ApprovalDiagnostics(tmp_path, data)
    capture.capture(process, timeout=False)
    receipt = json.loads((capture.directory / "capture.log").read_text())
    assert receipt["owned_log_count"] == 1
    assert (capture.directory / "runtime-0.log").read_text() == (
        "INFO 10-04 12:00:00.000 source function | [REDACTED]\n"
    )
    assert len(list(capture.directory.glob("runtime-*.log"))) == 1


def test_approval_diagnostics_bounds_session_get(tmp_path: Path, monkeypatch: Any) -> None:
    import asyncio
    import json
    import time
    from types import SimpleNamespace

    from tests.e2e._approval_diagnostics import ApprovalDiagnostics, _PtyWriter

    capture = ApprovalDiagnostics(tmp_path, tmp_path / "data")
    process = SimpleNamespace(pid=123, children=lambda **kwargs: [], open_files=list)
    monkeypatch.setattr("tests.e2e._approval_diagnostics.psutil.Process", lambda pid: process)
    requested = []

    async def stalled_snapshot(url: str, session_id: str) -> dict[str, Any]:
        requested.append((url, session_id))
        await asyncio.Event().wait()
        raise AssertionError("stall must be cancelled")

    monkeypatch.setattr(capture, "_snapshot", stalled_snapshot)
    _PtyWriter(capture, "read").write(
        "http://127.0.0.1:12345 · server 0.16.0\n"
        "[sessions-adapter] session created id='conv_owned'\n"
    )
    original = pexpect.TIMEOUT("original banner deadline")
    started = time.monotonic()
    with pytest.raises(pexpect.TIMEOUT) as raised:
        try:
            raise original
        finally:
            capture.capture(process, timeout=True)
    assert raised.value is original
    assert requested == [("http://127.0.0.1:12345", "conv_owned")]
    assert time.monotonic() - started < 2.0
    receipt = json.loads((capture.directory / "capture.log").read_text())
    assert receipt["capture_error"] == "TimeoutError"
    assert receipt["session"]["pending_elicitations"] == "unknown"
    assert "conv_owned" in (capture.directory / "pty.log").read_text()


@pytest.mark.parametrize("direction", ["read", "send"])
def test_approval_diagnostics_masks_multiline_and_final_secret(
    tmp_path: Path, direction: str
) -> None:
    import json

    from tests.e2e._approval_diagnostics import ApprovalDiagnostics, _PtyWriter

    capture = ApprovalDiagnostics(tmp_path, tmp_path / "data")
    writer = _PtyWriter(capture, direction)
    token = "dapi" + "a" * 32
    writer.write("ready\n" + token[:13])
    writer.write(token[13:] + "\nconfig:\n  endpoint: SYNTHETIC_MULTILINE_VALUE\n")
    writer.write("approval required\n" + "SYNTHETIC_PARTIAL_SECRET")
    capture.write_pty()
    output = "".join(
        json.loads(line)["text"]
        for line in (capture.directory / "pty.log").read_text().splitlines()
    )
    assert "ready\n" in output and "approval required\n" in output
    assert token not in output
    assert "SYNTHETIC_MULTILINE_VALUE" not in output
    assert "SYNTHETIC_PARTIAL_SECRET" not in output


def test_approval_diagnostics_caps_pty_and_pending(tmp_path: Path) -> None:
    import json

    from tests.e2e._approval_diagnostics import ApprovalDiagnostics, _PtyWriter, session_projection

    capture = ApprovalDiagnostics(tmp_path, tmp_path / "data")
    writer = _PtyWriter(capture, "read")
    writer.write("ready\n")
    for _ in range(10000):
        writer.write("X" * 2048 + "\n")
    capture.write_pty()
    assert sum(len(e["text"].encode()) for e in capture.events) <= 512 * 1024
    assert len(capture.events) <= 4096
    assert (capture.directory / "pty.log").stat().st_size < 2 * 1024 * 1024
    records = [
        json.loads(line) for line in (capture.directory / "pty.log").read_text().splitlines()
    ]
    assert records[-1]["truncated"] is True
    projection = session_projection(
        {
            "id": "conv_owned",
            "pending_elicitations": [{"elicitation_id": "elicit_owned", "params": {}}] * 10000,
        }
    )
    assert projection["session_id"] == "conv_owned"
    assert len(projection["pending_elicitations"]) == 32
    assert projection["pending_truncated"] is True


def test_approval_diagnostics_discards_secret_crossing_caps(tmp_path: Path) -> None:
    import json

    from tests.e2e._approval_diagnostics import ApprovalDiagnostics, _PtyWriter

    capture = ApprovalDiagnostics(tmp_path, tmp_path / "data")
    writer = _PtyWriter(capture, "read")
    writer.write("ready\n")
    writer.write("config:\n" + "SYNTHETIC_CAP_SECRET" * 2000)
    writer.write("remaining secret\n")
    capture.write_pty()
    output = (capture.directory / "pty.log").read_text()
    assert "ready" in output
    assert "SYNTHETIC_CAP_SECRET" not in output
    assert "remaining secret" not in output
    assert json.loads(output.splitlines()[-1])["truncated"] is True
    assert sum(len(e["text"]) for e in capture.events) <= 16384 + 32


@pytest.mark.parametrize("failure", ["capture", "flush", "redact", "receipt"])
@pytest.mark.parametrize("original_type", [pexpect.TIMEOUT, AssertionError])
def test_approval_diagnostics_testcase_preserves_failure(
    tmp_path: Path, monkeypatch: Any, failure: str, original_type: Any
) -> None:
    from types import SimpleNamespace

    from tests.e2e import _approval_diagnostics as diagnostics
    from tests.e2e import test_repl_sessions_approval_e2e as module

    original = original_type("original test failure")
    cleaned = []
    child = SimpleNamespace(pid=os.getpid(), send=lambda text: None)

    def expect(pattern: str, timeout: Any = None) -> None:
        if pattern == "approval required":
            raise original

    child.expect = expect
    monkeypatch.setattr(module, "reset_mock_llm", lambda _: None)
    monkeypatch.setattr(module, "_configure_simple_response", lambda _: None)
    monkeypatch.setattr(module, "_spawn_sessions_repl", lambda *a, **k: child)
    monkeypatch.setattr(module, "_clean_exit", lambda _: cleaned.append(True))

    def fault(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("SYNTHETIC_FAILURE_PAYLOAD")

    if failure == "capture":
        monkeypatch.setattr(diagnostics.ApprovalDiagnostics, "capture", fault)
    elif failure == "redact":
        monkeypatch.setattr(diagnostics, "redact", fault)
    elif failure == "receipt":
        monkeypatch.setattr(Path, "write_text", fault)
    else:
        real = diagnostics.ApprovalDiagnostics.write_pty
        calls = []

        def final_fault(self: Any) -> None:
            calls.append(True)
            if len(calls) == 2:
                fault()
            real(self)

        monkeypatch.setattr(diagnostics.ApprovalDiagnostics, "write_pty", final_fault)
    with pytest.raises(original_type) as raised:
        module.test_sessions_single_approval_allows_llm_response(
            {"OMNIGENT_DATA_DIR": str(tmp_path / "data")}, "unused", tmp_path
        )
    assert raised.value is original
    assert cleaned == [True]


def test_approval_diagnostics_bounds_pending_fields() -> None:
    from tests.e2e._approval_diagnostics import session_projection

    projected = session_projection(
        {
            "id": "conv_owned",
            "status": "X" * 10000,
            "pending_elicitations": [
                {
                    "elicitation_id": "elicit_owned",
                    "params": {"phase": "request", "policy_name": "S" * 10000},
                }
            ]
            * 10000,
        }
    )
    assert projected["session_id"] == "conv_owned"
    assert projected["status"] == "unknown"
    assert len(projected["pending_elicitations"]) == 32
    assert projected["pending_elicitations"][0] == {
        "elicitation_id": "elicit_owned",
        "type": "unknown",
        "phase": "request",
        "policy_name": "unknown",
    }
    assert projected["pending_truncated"] is True
    assert (
        session_projection({"pending_elicitations": [None]})["pending_elicitations"][0]["phase"]
        == "unknown"
    )


def test_approval_diagnostics_bounds_runtime_copies(tmp_path: Path, monkeypatch: Any) -> None:
    import json
    from types import SimpleNamespace

    from tests.e2e._approval_diagnostics import ApprovalDiagnostics

    logs = tmp_path / "data" / "logs"
    logs.mkdir(parents=True)
    owned = []
    record = "INFO 10-04 12:00:00.000 source function | SYNTHETIC_MESSAGE conv_owned\n"
    for index in range(10):
        path = logs / f"owned-{index}.log"
        path.write_text(record * 20000)
        owned.append(SimpleNamespace(path=str(path)))
    (logs / "foreign.log").write_text("FOREIGN_CASE_SENTINEL")
    process = SimpleNamespace(pid=123, children=lambda **kwargs: [], open_files=lambda: owned)
    monkeypatch.setattr("tests.e2e._approval_diagnostics.psutil.Process", lambda pid: process)
    capture = ApprovalDiagnostics(tmp_path, tmp_path / "data")
    capture.capture(process, timeout=False)
    copies = list(capture.directory.glob("runtime-*.log"))
    assert 1 <= len(copies) <= 8
    assert max(path.stat().st_size for path in copies) <= 256 * 1024
    assert sum(path.stat().st_size for path in copies) <= 1024 * 1024
    text = "".join(path.read_text() for path in copies)
    assert "INFO 10-04 12:00:00.000 source function | [REDACTED]" in text
    assert "SYNTHETIC_MESSAGE" not in text and "FOREIGN_CASE_SENTINEL" not in text
    receipt = json.loads((capture.directory / "capture.log").read_text())
    assert receipt["runtime_truncated"] is True
    assert receipt["runtime_bytes_read"] <= 1024 * 1024
    assert "[TRUNCATED]" in text


def test_approval_diagnostics_masks_runtime_header_and_partial_record(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from types import SimpleNamespace

    from tests.e2e._approval_diagnostics import ApprovalDiagnostics

    logs = tmp_path / "data" / "logs"
    logs.mkdir(parents=True)
    path = logs / "owned.log"
    token = "dapi" + "b" * 32
    path.write_text(
        f"INFO 10-04 12:00:00.000 {token} function | arbitrary payload\n"
        "INFO 10-04 12:00:00.000 SYNTHETIC_PARTIAL_HEADER"
    )
    process = SimpleNamespace(
        pid=123, children=lambda **kwargs: [], open_files=lambda: [SimpleNamespace(path=str(path))]
    )
    monkeypatch.setattr("tests.e2e._approval_diagnostics.psutil.Process", lambda pid: process)
    capture = ApprovalDiagnostics(tmp_path, tmp_path / "data")
    capture.capture(process, timeout=False)
    text = (capture.directory / "runtime-0.log").read_text()
    assert "INFO 10-04 12:00:00.000" in text and "[REDACTED]" in text
    assert token not in text and "SYNTHETIC_PARTIAL_HEADER" not in text
    assert "[TRUNCATED]" in text


def test_approval_diagnostics_bounds_http_before_json(tmp_path: Path, monkeypatch: Any) -> None:
    import asyncio
    import json

    import httpx

    from tests.e2e._approval_diagnostics import ApprovalDiagnostics

    chunks = []
    requested = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> Any:
            payload = json.dumps(
                {
                    "id": "conv_owned",
                    "status": "running",
                    "content_preview": "SYNTHETIC_HTTP_PAYLOAD" * 20000,
                }
            ).encode()
            for start in range(0, len(payload), 8192):
                chunks.append(start)
                yield payload[start : start + 8192]

    def response(request: Any) -> Any:
        requested.append(str(request.url))
        return httpx.Response(200, stream=Body())

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(response), **kwargs),
    )
    capture = ApprovalDiagnostics(tmp_path, tmp_path / "data")
    result = asyncio.run(capture._snapshot("http://127.0.0.1:12345", "conv_owned"))
    assert result["session_id"] == "conv_owned"
    assert result["status"] == "unknown"
    assert len(chunks) <= 17
    assert capture.metadata["http_truncated"] is True
    assert requested == ["http://127.0.0.1:12345/v1/sessions/conv_owned"]


def test_approval_diagnostics_bounds_tiny_events(tmp_path: Path) -> None:
    import json

    from tests.e2e._approval_diagnostics import ApprovalDiagnostics, _PtyWriter

    capture = ApprovalDiagnostics(tmp_path, tmp_path / "data")
    writer = _PtyWriter(capture, "read")
    writer.write("ready\n")
    for _ in range(10000):
        writer.write("\n")
    capture.write_pty()
    records = [
        json.loads(line) for line in (capture.directory / "pty.log").read_text().splitlines()
    ]
    assert len(capture.events) == 4096
    assert records[0]["text"] == "ready\n"
    assert records[-1]["truncated"] is True


@pytest.mark.parametrize("kind", ["bytes", "events", "line"])
def test_approval_diagnostics_masks_token_at_budget(tmp_path: Path, kind: str) -> None:
    import json

    from tests.e2e._approval_diagnostics import ApprovalDiagnostics, _PtyWriter

    capture = ApprovalDiagnostics(tmp_path, tmp_path / "data")
    writer = _PtyWriter(capture, "read")
    writer.write("ready\n")
    if kind == "bytes":
        for _ in range(511):
            writer.write("X" * 1023 + "\n")
        writer.write("X" * 1007 + "\n")
    elif kind == "events":
        for _ in range(4094):
            writer.write("\n")
    else:
        writer.write("X" * 16378)
    writer.write("dapi" + "a" * 6)
    writer.write("a" * 26 + "\n")
    capture.write_pty()
    records = [
        json.loads(line) for line in (capture.directory / "pty.log").read_text().splitlines()
    ]
    text = "".join(record["text"] for record in records)
    assert "ready\n" in text
    assert "dapi" not in text
    assert records[-1]["truncated"] is True


@pytest.mark.parametrize(
    "text, forbidden",
    [
        (
            "config:\n  endpoint: SYNTHETIC_ENDPOINT_VALUE\napproval required\n",
            "SYNTHETIC_ENDPOINT_VALUE",
        ),
        ("config:\nSYNTHETIC_UNINDENTED_VALUE\napproval required\n", "SYNTHETIC_UNINDENTED_VALUE"),
        ("approval required\nPARTIAL_VALUE_SENTINEL", "PARTIAL_VALUE_SENTINEL"),
    ],
)
def test_approval_diagnostics_masks_sensitive_record(
    tmp_path: Path, text: str, forbidden: str
) -> None:
    import json

    from tests.e2e._approval_diagnostics import ApprovalDiagnostics, _PtyWriter

    capture = ApprovalDiagnostics(tmp_path, tmp_path / "data")
    _PtyWriter(capture, "read").write(text)
    capture.write_pty()
    output = "".join(
        json.loads(line)["text"]
        for line in (capture.directory / "pty.log").read_text().splitlines()
    )
    assert "approval required\n" in output
    assert forbidden not in output


def test_approval_diagnostics_flush_masks_incomplete_token(tmp_path: Path) -> None:
    import json

    from tests.e2e._approval_diagnostics import ApprovalDiagnostics, _PtyWriter

    capture = ApprovalDiagnostics(tmp_path, tmp_path / "data")
    writer = _PtyWriter(capture, "read")
    writer.write("ready\ndapi" + "a" * 6)
    capture.write_pty()
    first = (capture.directory / "pty.log").read_text()
    assert "ready" in first and "dapi" not in first
    writer.write("a" * 26 + "\n")
    capture.write_pty()
    text = "".join(
        json.loads(line)["text"]
        for line in (capture.directory / "pty.log").read_text().splitlines()
    )
    assert "ready\n" in text and "dapi" not in text


def test_approval_diagnostics_retains_exit_key_after_partial_secret(tmp_path: Path) -> None:
    import json

    from tests.e2e._approval_diagnostics import ApprovalDiagnostics, _PtyWriter

    capture = ApprovalDiagnostics(tmp_path, tmp_path / "data")
    writer = _PtyWriter(capture, "send")
    writer.write("Hello\rPARTIAL_VALUE_SENTINEL")
    writer.write("\x04")
    capture.write_pty()
    records = [
        json.loads(line) for line in (capture.directory / "pty.log").read_text().splitlines()
    ]
    assert records[-1]["text"] == "\x04"
    assert "Hello\r" in records[0]["text"]
    assert "PARTIAL_VALUE_SENTINEL" not in records[0]["text"]
