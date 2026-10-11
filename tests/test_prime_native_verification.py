from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import psutil
import pytest


def _provider_probe():
    scripts = Path(__file__).parents[1] / ".agents/skills/verify-prime-native/scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location(
        "prime_native_provider_probe_for_test", scripts / "provider_probe.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _provider_reply(probe):
    header = {"id": "session", "type": "session"}
    entries = [
        header,
        {
            "id": "native-user",
            "type": "message",
            "parentId": None,
            "message": {"role": "user", "content": "PROMPT"},
        },
        {
            "id": "native-final",
            "type": "message",
            "parentId": "native-user",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": "REPLY"}],
            },
        },
    ]
    items = [
        {
            "id": "public-user",
            "type": "message",
            "role": "user",
            "response_id": "independent-user-response",
            "content": [{"type": "input_text", "text": "PROMPT"}],
        },
        {
            "id": "public-final",
            "type": "message",
            "role": "assistant",
            "response_id": "final-response",
            "status": "completed",
            "content": [{"type": "output_text", "text": "REPLY"}],
        },
    ]
    operation = probe._ReplyOperation(
        prompt="PROMPT",
        literal="REPLY",
        code=None,
        deadline=time.monotonic() + 10,
        native_baseline=probe._reply_rows([header]),
        public_baseline=(),
        native_ids=("session",),
        public_ids=(),
        journal_path=Path("/synthetic/journal"),
        session_id="conversation",
        external_id="session",
        session_headers=("session",),
        root=None,
    )
    return operation, entries, items


def test_provider_reply_requires_a_fresh_public_prompt():
    probe = _provider_probe()
    operation, entries, items = _provider_reply(probe)

    missing = probe._reply_verdict(operation, entries, items[1:])
    assert missing["complete"] is False
    assert missing["predicates"]["public_user_count"] == 0
    assert missing["predicates"]["public_final_position"] == 0
    assert missing["predicates"]["public_user_before_final"] is False

    valid = probe._reply_verdict(operation, entries, items)
    assert valid["complete"] is True
    assert valid["native_user_id"] == "native-user"
    assert valid["public_user_id"] == "public-user"
    assert valid["native_reply_id"] == "native-final"
    assert valid["public_reply_id"] == "public-final"
    assert valid["response_id"] == "final-response"
    assert valid["predicates"]["public_user_ids"] == ["public-user"]
    assert valid["predicates"]["public_user_positions"] == [0]
    assert valid["predicates"]["public_user_fresh"] is True
    assert valid["predicates"]["public_user_exact_prompt"] is True
    assert valid["predicates"]["public_user_before_final"] is True


@pytest.mark.parametrize("text", ["WRONG", " PROMPT", "PROMPT\n", "prompt", ""])
def test_provider_reply_does_not_normalize_public_prompt(text):
    probe = _provider_probe()
    operation, entries, items = _provider_reply(probe)
    items[0]["content"][0]["text"] = text

    result = probe._reply_verdict(operation, entries, items)

    assert result["complete"] is False
    assert result["predicates"]["public_user_count"] == 0
    assert result["predicates"]["reason"] == "public_prompt_pending"


@pytest.mark.parametrize("role", ["system", "tool", None])
def test_provider_reply_counts_only_public_users(role):
    probe = _provider_probe()
    operation, entries, items = _provider_reply(probe)
    items[0]["role"] = role

    result = probe._reply_verdict(operation, entries, items)

    assert result["complete"] is False
    assert result["predicates"]["public_user_ids"] == []


@pytest.mark.parametrize(
    "content",
    [
        "PROMPT",
        None,
        {"type": "input_text", "text": "PROMPT"},
        ["PROMPT"],
        [{}],
        [{"type": "input_text", "text": 7}],
        [{"type": "output_text", "text": "PROMPT"}],
        [{"type": "input_text", "input_text": "PROMPT"}],
    ],
)
def test_provider_reply_rejects_malformed_public_user_content(content):
    probe = _provider_probe()
    operation, entries, items = _provider_reply(probe)
    items[0]["content"] = content

    with pytest.raises(RuntimeError, match=r"public_.*content_malformed"):
        probe._reply_verdict(operation, entries, items)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("duplicate", "public_owned_prompt_ambiguous"),
        ("late", "public_owned_prompt_after_final"),
        ("intervening", "public_owned_turn_interrupted"),
        ("stale", "public_owned_prompt_in_baseline"),
        ("duplicate_id", "public_item_id_malformed_or_duplicate"),
    ],
)
def test_provider_reply_rejects_ambiguous_or_unowned_public_turn(change, reason):
    probe = _provider_probe()
    operation, entries, items = _provider_reply(probe)
    if change == "duplicate":
        items.insert(1, {**items[0], "id": "public-user-2"})
    elif change == "late":
        items.reverse()
    elif change == "intervening":
        items.insert(
            1,
            {
                **items[0],
                "id": "different-user",
                "content": [{"type": "input_text", "text": "DIFFERENT"}],
            },
        )
    elif change == "stale":
        operation = dataclasses.replace(
            operation,
            public_baseline=probe._reply_rows(items[:1]),
            public_ids=("public-user",),
        )
    else:
        items.insert(1, dict(items[0]))

    with pytest.raises(RuntimeError, match=reason):
        probe._reply_verdict(operation, entries, items)


def test_provider_reply_allows_earlier_wait_prompt_and_split_input_text():
    probe = _provider_probe()
    operation, entries, items = _provider_reply(probe)
    wait_user = {
        **items[0],
        "id": "wait-user",
        "content": [{"type": "input_text", "text": "WAIT"}],
    }
    items.insert(0, wait_user)
    items[1]["content"] = [
        {"type": "input_text", "text": "PRO"},
        {"type": "input_text", "text": "MPT"},
    ]
    entries.insert(
        1,
        {
            "id": "native-wait-user",
            "type": "message",
            "parentId": None,
            "message": {"role": "user", "content": "WAIT"},
        },
    )
    entries[2]["parentId"] = "native-wait-user"

    result = probe._reply_verdict(operation, entries, items)

    assert result["complete"] is True
    assert result["predicates"]["public_user_count"] == 1
    assert result["predicates"]["public_user_positions"] == [1]
    assert result["predicates"]["public_final_position"] == 2


def test_provider_reply_reports_prompt_evidence_before_final_arrives():
    probe = _provider_probe()
    operation, entries, items = _provider_reply(probe)

    waiting = probe._reply_verdict(operation, entries[:2], items[:1])
    assert waiting["complete"] is False
    assert waiting["predicates"]["public_user_count"] == 1
    assert waiting["predicates"]["public_user_fresh"] is True
    assert waiting["predicates"]["public_user_exact_prompt"] is True
    assert waiting["predicates"]["public_final_position"] is None
    assert waiting["predicates"]["public_user_before_final"] is False
    assert probe._reply_verdict(operation, entries, items)["complete"] is True


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("native_branch", "native_branch_ambiguous"),
        ("native_final", "native_assistant_not_successful"),
        ("native_literal", "native_assistant_literal_mismatch"),
        ("public_final", "public_final_interrupted"),
        ("public_literal", "public_native_assistant_mismatch"),
        ("public_status", "public_projection_not_completed"),
        ("public_prefix", "public_baseline_changed"),
    ],
)
def test_provider_public_prompt_does_not_relax_existing_reply_checks(change, reason):
    probe = _provider_probe()
    operation, entries, items = _provider_reply(probe)
    if change == "native_branch":
        entries[-1]["parentId"] = None
    elif change == "native_final":
        entries[-1]["message"]["stopReason"] = "error"
    elif change == "native_literal":
        entries[-1]["message"]["content"][0]["text"] = "WRONG"
    elif change == "public_final":
        items[-1]["interrupted"] = True
    elif change == "public_literal":
        items[-1]["content"][0]["text"] = "WRONG"
    elif change == "public_status":
        items[-1]["status"] = "in_progress"
    else:
        operation = dataclasses.replace(
            operation, public_baseline=probe._reply_rows(items[:1]), public_ids=("public-user",)
        )
        items[0]["content"][0]["text"] = "CHANGED"

    with pytest.raises(RuntimeError, match=reason):
        probe._reply_verdict(operation, entries, items)


@pytest.mark.parametrize("change", [None, "call_id", "output", "response_id", "missing_result"])
def test_provider_public_prompt_preserves_tool_projection(change):
    probe = _provider_probe()
    operation, entries, items = _provider_reply(probe)
    entries[2:2] = [
        {
            "id": "native-carrier",
            "type": "message",
            "parentId": "native-user",
            "message": {
                "role": "assistant",
                "stopReason": "toolUse",
                "content": [{"type": "toolCall", "id": "call", "name": "tool", "arguments": {}}],
            },
        },
        {
            "id": "native-result",
            "type": "message",
            "parentId": "native-carrier",
            "message": {
                "role": "toolResult",
                "toolCallId": "call",
                "toolName": "tool",
                "content": [{"type": "text", "text": "RESULT"}],
            },
        },
    ]
    entries[-1]["parentId"] = "native-result"
    items[1:1] = [
        {
            "id": "public-call",
            "type": "function_call",
            "response_id": "tool-response",
            "status": "completed",
            "call_id": "call",
            "name": "tool",
            "arguments": "{}",
        },
        {
            "id": "public-result",
            "type": "function_call_output",
            "response_id": "tool-response",
            "status": "completed",
            "call_id": "call",
            "output": "RESULT",
        },
    ]
    if change is None:
        result = probe._reply_verdict(operation, entries, items)
        assert result["complete"] is True
        assert result["predicates"]["call_ids"] == ["call"]
        assert result["predicates"]["result_ids"] == ["native-result"]
        assert result["public_user_id"] == "public-user"
        return
    if change == "missing_result":
        items.pop(2)
    else:
        items[2][change] = "WRONG"
    with pytest.raises(RuntimeError, match=r"public_.*mismatch|public_result_unmatched"):
        probe._reply_verdict(operation, entries, items)


def _provider_capture_run(probe, operation, entries, tmp_path):
    run = probe._OwnedRun.__new__(probe._OwnedRun)
    run.evidence = tmp_path
    run.journal_path = operation.journal_path
    run.session_id = operation.session_id
    run.external_id = operation.external_id
    run.scenario_deadline = None
    run.journal = lambda: entries
    run.snapshot = lambda **kwargs: {"status": "idle"}
    return run


@pytest.mark.parametrize("terminal", [False, True])
def test_provider_message_waits_for_public_prompt_with_one_deadline(
    tmp_path, monkeypatch, terminal
):
    probe = _provider_probe()
    operation, entries, items = _provider_reply(probe)
    run = _provider_capture_run(probe, operation, entries, tmp_path)
    pages = iter([[], items])
    deadlines = []

    def collect(*, deadline):
        deadlines.append(deadline)
        return next(pages)

    def baseline(prompt, literal, code, deadline):
        assert (prompt, literal, code) == ("PROMPT", "REPLY", None)
        return dataclasses.replace(operation, deadline=deadline)

    sent = []
    run.items = collect
    run.reply_operation = baseline
    run.send = lambda prompt, **kwargs: sent.append((prompt, kwargs))
    run.census = list
    run.metadata = {"tmux_target": "synthetic-pane"}
    run.tmux = lambda *args: SimpleNamespace(stdout="REPLY\n", returncode=0)
    monkeypatch.setattr(
        probe, "time", SimpleNamespace(monotonic=lambda: 100.0, sleep=lambda _: None)
    )

    result = run.message("reply", "PROMPT", "REPLY", terminal=terminal, timeout=10)

    assert result["public_user_id"] == "public-user"
    assert deadlines == [110.0, 110.0]
    assert sent == [("PROMPT", {"terminal": terminal, "deadline": 110.0})]
    assert json.loads((tmp_path / "reply.json").read_text())["public_reply_id"] == "public-final"


def test_provider_capture_missing_prompt_expires_without_resetting_deadline(tmp_path, monkeypatch):
    probe = _provider_probe()
    operation, entries, items = _provider_reply(probe)
    operation = dataclasses.replace(operation, deadline=0.1)
    run = _provider_capture_run(probe, operation, entries, tmp_path)
    now = [0.0]
    run.items = lambda **kwargs: items[1:]
    monkeypatch.setattr(
        probe,
        "time",
        SimpleNamespace(
            monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds)
        ),
    )

    with pytest.raises(RuntimeError, match="wait_scenario_deadline"):
        run.completed_reply(operation, "missing")

    receipt = json.loads((tmp_path / "missing-native-predicates.json").read_text())
    assert receipt["public_user_count"] == 0
    assert receipt["public_final_position"] == 0
    assert receipt["reason"] == "wait_scenario_deadline"
    assert now[0] == 0.1


def test_provider_capture_preserves_rejected_prompt_evidence(tmp_path):
    probe = _provider_probe()
    operation, entries, items = _provider_reply(probe)
    run = _provider_capture_run(probe, operation, entries, tmp_path)
    run.items = lambda **kwargs: list(reversed(items))

    with pytest.raises(RuntimeError, match="public_owned_prompt_after_final"):
        run.completed_reply(operation, "late")

    receipt = json.loads((tmp_path / "late-native-predicates.json").read_text())
    assert receipt["public_user_ids"] == ["public-user"]
    assert receipt["public_user_positions"] == [1]
    assert receipt["public_final_position"] == 0
    assert receipt["public_user_before_final"] is False


def _adapter_probe():
    scripts = Path(__file__).parents[1] / ".agents/skills/verify-prime-native/scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location(
        "prime_native_adapter_probe_for_test", scripts / "adapter_probe.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _snapshot(probe, *artifacts: tuple[str, int, str]):
    return probe.SourceSnapshot(
        tuple(probe.SourceArtifact(path, size, digest) for path, size, digest in artifacts)
    )


def _kernel_pair(probe):
    requested = probe.RequestedKernel(
        entry=Path("/kernel/bin/python"),
        venv_root=Path("/kernel"),
        rlm_module=Path("/kernel/rlm.py"),
        rlm_sha256="sha",
    )
    actual = probe.ActualKernelIdentity(
        nonce="nonce",
        pid=42,
        executable=Path("/kernel/bin/python"),
        prefix=Path("/kernel"),
        rlm_module=Path("/kernel/rlm.py"),
        rlm_sha256="sha",
    )
    return requested, actual


def _kernel_witness() -> dict[str, object]:
    return {
        "nonce": "nonce",
        "pid": 42,
        "executable": "/kernel/bin/python",
        "prefix": "/kernel",
        "rlm_module": "/kernel/rlm.py",
        "rlm_sha256": "sha",
    }


def _kernel_record(pid: int = 42, entry: str = "/kernel/bin/python") -> dict[str, object]:
    return {"pid": pid, "argv": [entry, "-m", "rlm.repl"]}


def _selected_root_identity(probe, *, worker_pid: int = 12, worker_started: float = 2.0):
    processes = (
        probe.ProcessIdentity(
            10,
            1.0,
            ("prime-agent", "--extension", "/runtime/root/omnigent_pi_native_extension.js"),
        ),
        probe.ProcessIdentity(
            11,
            1.5,
            (
                "prime-agent",
                "--mode",
                "daemon",
                "--daemon-socket",
                "/runtime/root/tmp/daemon.sock",
            ),
        ),
        probe.ProcessIdentity(
            worker_pid,
            worker_started,
            (
                "prime-agent",
                "--mode",
                "daemon",
                "--daemon-socket",
                "/runtime/root/tmp/worker-root.sock",
            ),
        ),
        probe.ProcessIdentity(13, 3.0, ("/kernel/bin/python", "-m", "rlm.repl")),
    )
    return probe.SelectedRootIdentity(
        session_id="session-1",
        config_path="/runtime/root/controls/config.json",
        bridge_root="/runtime/root",
        agent_dir="/runtime/root/agent",
        binding_pid=10,
        binding_started=1.0,
        binding_incarnation="incarnation-1",
        external_session_id="prime-session-1",
        processes=processes,
        roles=probe.qualify_selected_root_roles(
            processes, binding_pid=worker_pid, kernel_entry="/kernel/bin/python"
        ),
    )


def test_selected_root_identity_ignores_other_root_helper_churn():
    probe = _adapter_probe()
    before = _selected_root_identity(probe)
    after = _selected_root_identity(probe)
    raw_before = [{"pid": 90, "started": 9.0, "argv": ["prime-agent", "shutdown"]}]
    raw_after = [{"pid": 91, "started": 10.0, "argv": ["prime-agent", "shutdown"]}]

    probe.assert_same_selected_root_identity(before, after)

    assert raw_before != raw_after
    assert probe.selected_root_identity_record(before)["processes"][2] == {
        "pid": 12,
        "started": 2.0,
        "argv": [
            "prime-agent",
            "--mode",
            "daemon",
            "--daemon-socket",
            "/runtime/root/tmp/worker-root.sock",
        ],
    }


def test_selected_root_rejects_missing_supervisor_role():
    probe = _adapter_probe()
    root = _selected_root_identity(probe)

    with pytest.raises(RuntimeError, match="supervisor"):
        probe.qualify_selected_root_roles(
            tuple(process for process in root.processes if process.pid != 11),
            binding_pid=12,
            kernel_entry="/kernel/bin/python",
        )


def test_selected_root_ignores_transient_extra_process_when_roles_are_stable():
    probe = _adapter_probe()
    before = _selected_root_identity(probe)
    extra = probe.ProcessIdentity(90, 9.0, ("prime-helper",))
    after = dataclasses.replace(before, processes=(*before.processes, extra))

    probe.assert_same_selected_root_identity(before, after)


def test_selected_root_identity_rejects_worker_replacement_with_stable_binding():
    probe = _adapter_probe()
    before = _selected_root_identity(probe)
    after = _selected_root_identity(probe, worker_pid=14, worker_started=4.0)

    with pytest.raises(RuntimeError, match="selected Prime root process identity"):
        probe.assert_same_selected_root_identity(before, after)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("binding_pid", 14, "binding"),
        ("binding_started", 4.0, "binding"),
        ("binding_incarnation", "incarnation-2", "binding"),
        ("external_session_id", "prime-session-2", "external session"),
    ],
)
def test_selected_root_identity_rejects_binding_or_native_session_change(field, value, message):
    probe = _adapter_probe()
    before = _selected_root_identity(probe)
    after = dataclasses.replace(before, **{field: value})

    with pytest.raises(RuntimeError, match=message):
        probe.assert_same_selected_root_identity(before, after)


_SPAWNER = """
import json, subprocess, sys
for line in sys.stdin:
    child = subprocess.Popen(json.loads(line), stdout=subprocess.DEVNULL)
    print(child.pid, flush=True)
"""
_SETUID_TOP = Path("/usr/bin/top")


@contextlib.contextmanager
def _process_tree(env: dict[str, str]):
    root = subprocess.Popen(
        [sys.executable, "-c", _SPAWNER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, **env},
    )
    assert root.stdin is not None and root.stdout is not None

    def spawn(argv: list[str]) -> int:
        root.stdin.write(json.dumps(argv) + "\n")
        root.stdin.flush()
        return int(root.stdout.readline())

    try:
        yield root, spawn
    finally:
        for child in psutil.Process(root.pid).children(recursive=True):
            with contextlib.suppress(psutil.NoSuchProcess):
                child.kill()
        root.kill()
        root.wait(timeout=5)


def _spawn_unreadable(probe, monkeypatch, spawn, kind: str) -> tuple[int, str]:
    """Spawn a child whose argv and environment cannot be read, as macOS reports setuid ps."""
    if kind == "setuid":
        if (
            sys.platform != "darwin"
            or os.getuid() == 0
            or not _SETUID_TOP.stat().st_mode & stat.S_ISUID
        ):
            pytest.skip("needs a non-root macOS user and setuid /usr/bin/top")
        return spawn([str(_SETUID_TOP), "-l", "0", "-s", "1"]), str(_SETUID_TOP)
    pid = spawn([sys.executable, "-c", "import time; time.sleep(60)"])
    exe = probe.psutil.Process(pid).exe()
    for name in ("cmdline", "environ"):
        original = getattr(probe.psutil.Process, name)

        def hidden(self, original=original):
            if self.pid == pid:
                # psutil raises SystemError from proc_cmdline for an exiting process.
                raise (
                    SystemError("proc_cmdline")
                    if kind == "exiting"
                    else probe.psutil.AccessDenied(pid)
                )
            return original(self)

        monkeypatch.setattr(probe.psutil.Process, name, hidden)
    return pid, exe


def _census_run(probe, tmp_path: Path):
    run = probe._OwnedRun.__new__(probe._OwnedRun)
    run.evidence = tmp_path / "evidence"
    run.runtime = tmp_path / "runtime"
    run.evidence.mkdir()
    (run.runtime / "data").mkdir(parents=True)
    run.url = "http://127.0.0.1:1"
    run.session_id = None
    run.owners = {}
    run.census_errors = set()
    run.unowned_unreadable = {}
    run.bridge_roots = set()
    run.foreground_host = None
    run.foreground_host_identity = None
    run.host_log_handle = None
    return run


@pytest.mark.parametrize("kind", ["setuid", "simulated", "exiting"])
def test_census_owns_an_unreadable_descendant_through_its_owned_parent(
    tmp_path, monkeypatch, kind
):
    probe = _provider_probe()
    run = _census_run(probe, tmp_path)
    with _process_tree({"OMNIGENT_DATA_DIR": str(run.runtime / "data")}) as (root, spawn):
        helper, exe = _spawn_unreadable(probe, monkeypatch, spawn, kind)
        started = probe.psutil.Process(helper).create_time()
        observations = [run.census(), run.census()]

    for records in observations:
        assert [record for record in records if record["pid"] == helper] == [
            {
                "pid": helper,
                "started": started,
                "argv": [],
                "ownership": "initial_owned_descendant_unreadable",
            }
        ]
        assert root.pid in {record["pid"] for record in records}
    assert run.census_errors == set()
    receipt = json.loads((run.evidence / "progress.json").read_text())
    assert [owner for owner in receipt["owners"] if owner["pid"] == helper] == [
        {
            "pid": helper,
            "started": started,
            "argv": [],
            "exe": exe,
            "ownership": "owned_descendant_unreadable",
        }
    ]


@pytest.mark.parametrize("kind", ["simulated", "exiting"])
def test_census_reports_an_unreadable_process_outside_the_owned_tree(tmp_path, monkeypatch, kind):
    probe = _provider_probe()
    run = _census_run(probe, tmp_path)
    owned_env = {"OMNIGENT_DATA_DIR": str(run.runtime / "data")}
    with _process_tree(owned_env) as (root, _), _process_tree({}) as (_, spawn_foreign):
        foreign, exe = _spawn_unreadable(probe, monkeypatch, spawn_foreign, kind)
        started = probe.psutil.Process(foreign).create_time()
        records = run.census()

    assert root.pid in {record["pid"] for record in records}
    assert foreign not in {record["pid"] for record in records}
    assert foreign not in {pid for pid, _ in run.owners}
    receipt = json.loads((run.evidence / "progress.json").read_text())
    assert [item for item in receipt["unowned_unreadable"] if item["pid"] == foreign] == [
        {"pid": foreign, "started": started, "exe": exe}
    ]


def _selected_root_run(probe, tmp_path: Path, monkeypatch):
    run = _census_run(probe, tmp_path)
    bridge = run.runtime / "bridge"
    (bridge / "controls").mkdir(parents=True)
    (bridge / "agent").mkdir()
    config = bridge / "config.json"
    config.write_text(
        json.dumps({"sessionId": "session-1", "primeControlsDir": str(bridge / "controls")})
    )
    (bridge / "controls/binding.json").write_text(json.dumps({"pid": 1, "incarnation": "i-1"}))
    run.session_id = "session-1"
    run.external_id = "prime-session-1"
    run.config_path = config
    run.admit_config = lambda path: json.loads(path.read_text())
    run.request = SimpleNamespace(prime_path=Path(sys.executable))
    run.requested_kernel = SimpleNamespace(entry=Path("/absent/kernel/python"))
    monkeypatch.setattr(
        probe,
        "qualify_selected_root_roles",
        lambda identities, **_: SimpleNamespace(worker=SimpleNamespace(started=1.0)),
    )
    env = {
        "PRIME_AGENT_CODING_AGENT_DIR": str(bridge / "agent"),
        "OMNIGENT_EXTENSION_NATIVE_CONFIG": str(config),
    }
    return run, env


def _spawn_after_census(run, spawn_child):
    census = run.census
    spawned: list[int] = []

    def census_then_spawn():
        records = census()
        if not spawned:
            spawned.append(spawn_child())
        return records

    run.census = census_then_spawn
    return spawned


@pytest.mark.parametrize("timing", ["before_census", "after_census"])
@pytest.mark.parametrize("kind", ["readable", "setuid", "simulated"])
def test_selected_root_owns_descendants_of_the_qualified_root(tmp_path, monkeypatch, kind, timing):
    probe = _provider_probe()
    run, env = _selected_root_run(probe, tmp_path, monkeypatch)
    with _process_tree(env) as (_, spawn):

        def spawn_child() -> int:
            if kind == "readable":
                return spawn([sys.executable, "-c", "import time; time.sleep(60)"])
            return _spawn_unreadable(probe, monkeypatch, spawn, kind)[0]

        spawned = [spawn_child()] if timing == "before_census" else []
        if not spawned:
            spawned = _spawn_after_census(run, spawn_child)
        run.selected_root(passive=True)
        child = spawned[0]
        started = probe.psutil.Process(child).create_time()
        ownership = run.owners[(child, started)]["ownership"]
        later_census = probe._OwnedRun.census(run)

    assert ownership == {
        ("readable", "before_census"): "exact_run",
        ("setuid", "before_census"): "owned_descendant_unreadable",
        ("simulated", "before_census"): "owned_descendant_unreadable",
    }.get((kind, timing), "selected_descendant")
    assert child in {record["pid"] for record in later_census}
    assert run.census_errors == set()


def test_selected_root_rejects_a_descendant_owned_by_another_user(tmp_path, monkeypatch):
    probe = _provider_probe()
    run, env = _selected_root_run(probe, tmp_path, monkeypatch)
    with _process_tree(env) as (_, spawn):
        spawned = _spawn_after_census(
            run, lambda: spawn([sys.executable, "-c", "import time; time.sleep(60)"])
        )
        uids = probe.psutil.Process.uids

        def foreign_uids(self):
            if spawned and self.pid == spawned[0]:
                return SimpleNamespace(real=os.getuid() + 1)
            return uids(self)

        monkeypatch.setattr(probe.psutil.Process, "uids", foreign_uids)
        with pytest.raises(RuntimeError) as failure:
            run.selected_root(passive=True)
        exe = probe.psutil.Process(spawned[0]).exe()

    assert str(failure.value) == f"selected_descendant_foreign_uid {spawned[0]} {exe}"
    assert spawned[0] not in {pid for pid, _ in run.owners}


class _OwnedHostProcess:
    def __init__(self, *, pid: int, started: float, data_dir: Path):
        self.pid = pid
        self._started = started
        self._data_dir = data_dir

    def create_time(self) -> float:
        return self._started

    def environ(self) -> dict[str, str]:
        return {"OMNIGENT_DATA_DIR": str(self._data_dir)}


def test_owned_host_qualification_requires_exact_target_pid_start_and_data_dir(tmp_path: Path):
    probe = _adapter_probe()
    data_dir = tmp_path / "data"
    record = {
        "pid": 42,
        "target": "http://127.0.0.1:8123",
        "mode": "server",
        "server_url": "http://127.0.0.1:8123",
        "started_at": 100,
    }

    qualified = probe.qualify_owned_host_record(
        record,
        "http://127.0.0.1:8123",
        data_dir,
        _OwnedHostProcess(pid=42, started=99.5, data_dir=data_dir),
    )

    assert qualified == {"pid": 42, "started": 99.5, "target": "http://127.0.0.1:8123"}


@pytest.mark.parametrize(
    ("record_update", "process", "message"),
    [
        ({"target": "http://other:8123"}, _OwnedHostProcess, "target"),
        ({"started_at": 10}, _OwnedHostProcess, "creation time"),
    ],
)
def test_owned_host_qualification_rejects_unrelated_or_reused_process(
    tmp_path: Path, record_update, process, message
):
    probe = _adapter_probe()
    data_dir = tmp_path / "data"
    record = {
        "pid": 42,
        "target": "http://127.0.0.1:8123",
        "mode": "server",
        "server_url": "http://127.0.0.1:8123",
        "started_at": 100,
    }
    record.update(record_update)
    started = 99.5 if message == "target" else 20.0

    with pytest.raises(RuntimeError, match=message):
        probe.qualify_owned_host_record(
            record,
            "http://127.0.0.1:8123",
            data_dir,
            process(pid=42, started=started, data_dir=data_dir),
        )


def test_cleanup_verdict_rejects_own_survivor_or_forced_native_fallback():
    probe = _adapter_probe()

    assert not probe.cleanup_is_complete(
        final_owned_census=[{"pid": 42, "started": 1.0, "ownership": "exact_data_dir"}],
        forced_native_fallback=False,
        server_stopped=True,
        fixture_stopped=True,
        mcp_stopped=True,
        errors=[],
    )
    assert not probe.cleanup_is_complete(
        final_owned_census=[],
        forced_native_fallback=True,
        server_stopped=True,
        fixture_stopped=True,
        mcp_stopped=True,
        errors=[],
    )


def test_final_cleanup_retains_a_live_initial_identity_when_fresh_scan_misses_it(
    monkeypatch: pytest.MonkeyPatch,
):
    probe = _adapter_probe()

    class Process:
        pid = 42

        def create_time(self):
            return 1.5

        def is_running(self):
            return True

        def status(self):
            return "sleeping"

        def cmdline(self):
            return ["python", "-m", "rlm.repl"]

    monkeypatch.setattr(probe.psutil, "Process", lambda pid: Process())
    initial = [{"pid": 42, "started": 1.5, "ownership": "exact_data_dir"}]

    retained = probe.live_initial_process_records(initial)

    assert retained == [
        {
            "pid": 42,
            "started": 1.5,
            "argv": ["python", "-m", "rlm.repl"],
            "ownership": "initial_exact_data_dir",
        }
    ]
    assert not probe.cleanup_is_complete(
        final_owned_census=retained,
        forced_native_fallback=False,
        server_stopped=True,
        fixture_stopped=True,
        mcp_stopped=True,
        errors=[],
    )


def test_final_cleanup_fails_when_initial_identity_becomes_unreadable(
    monkeypatch: pytest.MonkeyPatch,
):
    probe = _adapter_probe()

    class Process:
        def create_time(self):
            raise probe.psutil.AccessDenied(pid=42)

    monkeypatch.setattr(probe.psutil, "Process", lambda pid: Process())

    retained = probe.live_initial_process_records(
        [{"pid": 42, "started": 1.5, "ownership": "exact_data_dir"}]
    )

    assert len(retained) == 1
    assert "AccessDenied" in retained[0]["process_error"]
    assert not probe.cleanup_is_complete(
        final_owned_census=retained,
        forced_native_fallback=False,
        server_stopped=True,
        fixture_stopped=True,
        mcp_stopped=True,
        errors=[],
    )


def test_final_cleanup_does_not_claim_a_reused_pid(monkeypatch: pytest.MonkeyPatch):
    probe = _adapter_probe()

    class Process:
        def create_time(self):
            return 2.0

    monkeypatch.setattr(probe.psutil, "Process", lambda pid: Process())

    assert (
        probe.live_initial_process_records(
            [{"pid": 42, "started": 1.5, "ownership": "exact_data_dir"}]
        )
        == []
    )


def test_cleanup_records_failed_session_delete():
    probe = _adapter_probe()
    run = object.__new__(probe.AdapterRun)
    run.url = "http://127.0.0.1:8123"
    run.server = type("Server", (), {"poll": lambda self: None})()
    run.owned_session_ids = ["session-1"]

    class Client:
        def delete(self, path: str) -> httpx.Response:
            assert path == "/v1/sessions/session-1"
            return httpx.Response(
                503,
                text="unavailable",
                request=httpx.Request("DELETE", run.url + path),
            )

    run.client = lambda **kwargs: contextlib.nullcontext(Client())
    steps: list[object] = []
    errors: list[object] = []

    run.delete_owned_sessions(steps, errors)

    assert steps == [{"delete_session": "session-1", "status": 503, "body": "unavailable"}]
    assert len(errors) == 1
    assert "503" in str(errors[0])


def test_stop_owned_host_uses_exact_private_env_and_never_stops_other_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    probe = _adapter_probe()
    run = object.__new__(probe.AdapterRun)
    run.url = "http://127.0.0.1:8123"
    run.runtime = tmp_path / "runtime"
    run.workspace = tmp_path / "workspace"
    data_dir = run.runtime / "data"
    run.env = lambda: {"OMNIGENT_DATA_DIR": str(data_dir)}
    own_record = {
        "pid": 42,
        "target": run.url,
        "mode": "server",
        "server_url": run.url,
        "started_at": 100,
    }
    run.own_host_registry = lambda: [
        {"path": str(data_dir / "daemons" / "own.json"), "record": own_record},
        {
            "path": str(data_dir / "daemons" / "other.json"),
            "record": {**own_record, "pid": 43, "target": "http://other:8123"},
        },
    ]
    monkeypatch.setattr(
        probe.psutil,
        "Process",
        lambda pid: _OwnedHostProcess(pid=pid, started=99.5, data_dir=data_dir),
    )
    calls: list[dict[str, object]] = []

    class Result:
        returncode = 0
        stdout = "stopped\n"
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        return Result()

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    steps = []
    errors = []

    run.stop_owned_host(steps, errors)

    assert errors == []
    assert calls == [
        {
            "argv": [
                str(probe.REPO / ".venv/bin/omnigent"),
                "host",
                "stop",
                "--server",
                run.url,
                "--daemon-only",
            ],
            "cwd": run.workspace,
            "env": {"OMNIGENT_DATA_DIR": str(data_dir)},
            "capture_output": True,
            "text": True,
            "timeout": 30,
        }
    ]
    assert steps[0]["host_stop"]["qualified"] == {
        "pid": 42,
        "started": 99.5,
        "target": run.url,
    }


def test_source_snapshot_rejects_byte_drift():
    probe = _adapter_probe()
    before = _snapshot(probe, ("omnigent/runner/app.py", 3, "before"))
    after = _snapshot(probe, ("omnigent/runner/app.py", 3, "after"))

    with pytest.raises(probe.SourceIntegrityError, match="Source integrity changed"):
        probe.assert_same_sources(before, after)

    comparison = probe.compare_source_snapshots(before, after)
    assert comparison["changed"] == [
        {
            "path": "omnigent/runner/app.py",
            "before": {"path": "omnigent/runner/app.py", "bytes": 3, "sha256": "before"},
            "after": {"path": "omnigent/runner/app.py", "bytes": 3, "sha256": "after"},
        }
    ]


@pytest.mark.parametrize(
    ("before", "after", "key", "expected"),
    [
        (
            (("omnigent/host/connect.py", 1, "a"),),
            (("omnigent/host/connect.py", 1, "a"), ("omnigent/new.py", 1, "b")),
            "added",
            ["omnigent/new.py"],
        ),
        (
            (("omnigent/host/connect.py", 1, "a"), ("omnigent/old.py", 1, "b")),
            (("omnigent/host/connect.py", 1, "a"),),
            "removed",
            ["omnigent/old.py"],
        ),
    ],
)
def test_source_snapshot_rejects_membership_changes(before, after, key, expected):
    probe = _adapter_probe()

    comparison = probe.compare_source_snapshots(
        _snapshot(probe, *before), _snapshot(probe, *after)
    )

    assert comparison[key] == expected
    with pytest.raises(probe.SourceIntegrityError, match="Source integrity changed"):
        probe.assert_same_sources(_snapshot(probe, *before), _snapshot(probe, *after))


def test_checkout_origin_rejects_a_module_outside_the_managed_checkout(tmp_path: Path):
    probe = _adapter_probe()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(probe.SourceIntegrityError, match="outside the exact worktree"):
        probe.assert_checkout_origin("omnigent.host.connect", outside, checkout)


def test_deployed_extension_identity_rejects_different_bytes(tmp_path: Path):
    probe = _adapter_probe()
    source = tmp_path / "packaged.js"
    deployed = tmp_path / "deployed.js"
    source.write_text("export const value = 'source';\n", encoding="utf-8")
    deployed.write_text("export const value = 'runtime';\n", encoding="utf-8")

    with pytest.raises(probe.SourceIntegrityError, match="Deployed extension bytes differ"):
        probe.deployed_extension_identity(source, deployed)


def test_fatal_failure_overrides_ten_verified_scenarios(tmp_path: Path):
    probe = _adapter_probe()
    evidence = probe.Evidence(tmp_path)
    for name in probe.REQUIRED:
        evidence.verified(name)

    evidence.record_fatal(RuntimeError("source changed after scenarios"))

    assert evidence.final_verdict == "NOT VERIFIED"
    assert evidence.fatal == [{"type": "RuntimeError", "error": "source changed after scenarios"}]


def test_kernel_identity_qualifies_one_owned_requested_kernel():
    probe = _adapter_probe()
    requested, actual = _kernel_pair(probe)

    probe.qualify_kernel_identity(
        requested,
        actual,
        "nonce",
        [_kernel_record()],
    )


def test_kernel_identity_rejects_another_owned_kernel_with_wrong_argv():
    probe = _adapter_probe()
    requested, actual = _kernel_pair(probe)

    with pytest.raises(RuntimeError, match="owned process argv differs"):
        probe.qualify_kernel_identity(
            requested,
            actual,
            "nonce",
            [_kernel_record(), _kernel_record(43, "/other/bin/python")],
        )


def test_kernel_preflight_keeps_a_real_venv_symlink_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    probe = _adapter_probe()
    checkout = tmp_path / "checkout"
    package = checkout / "rlm"
    package.mkdir(parents=True)
    source = package / "__init__.py"
    source.write_text(
        "def spawn():\n    pass\n\ndef collect():\n    pass\n",
        encoding="utf-8",
    )
    venv = tmp_path / "venv"
    probe.subprocess.check_call([sys.executable, "-m", "venv", "--without-pip", str(venv)])
    entry = venv / "bin" / "python"
    assert entry.is_symlink()
    monkeypatch.setattr(probe, "REPO", checkout)

    requested, payload = probe.requested_kernel_identity(entry)

    assert requested.entry == entry
    assert requested.venv_root == venv.resolve()
    assert requested.rlm_module == source.resolve()
    assert requested.rlm_sha256 == probe.sha256(source)
    assert payload["executable"] == str(entry)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("executable", "/other/bin/python", "executable"),
        ("prefix", "/other", "prefix"),
        ("module_sha256", "wrong-sha", "hash"),
    ],
)
def test_kernel_preflight_rejects_wrong_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    message: str,
):
    probe = _adapter_probe()
    venv = tmp_path / "venv"
    entry = venv / "bin" / "python"
    entry.parent.mkdir(parents=True)
    entry.write_text("", encoding="utf-8")
    module = venv / "rlm.py"
    module.write_text("rlm", encoding="utf-8")
    payload = {
        "executable": str(entry),
        "prefix": str(venv),
        "module": str(module),
        "module_sha256": probe.sha256(module),
    }
    payload[field] = value
    monkeypatch.setattr(
        probe.subprocess,
        "check_output",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    with pytest.raises(RuntimeError, match=message):
        probe.requested_kernel_identity(entry)


def test_kernel_identity_rejects_a_missing_witness_file(tmp_path: Path):
    probe = _adapter_probe()

    with pytest.raises(RuntimeError, match="sidecar is missing"):
        probe.read_actual_kernel_identity(tmp_path / "kernel-identity.json")


@pytest.mark.parametrize("contents", ["{", "[]"])
def test_kernel_identity_rejects_malformed_or_non_object_json(tmp_path: Path, contents: str):
    probe = _adapter_probe()
    sidecar = tmp_path / "kernel-identity.json"
    sidecar.write_text(contents, encoding="utf-8")

    with pytest.raises(RuntimeError, match="sidecar is malformed"):
        probe.read_actual_kernel_identity(sidecar)


@pytest.mark.parametrize("field", list(_kernel_witness()))
def test_kernel_identity_rejects_missing_witness_fields(tmp_path: Path, field: str):
    probe = _adapter_probe()
    payload = _kernel_witness()
    del payload[field]
    sidecar = tmp_path / "kernel-identity.json"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match=field):
        probe.read_actual_kernel_identity(sidecar)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("nonce", 1),
        ("pid", True),
        ("executable", 1),
        ("prefix", 1),
        ("rlm_module", 1),
        ("rlm_sha256", 1),
    ],
)
def test_kernel_identity_rejects_wrongly_typed_witness_fields(
    tmp_path: Path, field: str, value: object
):
    probe = _adapter_probe()
    payload = _kernel_witness()
    payload[field] = value
    sidecar = tmp_path / "kernel-identity.json"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match=field):
        probe.read_actual_kernel_identity(sidecar)


def test_kernel_identity_rejects_a_nonpositive_pid(tmp_path: Path):
    probe = _adapter_probe()
    payload = _kernel_witness()
    payload["pid"] = 0
    sidecar = tmp_path / "kernel-identity.json"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="pid"):
        probe.read_actual_kernel_identity(sidecar)


def test_kernel_identity_rejects_stale_nonce():
    probe = _adapter_probe()
    requested, actual = _kernel_pair(probe)

    with pytest.raises(RuntimeError, match="nonce"):
        probe.qualify_kernel_identity(
            requested,
            dataclasses.replace(actual, nonce="stale"),
            "current",
            [_kernel_record()],
        )


def test_kernel_identity_rejects_duplicate_witness_pid():
    probe = _adapter_probe()
    requested, actual = _kernel_pair(probe)

    with pytest.raises(RuntimeError, match="exactly one"):
        probe.qualify_kernel_identity(
            requested,
            actual,
            "nonce",
            [_kernel_record(), _kernel_record()],
        )


def test_kernel_identity_rejects_wrong_witnessed_argv():
    probe = _adapter_probe()
    requested, actual = _kernel_pair(probe)

    with pytest.raises(RuntimeError, match="argv"):
        probe.qualify_kernel_identity(
            requested,
            actual,
            "nonce",
            [{"pid": 42, "argv": ["/kernel/bin/python", "-m", "wrong"]}],
        )


def test_kernel_identity_rejects_an_unowned_witness_pid():
    probe = _adapter_probe()
    requested, actual = _kernel_pair(probe)

    with pytest.raises(RuntimeError, match="exactly one"):
        probe.qualify_kernel_identity(requested, actual, "nonce", [_kernel_record(99)])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("executable", Path("/wrong/bin/python"), "executable"),
        ("prefix", Path("/wrong"), "prefix"),
        ("rlm_module", Path("/wrong/rlm.py"), "module"),
        ("rlm_sha256", "wrong-sha", "hash"),
    ],
)
def test_kernel_identity_rejects_mismatched_requested_fields(field, value, message):
    probe = _adapter_probe()
    requested, actual = _kernel_pair(probe)

    with pytest.raises(RuntimeError, match=message):
        probe.qualify_kernel_identity(
            requested,
            dataclasses.replace(actual, **{field: value}),
            "nonce",
            [_kernel_record()],
        )


def test_kernel_identity_rejects_matching_bytes_from_another_venv():
    probe = _adapter_probe()
    requested, actual = _kernel_pair(probe)

    with pytest.raises(RuntimeError, match="prefix"):
        probe.qualify_kernel_identity(
            requested,
            dataclasses.replace(actual, prefix=Path("/other-kernel")),
            "nonce",
            [_kernel_record()],
        )


def test_kernel_identity_accepts_resolved_directory_aliases_and_lexical_entry(
    tmp_path: Path,
):
    probe = _adapter_probe()
    venv = tmp_path / "venv"
    entry = venv / "bin" / "python"
    module = venv / "lib" / "rlm" / "__init__.py"
    entry.parent.mkdir(parents=True)
    entry.symlink_to(sys.executable)
    module.parent.mkdir(parents=True)
    module.write_text("rlm", encoding="utf-8")
    prefix_alias = tmp_path / "prefix-alias"
    prefix_alias.symlink_to(venv, target_is_directory=True)
    module_alias = tmp_path / "module-alias"
    module_alias.symlink_to(module.parent, target_is_directory=True)
    requested = probe.RequestedKernel(entry, venv.resolve(), module.resolve(), "sha")
    actual = probe.ActualKernelIdentity(
        "nonce",
        42,
        entry,
        prefix_alias,
        module_alias / "__init__.py",
        "sha",
    )

    probe.qualify_kernel_identity(requested, actual, "nonce", [_kernel_record(42, str(entry))])


def test_kernel_identity_failure_makes_memory_not_verified_and_still_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    probe = _adapter_probe()
    before = _snapshot(probe, ("omnigent/runner/app.py", 1, "before"))
    cleanup_called: list[bool] = []
    requested, actual = _kernel_pair(probe)

    class FailingRun:
        def __init__(self, _args, evidence, requested_kernel):
            self.evidence = evidence
            self.requested_kernel = requested_kernel

        def start(self):
            return

        def cleanup(self):
            cleanup_called.append(True)
            return True, {"owned_survivors": []}

    class ParsedArgs:
        serve_mcp = False
        prime_path = tmp_path / "prime-agent"
        kernel_python = tmp_path / "kernel-python"
        evidence_dir = tmp_path
        production_ready = None

    class ParsedParser:
        def parse_args(self):
            return ParsedArgs()

    monkeypatch.setattr(probe, "parser", lambda: ParsedParser())
    monkeypatch.setattr(probe, "source_snapshot", lambda: before)
    monkeypatch.setattr(probe, "source_identity", lambda _args, _snapshot: ({}, requested))
    monkeypatch.setattr(probe, "AdapterRun", FailingRun)

    def failing_drive(run):
        for name in probe.REQUIRED:
            if name not in {"living_python_memory", "owned_cleanup"}:
                run.evidence.verified(name)
        assert not probe.exercise(
            "living_python_memory",
            run.evidence,
            lambda: probe.qualify_kernel_identity(
                run.requested_kernel,
                actual,
                "nonce",
                [_kernel_record(), _kernel_record(43, "/other/bin/python")],
            ),
        )

    monkeypatch.setattr(probe, "drive", failing_drive)

    assert probe.main() == 1
    stdout = capsys.readouterr().out
    outcomes = json.loads((tmp_path / "outcomes.json").read_text(encoding="utf-8"))
    integrity = json.loads((tmp_path / "source-integrity.json").read_text(encoding="utf-8"))
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))

    assert cleanup_called == [True]
    assert len(outcomes["scenarios"]) == 10
    assert set(outcomes["scenarios"]) == set(probe.REQUIRED)
    assert outcomes["scenarios"]["living_python_memory"] == {
        "actions": [],
        "observations": [],
        "reason": "RuntimeError: Kernel identity owned process argv differs",
        "verdict": "NOT VERIFIED",
    }
    assert outcomes["scenarios"]["owned_cleanup"]["verdict"] == "VERIFIED"
    assert all(
        scenario["verdict"] == "VERIFIED"
        for name, scenario in outcomes["scenarios"].items()
        if name != "living_python_memory"
    )
    assert outcomes["fatal"] == []
    assert outcomes["final_verdict"] == "NOT VERIFIED"
    assert integrity["comparison"] == {"added": [], "removed": [], "changed": []}
    assert manifest["exit"] == 1
    assert manifest["final_verdict"] == "NOT VERIFIED"
    assert "NOT VERIFIED final" in stdout


def test_cleanup_and_integrity_failures_are_both_retained(tmp_path: Path, monkeypatch):
    probe = _adapter_probe()
    before = _snapshot(probe, ("omnigent/runner/native/interrupt.py", 1, "before"))
    after = _snapshot(probe, ("omnigent/runner/native/interrupt.py", 1, "after"))

    class FailingRun:
        def __init__(self, _args, _evidence, _requested):
            return

        def start(self):
            raise RuntimeError("scenario setup failed")

        def cleanup(self):
            raise RuntimeError("cleanup failed")

    class ParsedArgs:
        serve_mcp = False
        prime_path = tmp_path / "prime-agent"
        kernel_python = tmp_path / "kernel-python"
        evidence_dir = tmp_path
        production_ready = None

    class ParsedParser:
        def parse_args(self):
            return ParsedArgs()

    snapshots = iter((before, after))
    monkeypatch.setattr(probe, "parser", lambda: ParsedParser())
    monkeypatch.setattr(probe, "source_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(probe, "source_identity", lambda _args, _snapshot: ({}, object()))
    monkeypatch.setattr(probe, "AdapterRun", FailingRun)

    assert probe.main() == 1
    outcomes = (tmp_path / "outcomes.json").read_text(encoding="utf-8")
    integrity = (tmp_path / "source-integrity.json").read_text(encoding="utf-8")

    assert "scenario setup failed" in outcomes
    assert "cleanup failed" in outcomes
    assert "Source integrity changed" in outcomes
    assert '"after"' in integrity


def _synthetic_owned_run(probe, tmp_path):
    source = tmp_path / "synthetic-source"
    source.write_text("SYNTHETIC SOURCE")
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    sentinels = [source, tmp_path / "compact-sibling", evidence / "sentinel"]
    for path in sentinels[1:]:
        path.write_text("SYNTHETIC OUTSIDE")
    before = {
        path: (path.stat().st_dev, path.stat().st_ino, path.read_bytes()) for path in sentinels
    }
    request = probe.Request(
        tmp_path / "no-prime",
        tmp_path / "no-kernel",
        source,
        tmp_path,
        "selector",
        cooperative_cleanup=True,
    )
    owner = probe._RuntimeOwner.allocate(request, evidence)
    run = probe._OwnedRun(request, probe.RUNNER, "selector", owner)
    run.census = list
    (run.runtime / "prime/auth.json").write_text("SYNTHETIC COPY")
    (run.runtime / "data/logs").mkdir()
    (run.runtime / "data/logs/diagnostic.log").write_text("synthetic log\n")
    return request, owner, run, before


def _assert_synthetic_sentinels(before):
    for path, expected in before.items():
        assert (path.stat().st_dev, path.stat().st_ino, path.read_bytes()) == expected


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
def test_provider_runtime_cleanup_removes_original_and_retries_known_credentials(tmp_path):
    probe = _provider_probe()
    _, owner, run, before = _synthetic_owned_run(probe, tmp_path)
    settlement = probe._ExternalSettlement(owner.allocation_id, "no_fixture", ())
    with owner:
        result = run.cleanup(settlement)
        assert result.status == "VERIFIED"
        assert owner.removed.root_identity == owner.target.directories[run.runtime]
        assert not run.runtime.exists()
        assert (run.evidence / "owned-logs/diagnostic.log").read_text() == "synthetic log\n"
        for _ in range(2):
            errors = []
            run.finalize_owned_credentials(errors)
            assert errors == []
            assert run.cleanup(settlement).status == "VERIFIED"
        with pytest.raises(RuntimeError, match="new_retired_descendant"):
            run.register_owned_credential(run.runtime / "unknown/auth.json")
        assert owner.removed.event_flags & probe.select.KQ_NOTE_DELETE
    assert owner.closed is True
    _assert_synthetic_sentinels(before)


@pytest.mark.parametrize(
    "failure",
    [
        "fixture",
        "wrong_allocation",
        "census",
        "capture",
        "symlink",
        "rename",
        "replacement",
        "denied",
    ],
)
@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
def test_provider_runtime_failure_retains_scratch_and_preserves_sentinels(
    tmp_path, monkeypatch, failure
):
    probe = _provider_probe()
    _, owner, run, before = _synthetic_owned_run(probe, tmp_path)
    settlement = probe._ExternalSettlement(owner.allocation_id, "no_fixture", ())
    original = run.runtime
    if failure == "fixture":
        settlement = probe._ExternalSettlement(
            owner.allocation_id, "failed_fixture", (), ("close failed",)
        )
    elif failure == "wrong_allocation":
        settlement = probe._ExternalSettlement("wrong", "no_fixture", ())
    elif failure == "census":

        def unreadable():
            raise OSError("census denied")

        run.census = unreadable
        monkeypatch.setattr(probe, "wait_for", lambda action, *args: action())
    elif failure == "capture":
        original_sanitize = probe.sanitize

        def bad_capture(text):
            if text == "synthetic log\n":
                raise OSError("capture failed")
            return original_sanitize(text)

        monkeypatch.setattr(probe, "sanitize", bad_capture)
    elif failure == "symlink":
        (run.runtime / "workspace/external").symlink_to(tmp_path / "compact-sibling")
    elif failure in ("rename", "replacement"):
        original.rename(tmp_path / "renamed-original")
        if failure == "replacement":
            original.mkdir()
            replacement = original / "replacement-sentinel"
            replacement.write_text("DO NOT REMOVE")
            before[replacement] = (
                replacement.stat().st_dev,
                replacement.stat().st_ino,
                replacement.read_bytes(),
            )
    elif failure == "denied":

        def denied(*args, **kwargs):
            raise PermissionError("rmdir denied")

        monkeypatch.setattr(probe.os, "rmdir", denied)
    try:
        try:
            result = run.cleanup(settlement)
        except (OSError, RuntimeError):
            result = None
        assert result is None or result.status == "FAILED"
        assert owner.removed is None
        assert (
            (tmp_path / "renamed-original").is_dir()
            if failure in ("rename", "replacement")
            else original.is_dir()
        )
    finally:
        with contextlib.suppress(RuntimeError):
            owner.__exit__(None, None, None)
        _assert_synthetic_sentinels(before)


@pytest.mark.parametrize("failure", ["receipt", "missing_event", "held_fstat", "recreated"])
@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
def test_provider_runtime_removal_failure_never_readmits_path(tmp_path, monkeypatch, failure):
    probe = _provider_probe()
    _, owner, run, before = _synthetic_owned_run(probe, tmp_path)
    settlement = probe._ExternalSettlement(owner.allocation_id, "no_fixture", ())
    original_write = probe.write_json
    if failure == "receipt":

        def bad_receipt(path, *args, **kwargs):
            if path.name == "runtime-removal.json":
                raise OSError("receipt failed")
            return original_write(path, *args, **kwargs)

        monkeypatch.setattr(probe, "write_json", bad_receipt)
    elif failure == "missing_event":
        monkeypatch.setattr(owner, "_events", lambda: 0)
    elif failure == "held_fstat":
        original_fstat = probe.os.fstat

        def fail_fstat(fd):
            if fd == owner.root_fd and not owner.root.exists():
                raise OSError("held fstat unreadable")
            return original_fstat(fd)

        monkeypatch.setattr(probe.os, "fstat", fail_fstat)
    else:
        original_rmdir = probe.os.rmdir

        def recreate(name, *, dir_fd):
            original_rmdir(name, dir_fd=dir_fd)
            if name == run.runtime.name:
                run.runtime.mkdir()
                sentinel = run.runtime / "replacement"
                sentinel.write_text("RECREATED")
                before[sentinel] = (
                    sentinel.stat().st_dev,
                    sentinel.stat().st_ino,
                    sentinel.read_bytes(),
                )

        monkeypatch.setattr(probe.os, "rmdir", recreate)
    try:
        assert run.cleanup(settlement).status == "FAILED"
        if failure not in ("missing_event", "held_fstat"):
            assert owner.removed is not None
        assert not (run.evidence / "completion.json").exists()
        errors = []
        run.finalize_owned_credentials(errors)
        assert run.cleanup(settlement).status == "FAILED"
    finally:
        with contextlib.suppress(RuntimeError):
            owner.__exit__(None, None, None)
        _assert_synthetic_sentinels(before)


def _synthetic_draft(probe, evidence):
    cleanup = probe.Observation("owned_cleanup", "VERIFIED", (), "synthetic")
    observation = probe.Observation("literal", "VERIFIED", (), "synthetic")
    result = probe.CaseResult(evidence / "result.json", ("literal",), (observation,), cleanup)
    return probe._CaseDraft(result, {"failure": None}, {"synthetic": True})


@pytest.mark.parametrize(
    "failure", [None, "result", "manifest", "completion", "chmod", "inventory", "commit"]
)
def test_provider_publication_has_one_authority(tmp_path, monkeypatch, failure):
    probe = _provider_probe()
    draft = _synthetic_draft(probe, tmp_path)
    original_write = probe.write_json
    names = {
        "result": "result.json",
        "manifest": "manifest.json",
        "completion": ".completion-pending.json",
    }
    if failure in names:

        def fail_write(path, *args, **kwargs):
            if path.name == names[failure]:
                raise OSError("publication failed")
            return original_write(path, *args, **kwargs)

        monkeypatch.setattr(probe, "write_json", fail_write)
    elif failure == "chmod":

        def fail_chmod(*args, **kwargs):
            raise OSError("chmod failed")

        monkeypatch.setattr(Path, "chmod", fail_chmod)
    elif failure == "inventory":

        def fail_inventory(*args, **kwargs):
            raise OSError("inventory failed")

        monkeypatch.setattr(Path, "rglob", fail_inventory)
    elif failure == "commit":

        def fail_commit(*args, **kwargs):
            raise OSError("commit failed")

        monkeypatch.setattr(probe.os, "rename", fail_commit)
    assert draft.result.passed is False
    if failure:
        with pytest.raises(OSError):
            probe._publish_case(draft)
        assert draft.result.passed is False
        assert not (tmp_path / "completion.json").exists()
    else:
        result = probe._publish_case(draft)
        assert result.passed is True
        assert json.loads(result.receipt.read_text())["authority"] == "completion.json"
        assert "passed" not in json.loads(result.receipt.read_text())
        assert json.loads((tmp_path / "completion.json").read_text())["passed"] is True


@pytest.mark.parametrize("failure", ["constructor", "enter", "exit", "pin_close"])
@pytest.mark.parametrize("expected_memory", [None, "WRONG"])
@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
def test_provider_case_guard_prevents_late_success(
    tmp_path, monkeypatch, failure, expected_memory
):
    probe = _provider_probe()
    request = probe.Request(
        tmp_path / "no-prime",
        tmp_path / "no-kernel",
        tmp_path / "synthetic-source",
        tmp_path,
        "selector",
        expected_memory,
        True,
    )
    allocations = []
    real_allocate = probe._RuntimeOwner.allocate

    def allocate(request, evidence):
        owner = real_allocate(request, evidence)
        allocations.append(owner)
        return owner

    monkeypatch.setattr(probe._RuntimeOwner, "allocate", allocate)

    def qualify(run):
        run.census = list
        cleanup = run.cleanup(
            probe._ExternalSettlement(run.runtime_owner.allocation_id, "no_fixture", ())
        )
        assert cleanup.status == "VERIFIED"
        return _synthetic_draft(probe, run.evidence)

    monkeypatch.setattr(probe._OwnedRun, "qualify", qualify)
    if failure == "constructor":

        def fail_constructor(run, request, profile, kind, owner):
            (owner.root / "partial").mkdir()
            raise RuntimeError("partial constructor")

        monkeypatch.setattr(probe._OwnedRun, "__init__", fail_constructor)
    elif failure == "enter":
        original_enter = probe._RuntimeOwner.__enter__

        def fail_enter(owner):
            owner.root.rename(tmp_path / "moved")
            return original_enter(owner)

        monkeypatch.setattr(probe._RuntimeOwner, "__enter__", fail_enter)
    elif failure == "exit":
        original_exit = probe._RuntimeOwner.__exit__

        def fail_exit(owner, *args):
            original_exit(owner, *args)
            raise OSError("late owner failure")

        monkeypatch.setattr(probe._RuntimeOwner, "__exit__", fail_exit)
    else:
        original_close = probe.os.close

        def fail_close(fd):
            original_close(fd)
            if allocations and fd == allocations[0].root_fd and allocations[0].closed:
                raise OSError("pin close failed")

        monkeypatch.setattr(probe.os, "close", fail_close)
    result = probe._run_case(request, probe.RUNNER, "selector", tmp_path)
    assert result.passed is False
    assert result.cleanup.status == "FAILED"
    assert allocations[0].closed is True
    if failure in ("exit", "pin_close"):
        assert allocations[0].removed is not None
        assert (result.receipt.parent / "runtime-removal.json").is_file()
    elif failure == "enter":
        assert (
            json.loads((result.receipt.parent / "runtime-enter-failed.json").read_text())[
                "retained"
            ]
            is True
        )
    assert json.loads((result.receipt.parent / "completion.json").read_text())["passed"] is False


@pytest.mark.parametrize(
    "failure",
    [
        "allocation",
        "watch_registration",
        "watch_close",
        "owner_receipt",
        "thread",
        "socket",
        "fixture_receipt",
        "log_symlink",
        "capture_writer",
    ],
)
@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
def test_provider_allocation_and_settlement_fail_closed(tmp_path, monkeypatch, failure):
    probe = _provider_probe()
    if failure in ("allocation", "watch_registration"):
        request = probe.Request(
            tmp_path / "no-prime",
            tmp_path / "no-kernel",
            tmp_path / "synthetic-source",
            tmp_path,
            "selector",
            cooperative_cleanup=True,
        )
        real_open = probe.os.open
        created = []
        real_mkdtemp = probe.tempfile.mkdtemp

        def allocate(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            if kwargs.get("prefix") == "pw-":
                created.append(Path(path))
            return path

        monkeypatch.setattr(probe.tempfile, "mkdtemp", allocate)
        if failure == "allocation":

            def bad_open(path, flags, *args, **kwargs):
                if created and str(path) == created[0].name:
                    raise OSError("pin admission denied")
                return real_open(path, flags, *args, **kwargs)

            monkeypatch.setattr(probe.os, "open", bad_open)
        else:

            def bad_watch():
                raise OSError("watch admission denied")

            monkeypatch.setattr(probe.select, "kqueue", bad_watch)
        result = probe._run_case(request, probe.RUNNER, "selector", tmp_path)
        assert result.passed is False
        assert created[0].is_dir()
        assert (
            json.loads((result.receipt.parent / "runtime-allocation-failed.json").read_text())[
                "retained"
            ]
            is True
        )
        return
    _, owner, run, before = _synthetic_owned_run(probe, tmp_path)
    settlement = probe._ExternalSettlement(owner.allocation_id, "no_fixture", ())
    if failure == "watch_close":
        queue = owner.queue

        class BadClose:
            def control(self, *args):
                return queue.control(*args)

            def close(self):
                queue.close()
                raise OSError("watch close denied")

        owner.queue = BadClose()
    elif failure == "owner_receipt":
        original_write = probe.write_json

        def bad_receipt(path, *args, **kwargs):
            if path.name == "runtime-owner.json":
                raise OSError("owner receipt denied")
            return original_write(path, *args, **kwargs)

        monkeypatch.setattr(probe, "write_json", bad_receipt)
    elif failure == "thread":
        run.server_thread = SimpleNamespace(join=lambda **kwargs: None, is_alive=lambda: True)
    elif failure == "socket":
        run.private_sockets = lambda: [str(run.runtime / "live-socket")]
        run.census_errors.add("unreadable owner")
    elif failure == "capture_writer":
        run.server_capture_error = "synthetic capture writer failure"
    elif failure == "fixture_receipt":
        settlement = probe._ExternalSettlement(
            owner.allocation_id, "settled_fixture", ("missing-receipt.json",)
        )
    else:
        (run.runtime / "data/logs/external").symlink_to(tmp_path / "compact-sibling")
    try:
        if failure == "log_symlink":
            with pytest.raises(RuntimeError, match="capture_unsafe"):
                run.cleanup(settlement)
        else:
            cleanup = run.cleanup(settlement)
            assert cleanup.status == (
                "VERIFIED" if failure in ("watch_close", "owner_receipt") else "FAILED"
            )
        if failure in ("watch_close", "owner_receipt"):
            assert owner.removed is not None
            with pytest.raises((RuntimeError, OSError)):
                owner.__exit__(None, None, None)
            assert not (run.evidence / "completion.json").exists()
        else:
            assert run.runtime.is_dir()
    finally:
        if not owner.closed:
            with contextlib.suppress(RuntimeError, OSError):
                owner.__exit__(None, None, None)
        _assert_synthetic_sentinels(before)


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
def test_provider_known_compact_retirement_remains_independent(tmp_path, monkeypatch):
    from omnigent.harnesses.prime_native import bridge

    probe = _provider_probe()
    _, owner, run, before = _synthetic_owned_run(probe, tmp_path)
    monkeypatch.setattr(bridge, "_COMPACT_ROOT", tmp_path / "compact-parent")
    compact = bridge._COMPACT_ROOT / "owned"
    paths = bridge.PrimeRuntimePaths(compact)
    paths.prepare()
    target = paths.agent_dir / "auth.json"
    target.write_text("SYNTHETIC COMPACT COPY")
    sibling = bridge._COMPACT_ROOT / "sibling"
    sibling.write_text("SYNTHETIC SIBLING")
    before[sibling] = (sibling.stat().st_dev, sibling.stat().st_ino, sibling.read_bytes())
    run.register_owned_credential(target)
    run.bridge_roots.add(compact)
    run.session_id = "synthetic-session"

    def delete(method, route, **kwargs):
        assert method == "DELETE"
        assert route == "/v1/sessions/synthetic-session"
        for directory in reversed(paths._directories):
            directory.rmdir()
        return httpx.Response(200, request=httpx.Request("DELETE", "http://synthetic/session"))

    run.request_http = delete
    retirement = {"steps": [], "errors": []}
    run._delete_owned_session(retirement)
    assert retirement["errors"] == []
    assert retirement["retired_owned_trees"][0]["event_flags"] & probe.select.KQ_NOTE_DELETE
    with owner:
        owner.finish(
            probe._ExternalSettlement(owner.allocation_id, "no_fixture", ()),
            run._capture_owned_logs(),
        )
        for _ in range(2):
            errors = []
            run.finalize_owned_credentials(errors)
            assert errors == []
        assert not compact.exists()
    _assert_synthetic_sentinels(before)


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
def test_provider_fixture_settlement_is_required_and_bound_to_allocation(tmp_path):
    probe = _provider_probe()
    _, owner, run, before = _synthetic_owned_run(probe, tmp_path)
    with owner:
        with pytest.raises(TypeError):
            run.cleanup()
        assert run.runtime.is_dir()
        probe.write_json(run.evidence / "fixture.json", {"closed": True, "synthetic": True})
        result = run.cleanup(
            probe._ExternalSettlement(owner.allocation_id, "settled_fixture", ("fixture.json",))
        )
        assert result.status == "VERIFIED"
        assert not run.runtime.exists()
    _assert_synthetic_sentinels(before)


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
def test_provider_credential_recreation_after_removal_is_read_only_and_sticky(tmp_path):
    probe = _provider_probe()
    _, owner, run, before = _synthetic_owned_run(probe, tmp_path)
    settlement = probe._ExternalSettlement(owner.allocation_id, "no_fixture", ())
    assert run.cleanup(settlement).status == "VERIFIED"
    (run.runtime / "prime").mkdir(parents=True)
    replacement = run.runtime / "prime/auth.json"
    replacement.write_text("SYNTHETIC RECREATED COPY")
    before[replacement] = (
        replacement.stat().st_dev,
        replacement.stat().st_ino,
        replacement.read_bytes(),
    )
    try:
        for _ in range(2):
            assert run.cleanup(settlement).status == "FAILED"
            _assert_synthetic_sentinels(before)
    finally:
        with pytest.raises(RuntimeError, match="runtime_root_recreated"):
            owner.__exit__(None, None, None)
    _assert_synthetic_sentinels(before)


@pytest.mark.parametrize("substitution", ["parent", "descendant", "before_rmdir", "closed_watch"])
@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
def test_provider_substitution_before_enforced_check_preserves_external_tree(
    tmp_path, monkeypatch, substitution
):
    probe = _provider_probe()
    allocation_parent = tmp_path / "allocation-parent"
    allocation_parent.mkdir(mode=0o700)
    original_mkdtemp = probe.tempfile.mkdtemp

    def allocate(*args, **kwargs):
        if kwargs.get("prefix") == "pw-":
            kwargs["dir"] = allocation_parent
        return original_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(probe.tempfile, "mkdtemp", allocate)
    _, owner, run, before = _synthetic_owned_run(probe, tmp_path)
    owned_file = run.runtime / "workspace/owned.txt"
    owned_file.write_text("ORIGINAL")

    def preserve(path):
        path.write_text("EXTERNAL REPLACEMENT")
        before[path] = (path.stat().st_dev, path.stat().st_ino, path.read_bytes())

    if substitution == "parent":
        allocation_parent.rename(tmp_path / "original-parent")
        allocation_parent.mkdir()
        preserve(allocation_parent / "external")
    elif substitution == "descendant":
        check = owner._check_directory

        def substitute(fd, path):
            if path == run.workspace and not (tmp_path / "moved-workspace").exists():
                path.rename(tmp_path / "moved-workspace")
                path.mkdir()
                preserve(path / "external")
            return check(fd, path)

        monkeypatch.setattr(owner, "_check_directory", substitute)
    elif substitution == "before_rmdir":
        remove_contents = owner._remove_contents

        def substitute(fd, path):
            remove_contents(fd, path)
            if path == owner.root:
                path.rename(tmp_path / "moved-root")
                path.mkdir()
                preserve(path / "external")

        monkeypatch.setattr(owner, "_remove_contents", substitute)
    else:
        owner.queue.close()
    try:
        with contextlib.suppress(RuntimeError, ValueError, OSError):
            result = run.cleanup(probe._ExternalSettlement(owner.allocation_id, "no_fixture", ()))
            assert result.status == "FAILED"
        assert owner.removed is None
        assert not (run.evidence / "completion.json").exists()
    finally:
        with contextlib.suppress(RuntimeError, ValueError):
            owner.__exit__(None, None, None)
        _assert_synthetic_sentinels(before)


def test_provider_completion_rejects_changed_manifest(tmp_path):
    probe = _provider_probe()
    result = probe._publish_case(_synthetic_draft(probe, tmp_path))
    assert result.passed is True
    manifest = tmp_path / "manifest.json"
    manifest.chmod(0o600)
    manifest.write_text('{"qualified": true}\n')
    assert result.passed is False


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
def test_provider_partial_subclass_retains_allocation_without_fabricated_settlement(tmp_path):
    probe = _provider_probe()
    source = tmp_path / "synthetic-source"
    source.write_text("SYNTHETIC SOURCE")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    request = probe.Request(
        tmp_path / "no-prime",
        tmp_path / "no-kernel",
        source,
        tmp_path,
        "selector",
        cooperative_cleanup=True,
    )
    owner = probe._RuntimeOwner.allocate(request, evidence)

    class Partial(probe._OwnedRun):
        def __init__(self):
            super().__init__(request, probe.RUNNER, "selector", owner)
            raise RuntimeError("subclass setup failed")

    with pytest.raises(RuntimeError, match="runtime_retained_unsettled"):
        with owner:
            Partial()
    assert owner.root.is_dir()
    assert owner.closed is True
    assert owner.removed is None
    assert not (evidence / "runtime-settlement.json").exists()
    assert not (evidence / "completion.json").exists()
    assert source.read_text() == "SYNTHETIC SOURCE"


@pytest.mark.parametrize("poll_pass", [1, 2])
@pytest.mark.parametrize(
    "reason,error,expected_reason,expected_class,truncated",
    [
        ("error", "401 unauthorized SYNTHETIC_SECRET", "error", "authentication", False),
        ("aborted", None, "aborted", "absent", False),
        ("stop", "rate limit SYNTHETIC_SECRET", "stop", "rate_limit", False),
        (None, None, "missing", "absent", False),
        (
            {"bad": "SYNTHETIC_SECRET"},
            {"bad": "SYNTHETIC_SECRET"},
            "malformed",
            "malformed",
            False,
        ),
        ("SYNTHETIC_SECRET", "https://SYNTHETIC_SECRET", "unknown", "unknown", False),
        ("error", "Authorization: Bearer SYNTHETIC_SECRET", "error", "unknown", False),
        ("error", "SYNTHETIC_SECRET" * 1000, "error", "unknown", True),
        ("error", [], "error", "malformed", False),
        ("error", {}, "error", "malformed", False),
        ("error", False, "error", "malformed", False),
        ("error", 0, "error", "malformed", False),
        ("stop", [], "stop", "malformed", False),
        ("stop", {}, "stop", "malformed", False),
        ("stop", False, "stop", "malformed", False),
        ("stop", 0, "stop", "malformed", False),
        ("stop", " \t\n", "stop", "unknown", False),
    ],
    ids=[
        "error",
        "aborted",
        "stop-error",
        "missing",
        "malformed",
        "unknown",
        "header",
        "overlong",
        "empty-list",
        "empty-dict",
        "false",
        "zero",
        "stop-empty-list",
        "stop-empty-dict",
        "stop-false",
        "stop-zero",
        "stop-whitespace",
    ],
)
def test_native_failure_observation_survives_completed_reply(
    tmp_path, poll_pass, reason, error, expected_reason, expected_class, truncated
):
    probe = _provider_probe()
    operation, entries, items = _provider_reply(probe)
    entries[-1]["message"].update(stopReason=reason, errorMessage=error)
    run = _provider_capture_run(probe, operation, entries, tmp_path)
    pages = iter([entries[:2], entries] if poll_pass == 2 else [entries])
    run.journal = lambda: next(pages)
    run.items = lambda **kwargs: items
    with pytest.raises(RuntimeError, match=r"^native_assistant_not_successful$") as caught:
        run.completed_reply(operation, "kernel-seed")
    receipt_path = tmp_path / "kernel-seed-native-failure.json"
    assert receipt_path.is_file(), "current native failure observation was discarded"
    encoded = receipt_path.read_text()
    observation = json.loads(encoded)
    assert len(encoded.encode()) <= 4096
    assert "SYNTHETIC_SECRET" not in encoded + str(caught.value)
    assert observation["stop_reason"] == expected_reason
    assert observation["error_class"] == expected_class
    assert observation["error_present"] is (expected_class != "absent")
    assert observation["classification_input_truncated"] is truncated
    assert observation["native_user_id"] == "native-user"
    assert observation["native_reply_id"] == "native-final"
    assert observation["baseline_count"] == 1
    assert observation["native_user_position"] == 1
    assert observation["native_final_position"] == 2
    assert observation["session_matches_operation"] is True
    assert observation["operation"] == "kernel-seed"
    assert observation["public_baseline_count"] == 0
    assert observation["session_id"] == "conversation"
    assert observation["native_session_id"] == "session"
    assert observation["reason"] == "native_assistant_not_successful"
    assert observation["ids_omitted"] is False
    assert set(observation) == {
        "version",
        "session_id",
        "native_session_id",
        "session_matches_operation",
        "baseline_count",
        "public_baseline_count",
        "native_user_position",
        "native_final_position",
        "native_user_id",
        "native_reply_id",
        "ids_omitted",
        "stop_reason",
        "error_present",
        "error_class",
        "classification_input_truncated",
        "diagnostic",
        "operation",
        "reason",
    }
    predicates = json.loads((tmp_path / "kernel-seed-native-predicates.json").read_text())
    assert predicates["observation_state"] == "previous_poll"
    draft = _synthetic_draft(probe, tmp_path)
    failed = dataclasses.replace(
        draft.result,
        observations=(
            probe.Observation("synthetic", "FAILED", (receipt_path.name,), str(caught.value)),
        ),
    )
    result = probe._publish_case(dataclasses.replace(draft, result=failed))
    assert result.passed is False
    assert json.loads((tmp_path / "completion.json").read_text())["passed"] is False


@pytest.mark.parametrize("poll_pass", [1, 2])
@pytest.mark.parametrize("error_state", ["missing", "null", "empty"])
@pytest.mark.parametrize("literal", ["REPLY", "WRONG"])
def test_native_absent_error_failure_and_success_controls(
    tmp_path, poll_pass, error_state, literal
):
    probe = _provider_probe()
    for reason in ("error", "stop"):
        evidence = tmp_path / reason
        evidence.mkdir()
        operation, entries, items = _provider_reply(probe)
        entries[-1]["message"]["stopReason"] = reason
        entries[-1]["message"]["content"][0]["text"] = literal
        if error_state != "missing":
            entries[-1]["message"]["errorMessage"] = None if error_state == "null" else ""
        run = _provider_capture_run(probe, operation, entries, evidence)
        pages = iter([entries[:2], entries] if poll_pass == 2 else [entries, entries])
        run.journal = lambda pages=pages: next(pages)
        run.items = lambda items=items, **kwargs: items
        receipt = evidence / "kernel-seed-native-failure.json"
        if reason == "error":
            with pytest.raises(RuntimeError, match=r"^native_assistant_not_successful$"):
                run.completed_reply(operation, "kernel-seed")
            observation = json.loads(receipt.read_text())
            assert observation["error_class"] == "absent"
            assert observation["error_present"] is False
            assert observation["native_reply_id"] == "native-final"
            assert observation["native_final_position"] == 2
        elif literal == "WRONG":
            with pytest.raises(RuntimeError, match=r"^native_assistant_literal_mismatch$"):
                run.completed_reply(operation, "kernel-seed")
            assert not receipt.exists()
            assert not (evidence / "completion.json").exists()
        else:
            result = run.completed_reply(operation, "kernel-seed")
            assert result["complete"] is True
            assert result["native_reply_id"] == "native-final"
            assert result["public_reply_id"] == "public-final"
            assert not receipt.exists()
            published = probe._publish_case(_synthetic_draft(probe, evidence))
            assert published.passed is True
            assert json.loads((evidence / "completion.json").read_text())["passed"] is True


@pytest.mark.parametrize("reason", ["stop", "error"])
@pytest.mark.parametrize("container", [list, dict])
def test_native_final_rejects_hostile_error_containers(reason, container):
    class Hostile(container):
        def __bool__(self):
            raise AssertionError("error presence must not invoke truthiness")

        def __eq__(self, other):
            raise AssertionError("error presence must not compare containers")

        def __str__(self):
            raise AssertionError("error presence must not export container text")

    probe = _provider_probe()
    operation, entries, items = _provider_reply(probe)
    entries[-1]["message"].update(stopReason=reason, errorMessage=Hostile())
    with pytest.raises(probe._NativeReplyFailure) as caught:
        probe._reply_verdict(operation, entries, items)
    assert str(caught.value) == "native_assistant_not_successful"
    assert caught.value.observation.error_present is True
    assert caught.value.observation.error_class == "malformed"
    assert caught.value.observation.stop_reason == reason


def _produce_cli_log(run):
    import os
    import subprocess

    produced = subprocess.run(
        [
            sys.executable,
            "-c",
            "from omnigent.cli_diagnostics import setup_cli_logging; import logging; "
            "setup_cli_logging(['synthetic-owner-test']); "
            "logging.getLogger('omnigent').warning('SYNTHETIC_CLI_LOG'); logging.shutdown()",
        ],
        env={**os.environ, "OMNIGENT_DATA_DIR": str(run.runtime / "data")},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert produced.returncode == 0, produced.stderr
    alias = run.runtime / "data/logs/cli/latest-cli.log"
    assert alias.is_symlink()
    return alias


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
def test_real_cli_alias_capture_and_removal(tmp_path):
    probe = _provider_probe()
    _, owner, run, before = _synthetic_owned_run(probe, tmp_path)
    _produce_cli_log(run)
    try:
        with owner:
            cleanup = run.cleanup(probe._ExternalSettlement(owner.allocation_id, "no_fixture", ()))
            assert cleanup.status == "VERIFIED"
        alias_receipt = json.loads((run.evidence / "runtime-cli-alias-capture.json").read_text())
        assert alias_receipt["action"] == "skip_payload"
        assert alias_receipt["target_followed"] is False
        assert alias_receipt["allocation_id"] == owner.allocation_id
        assert not (run.evidence / "owned-logs/cli/latest-cli.log").exists()
        canonical = list((run.evidence / "owned-logs/cli").glob("cli-*.log"))
        assert len(canonical) == 1
        assert "SYNTHETIC_CLI_LOG" in canonical[0].read_text()
        assert owner.removed.event_flags & probe.select.KQ_NOTE_DELETE
        assert owner.closed and owner.errors == []
        assert not run.runtime.exists()
        probe._publish_case(_synthetic_draft(probe, run.evidence))
        assert json.loads((run.evidence / "completion.json").read_text())["passed"] is True
        for receipt in (
            "runtime-settlement.json",
            "runtime-removal.json",
            "runtime-owner.json",
            "runtime-cli-alias-remove.json",
        ):
            assert (run.evidence / receipt).is_file()
    finally:
        if not owner.closed:
            with contextlib.suppress(RuntimeError):
                owner.__exit__(None, None, None)
        _assert_synthetic_sentinels(before)


@contextlib.contextmanager
def _deny_alias_target_access(probe, owner, alias, sentinel, monkeypatch):
    import builtins
    import io
    import os

    accesses = []
    operations = []
    descriptors = {owner.root_fd: owner.root, owner.parent_fd: owner.root.parent}

    def checked(operation, path, dir_fd=None):
        if isinstance(path, int):
            candidate = descriptors.get(path)
        else:
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = descriptors.get(dir_fd, Path.cwd()) / candidate
            candidate = Path(os.path.abspath(candidate))
        if candidate is not None:
            operations.append((operation, str(candidate)))
            if (
                candidate == sentinel
                or candidate.is_relative_to(sentinel)
                or (operation == "open" and candidate == alias)
            ):
                accesses.append((operation, str(candidate)))
                raise AssertionError("subject attempted outside or alias payload access")
        return candidate

    real_open, real_unlink, real_rmdir = os.open, os.unlink, os.rmdir
    real_builtin, real_io, real_readlink = builtins.open, io.open, os.readlink

    def open_fd(path, flags, *args, **kwargs):
        candidate = checked("open", path, kwargs.get("dir_fd"))
        fd = real_open(path, flags, *args, **kwargs)
        descriptors[fd] = candidate
        return fd

    def open_file(real):
        def opened(path, *args, **kwargs):
            checked("open", path)
            return real(path, *args, **kwargs)

        return opened

    def unlink(path, *args, **kwargs):
        checked("unlink", path, kwargs.get("dir_fd"))
        return real_unlink(path, *args, **kwargs)

    def rmdir(path, *args, **kwargs):
        checked("rmdir", path, kwargs.get("dir_fd"))
        return real_rmdir(path, *args, **kwargs)

    def readlink(path, *args, **kwargs):
        if Path(path) == alias or str(path) == alias.name:
            raise AssertionError("subject read alias target value")
        return real_readlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", open_fd)
        patch.setattr(builtins, "open", open_file(real_builtin))
        patch.setattr(io, "open", open_file(real_io))
        patch.setattr(os, "unlink", unlink)
        patch.setattr(os, "rmdir", rmdir)
        patch.setattr(os, "readlink", readlink)
        yield operations
    probe.write_json(
        owner.evidence / "alias-access-trace.json",
        {
            "operations": operations,
            "outside_accesses": accesses,
            "oracle_reads_outside_subject": True,
        },
    )
    assert accesses == []


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
@pytest.mark.parametrize("target", ["absolute", "relative", "dangling", "loop"])
def test_cli_alias_never_accesses_its_target(tmp_path, monkeypatch, target):
    import os

    probe = _provider_probe()
    _, owner, run, before = _synthetic_owned_run(probe, tmp_path)
    alias = _produce_cli_log(run)
    sentinel = tmp_path / "compact-sibling"
    alias.unlink()
    alias.symlink_to(
        {
            "absolute": str(sentinel),
            "relative": os.path.relpath(sentinel, alias.parent),
            "dangling": str(sentinel / "absent"),
            "loop": alias.name,
        }[target]
    )
    with _deny_alias_target_access(probe, owner, alias, sentinel, monkeypatch) as operations:
        with owner:
            cleanup = run.cleanup(probe._ExternalSettlement(owner.allocation_id, "no_fixture", ()))
            assert cleanup.status == "VERIFIED"
    assert ("unlink", str(alias)) in operations
    assert ("rmdir", str(run.runtime)) in operations
    assert owner.removed is not None and owner.closed and owner.errors == []
    _assert_synthetic_sentinels(before)


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
@pytest.mark.parametrize(
    "failure",
    [
        "replacement",
        "missing",
        "unknown",
        "hardlink",
        "ancestor",
        "ancestor_alias",
        "wrong_allocation",
        "receipt",
        "capture_close",
        "budget",
        "fifo",
    ],
)
def test_cli_alias_rejections_are_sticky(tmp_path, monkeypatch, failure):
    import os

    probe = _provider_probe()
    _, owner, run, before = _synthetic_owned_run(probe, tmp_path)
    alias = _produce_cli_log(run)
    sentinel = tmp_path / "compact-sibling"
    settlement = probe._ExternalSettlement(owner.allocation_id, "no_fixture", ())
    captures = ()
    if failure in {"replacement", "missing", "ancestor", "ancestor_alias", "wrong_allocation"}:
        captures = run._capture_owned_logs()
    if failure == "replacement":
        alias.rename(alias.with_name("retired-alias"))
        alias.symlink_to(sentinel)
    elif failure == "missing":
        alias.unlink()
    elif failure == "unknown":
        alias.with_name("unknown").symlink_to(sentinel)
    elif failure == "hardlink":
        os.link(sentinel, alias.with_name("hardlink"))
        before[sentinel] = (sentinel.stat().st_dev, sentinel.stat().st_ino, sentinel.read_bytes())
    elif failure in {"ancestor", "ancestor_alias"}:
        alias.parent.rename(alias.parent.with_name("moved-cli"))
        if failure == "ancestor":
            alias.parent.mkdir()
            alias.symlink_to(sentinel)
        else:
            alias.parent.symlink_to(tmp_path)
    elif failure == "wrong_allocation":
        owner.diagnostics_alias = dataclasses.replace(
            owner.diagnostics_alias, allocation_id="0" * 32
        )
    elif failure == "receipt":
        write = probe.write_json

        def reject_receipt(path, *args, **kwargs):
            if path.name == "runtime-cli-alias-capture.json":
                raise OSError("synthetic receipt failure")
            return write(path, *args, **kwargs)

        monkeypatch.setattr(probe, "write_json", reject_receipt)
    elif failure == "capture_close":
        close = probe.os.close

        def reject_close(fd):
            metadata = os.fstat(fd)
            close(fd)
            if metadata.st_ino == (run.runtime / "data/logs/diagnostic.log").stat().st_ino:
                raise OSError("synthetic capture close failure")

        monkeypatch.setattr(probe.os, "close", reject_close)
    elif failure == "budget":
        with (alias.parent / "large.log").open("wb") as handle:
            handle.truncate(64 * 1024 * 1024 + 1)
    elif failure == "fifo":
        os.mkfifo(alias.with_name("fifo"))
    try:
        with _deny_alias_target_access(probe, owner, alias, sentinel, monkeypatch):
            with pytest.raises((RuntimeError, OSError)):
                if captures:
                    owner.finish(settlement, captures)
                else:
                    run._capture_owned_logs()
        assert owner.errors
        assert owner.removed is None
        assert run.cleanup(settlement).status == "FAILED"
        with pytest.raises(RuntimeError):
            owner.__exit__(None, None, None)
        draft = _synthetic_draft(probe, run.evidence)
        cleanup = probe.Observation("owned_cleanup", "FAILED", (), "sticky_failure")
        result = probe._publish_case(
            dataclasses.replace(draft, result=dataclasses.replace(draft.result, cleanup=cleanup))
        )
        assert result.passed is False
        assert json.loads((run.evidence / "completion.json").read_text())["passed"] is False
        receipt = (run.evidence / "runtime-traversal-rejection.json").read_text()
        assert len(receipt.encode()) <= 4096
        assert "target_value" not in receipt
    finally:
        if not owner.closed:
            with contextlib.suppress(RuntimeError, OSError):
                owner.__exit__(None, None, None)
        _assert_synthetic_sentinels(before)


@pytest.mark.parametrize("runner_stopped", [False, True])
@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
def test_provider_delete_routes_teardown_before_stopping_dedicated_runner(
    tmp_path, monkeypatch, runner_stopped
):
    import subprocess

    from omnigent.harnesses.prime_native import bridge

    probe = _provider_probe()
    _, owner, run, before = _synthetic_owned_run(probe, tmp_path)
    monkeypatch.setattr(bridge, "_COMPACT_ROOT", tmp_path / "compact-parent")
    compact = bridge._COMPACT_ROOT / "owned"
    paths = bridge.PrimeRuntimePaths(compact)
    paths.prepare()
    target = paths.agent_dir / "auth.json"
    target.write_text("SYNTHETIC COMPACT COPY")
    run.register_owned_credential(target)
    run.bridge_roots.add(compact)
    run.session_id = "synthetic-session"
    runner_script = r"""
import asyncio, importlib, json, sys
from pathlib import Path
from omnigent.harnesses.prime_native import bridge, process
from omnigent.runner.native.orchestration import _delete_native_bridge_dirs
root = Path(sys.argv[1])
for family in (
    "antigravity", "claude", "codex", "cursor", "goose", "hermes",
    "kimi", "kiro", "opencode", "pi", "qwen",
):
    module = importlib.import_module(f"omnigent.harnesses.{family}_native.bridge")
    for name in ("bridge_dir_for_bridge_id", "bridge_dir_for_session_id"):
        if hasattr(module, name):
            setattr(module, name, lambda *args, family=family: root.parent / ("unused-" + family))
bridge.runtime_paths = lambda session: bridge.PrimeRuntimePaths(root)
writer = (root / "writer.log").open("w")
def stop(paths):
    writer.close()
process.stop_prime_runtime = stop
print("READY", flush=True)
for command in sys.stdin:
    if command.strip() == "stop":
        stop(bridge.PrimeRuntimePaths(root))
        print("STOPPED_WRITER_CLOSED", flush=True)
        break
    if command.strip() == "delete":
        asyncio.run(_delete_native_bridge_dirs(server_client=None, session_id="synthetic-session"))
        print(json.dumps({
            "writer_closed": writer.closed, "root_absent": not root.exists(),
        }), flush=True)
"""
    runner = subprocess.Popen(
        [sys.executable, "-c", runner_script, str(compact)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    replies = []
    actions = []
    try:
        assert runner.stdout.readline().strip() == "READY"
        if runner_stopped:
            runner.stdin.write("stop\n")
            runner.stdin.flush()
            assert runner.stdout.readline().strip() == "STOPPED_WRITER_CLOSED"
            runner.wait(timeout=5)
        run.server = SimpleNamespace(poll=lambda: None)
        run.recover_owned_session = lambda: None
        run.owned_paths = lambda session: paths

        def request(method, route, *args, **kwargs):
            actions.append(method)
            if runner.poll() is None:
                runner.stdin.write("stop\n" if method == "POST" else "delete\n")
                runner.stdin.flush()
                replies.append(runner.stdout.readline().strip())
                if method == "POST":
                    runner.wait(timeout=5)
            run.server = None
            return httpx.Response(200, request=httpx.Request(method, "http://synthetic/session"))

        run.request_http = request

        def stop_before_qualification():
            raise RuntimeError("synthetic_observation_only")

        run._capture_owned_logs = stop_before_qualification
        with pytest.raises(RuntimeError, match="synthetic_observation_only"):
            run._cleanup_live(probe._ExternalSettlement(owner.allocation_id, "no_fixture", ()))
        records = json.loads((run.evidence / "retirement-attempts.json").read_text())
        assert records[0]["http_status"] == 200
        if runner_stopped:
            assert compact not in run._retired_trees
            assert compact.is_dir()
            assert records[0]["branch"] == "retained_original"
            assert records[0]["name_state"] == "original"
        else:
            assert compact in run._retired_trees, (actions, replies, compact.exists())
            assert not compact.exists()
            assert json.loads(replies[0]) == {"writer_closed": True, "root_absent": True}
            assert run._retired_trees[compact].event_flags & probe.select.KQ_NOTE_DELETE
            assert records[0]["branch"] == "retired"
        assert not (run.evidence / "runtime-settlement.json").exists()
        assert owner.removed is None
        assert actions == ["DELETE"]
    finally:
        runner.stdin.close()
        runner.wait(timeout=5)
        runner.stdout.close()
        runner.stderr.close()
        with contextlib.suppress(RuntimeError):
            owner.__exit__(None, None, None)
        _assert_synthetic_sentinels(before)


def _access_line(method, session_id, suffix=""):
    return (
        "INFO  10-10 14:39:08.709 uvicorn.access                   send               | "
        f'127.0.0.1:62985 - "{method} /v1/sessions/{session_id}{suffix} HTTP/1.1" 200 OK '
        f"3.0ms sid={session_id}\n"
    )


@pytest.mark.parametrize(
    "variant",
    [
        "accepted",
        "no_status",
        "status_before_delete",
        "other_session",
        "replaced",
        "wrong_epoch",
        "missing_epoch",
    ],
)
@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
def test_provider_cleanup_accepts_only_server_confirmed_maintenance_removal(
    tmp_path, monkeypatch, variant
):
    import shutil

    from omnigent.harnesses.prime_native import bridge

    probe = _provider_probe()
    _, owner, run, before = _synthetic_owned_run(probe, tmp_path)
    monkeypatch.setattr(bridge, "_COMPACT_ROOT", tmp_path / "compact-parent")
    compact = bridge._COMPACT_ROOT / "owned"
    paths = bridge.PrimeRuntimePaths(compact)
    paths.prepare()
    target = paths.agent_dir / "auth.json"
    target.write_text("SYNTHETIC COMPACT COPY")
    run.register_owned_credential(target)
    run.bridge_roots.add(compact)
    run.session_id = "synthetic-session"
    run.server = SimpleNamespace(poll=lambda: None)
    run.recover_owned_session = lambda: None
    run.owned_paths = lambda session: (run.register_owned_credential(target), paths)[1]
    epoch = "wrong-epoch" if variant == "wrong_epoch" else "owned-epoch"
    (compact / "config.json").write_text(
        json.dumps(
            {
                "serverUrl": run.url,
                "nativeAdmission": {
                    "source_id": run.session_id,
                    **({} if variant == "missing_epoch" else {"epoch": epoch}),
                },
                "authHeaders": {"Authorization": "FORBIDDEN_AUTH_SENTINEL"},
            }
        )
    )
    confirmations = []

    async def status_confirmation(method, route, body, timeout, deadline):
        assert (method, route) == (
            "POST",
            "/v1/sessions/synthetic-session/native-admission/status",
        )
        assert not compact.exists()
        confirmations.append(body)
        return httpx.Response(200, json={"deleted": body == {"epoch": "owned-epoch"}})

    run._request_http = status_confirmation
    deleted = _access_line("DELETE", run.session_id)
    status = _access_line(
        "POST",
        "other-session" if variant == "other_session" else run.session_id,
        "/native-admission/status",
    )
    server_log = run.runtime / "data/logs/server/server-20261010-143750-143838.log"
    server_log.parent.mkdir()
    server_log.write_text(
        {"no_status": deleted, "status_before_delete": status + deleted}.get(
            variant, deleted + status
        )
    )

    def offline_runner_delete(method, route, *args, **kwargs):
        assert (method, route) == ("DELETE", "/v1/sessions/synthetic-session")
        run.server = None
        return httpx.Response(200, request=httpx.Request(method, "http://synthetic/session"))

    def census_during_host_maintenance():
        if run.server is None and compact.exists() and compact not in run._retired_trees:
            shutil.rmtree(compact)
            if variant == "replaced":
                paths.prepare()
        return []

    run.request_http = offline_runner_delete
    run.census = census_during_host_maintenance
    settlement = probe._ExternalSettlement(owner.allocation_id, "no_fixture", ())
    try:
        if variant == "accepted":
            result = run.cleanup(settlement)
            assert result.status == "VERIFIED", run.cleanup_errors
            receipt = run._retired_trees[compact]
            assert receipt.kind == "maintenance_retired"
            assert receipt.session_id == "synthetic-session"
            assert receipt.status_line == status.strip()
            recorded = json.loads((run.evidence / "maintenance-retirement.json").read_text())
            assert recorded["kind"] == "maintenance_retired"
            assert recorded["epoch_sha256"] == hashlib.sha256(b"owned-epoch").hexdigest()
            assert recorded["status_response"] == {"deleted": True}
            assert confirmations == [{"epoch": "owned-epoch"}]
            assert "owned-epoch" not in json.dumps(recorded)
            assert "FORBIDDEN_AUTH_SENTINEL" not in json.dumps(recorded)
            assert owner.removed is not None
        else:
            expected = "replaced" if variant == "replaced" else "missing"
            with pytest.raises(RuntimeError, match=f"owned_credential_directory_{expected}"):
                run.cleanup(settlement)
            assert compact not in run._retired_trees
            assert owner.removed is None
            if variant == "wrong_epoch":
                assert confirmations == [{"epoch": "wrong-epoch"}]
            else:
                assert confirmations == []
        attempts = json.loads((run.evidence / "retirement-attempts.json").read_text())
        assert [(item["branch"], item["http_status"]) for item in attempts] == [
            ("retained_original", 200)
        ]
    finally:
        if not owner.closed:
            with contextlib.suppress(RuntimeError, OSError):
                owner.__exit__(None, None, None)
        _assert_synthetic_sentinels(before)


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
def test_provider_cleanup_continues_with_malformed_admission_config(tmp_path, monkeypatch):
    import shutil

    from omnigent.harnesses.prime_native import bridge

    probe = _provider_probe()
    _, owner, run, before = _synthetic_owned_run(probe, tmp_path)
    monkeypatch.setattr(bridge, "_COMPACT_ROOT", tmp_path / "compact-parent")
    compact = bridge._COMPACT_ROOT / "owned"
    paths = bridge.PrimeRuntimePaths(compact)
    paths.prepare()
    target = paths.agent_dir / "auth.json"
    target.write_text("SYNTHETIC COMPACT COPY")
    (compact / "config.json").write_text('{"nativeAdmission":')
    run.register_owned_credential(target)
    run.bridge_roots.add(compact)
    run.session_id = "synthetic-session"
    run.server = SimpleNamespace(poll=lambda: None)
    run.recover_owned_session = lambda: None
    run.owned_paths = lambda session: (run.register_owned_credential(target), paths)[1]
    run._root_admissions[compact] = (run.session_id, "previous-epoch")

    def delete(method, route, **kwargs):
        assert (method, route) == ("DELETE", "/v1/sessions/synthetic-session")
        assert compact not in run._root_admissions
        shutil.rmtree(compact)
        run.server = None
        return httpx.Response(200, request=httpx.Request(method, "http://synthetic/session"))

    run.request_http = delete
    try:
        with owner:
            settlement = probe._ExternalSettlement(owner.allocation_id, "no_fixture", ())
            assert run.cleanup(settlement).status == "VERIFIED", run.cleanup_errors
            assert compact not in run._root_admissions
            assert run._retired_trees[compact].kind == "delete_event"
            assert not compact.exists()
            assert owner.removed is not None
    finally:
        if not owner.closed:
            with contextlib.suppress(RuntimeError, OSError):
                owner.__exit__(None, None, None)
        _assert_synthetic_sentinels(before)


class _LiveServer:
    pid = 0
    stdout = None

    def __init__(self):
        self.stopped = False

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self):
        self.stopped = True

    kill = terminate

    def wait(self, timeout=None):
        return 0


@pytest.mark.parametrize("variant", ["reclaimed", "still_present", "removed_by_delete"])
@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
def test_provider_restarts_owned_host_to_run_maintenance_for_a_retained_root(
    tmp_path, monkeypatch, variant
):
    import shutil
    import threading

    from omnigent.harnesses.prime_native import bridge

    probe = _provider_probe()
    _, owner, run, before = _synthetic_owned_run(probe, tmp_path)
    monkeypatch.setattr(bridge, "_COMPACT_ROOT", tmp_path / "compact-parent")
    monkeypatch.setattr(probe, "MAINTENANCE_RECLAIM_TIMEOUT_S", 5)
    compact = bridge._COMPACT_ROOT / "owned"
    paths = bridge.PrimeRuntimePaths(compact)
    paths.prepare()
    target = paths.agent_dir / "auth.json"
    target.write_text("SYNTHETIC COMPACT COPY")
    run.register_owned_credential(target)
    run.bridge_roots.add(compact)
    run.session_id = "synthetic-session"
    run.server = _LiveServer()
    (run.evidence / "server.log").write_text("synthetic server\n")
    run.host_identity = {"pid": os.getpid(), "started": 1.0}
    run.recover_owned_session = lambda: None
    run.owned_paths = lambda session: (run.register_owned_credential(target), paths)[1]
    (compact / "config.json").write_text(
        json.dumps(
            {
                "serverUrl": run.url,
                "nativeAdmission": {"source_id": run.session_id, "epoch": "owned-epoch"},
                "authHeaders": {"Authorization": "FORBIDDEN_AUTH_SENTINEL"},
            }
        )
    )
    server_log = run.runtime / "data/logs/server/server-20261010-190652-190824.log"
    server_log.parent.mkdir()
    server_log.write_text("")

    def delete(method, route, **kwargs):
        assert (method, route) == ("DELETE", "/v1/sessions/synthetic-session")
        if variant == "removed_by_delete":
            shutil.rmtree(compact)
        with server_log.open("a") as handle:
            handle.write(_access_line("DELETE", run.session_id))
        return httpx.Response(200, request=httpx.Request(method, "http://synthetic/session"))

    async def status_confirmation(method, route, body, timeout, deadline):
        assert route == "/v1/sessions/synthetic-session/native-admission/status"
        return httpx.Response(200, json={"deleted": body == {"epoch": "owned-epoch"}})

    restarts = []
    maintenance = []

    def restarted_host(name):
        restarts.append((name, compact.exists(), run.server.poll()))
        child = subprocess.Popen([sys.executable, "-c", ""])
        child.wait()
        run.foreground_host = child
        run.foreground_host_identity = probe.ProcessIdentity(child.pid, 0.0, ())
        run.host_log_handle = (run.evidence / f"{name}.log").open("w")
        if variant == "reclaimed":

            def startup_maintenance():
                time.sleep(1)
                with server_log.open("a") as handle:
                    handle.write(_access_line("POST", run.session_id, "/native-admission/status"))
                shutil.rmtree(compact)

            maintenance.append(threading.Thread(target=startup_maintenance))
            maintenance[0].start()

    run.request_http = delete
    run._request_http = status_confirmation
    run.start_host = restarted_host
    settlement = probe._ExternalSettlement(owner.allocation_id, "no_fixture", ())
    try:
        result = run.cleanup(settlement)
        cleanup = json.loads((run.evidence / "cleanup.json").read_text())
        assert "owned-epoch" not in json.dumps(cleanup)
        assert "FORBIDDEN_AUTH_SENTINEL" not in json.dumps(cleanup)
        if variant == "removed_by_delete":
            assert result.status == "VERIFIED", run.cleanup_errors
            assert restarts == []
            assert "maintenance_host_restart" not in cleanup
            assert run._retired_trees[compact].kind == "delete_event"
            return
        assert restarts == [("host-maintenance", True, None)]
        restart = cleanup["maintenance_host_restart"]
        assert restart["reason"] == "retained_root_after_delete"
        assert restart["roots"] == [str(compact)]
        assert restart["requested_at"] <= restart["host_ready_at"] <= restart["finished_at"]
        assert (run.evidence / "host-maintenance.log").is_file()
        attempts = json.loads((run.evidence / "retirement-attempts.json").read_text())
        assert [(item["branch"], item["http_status"]) for item in attempts] == [
            ("retained_original", 200)
        ]
        if variant == "reclaimed":
            assert result.status == "VERIFIED", run.cleanup_errors
            assert restart["roots_absent"] is True
            receipt = run._retired_trees[compact]
            assert receipt.kind == "maintenance_retired"
            assert receipt.status_response == {"deleted": True}
            assert owner.removed is not None
        else:
            assert result.status == "FAILED"
            assert restart["roots_absent"] is False
            assert compact.is_dir()
            assert f"compact_runtime_removal_unproved: {compact}" in run.cleanup_errors
            assert compact not in run._retired_trees
            assert owner.removed is None
            with contextlib.suppress(RuntimeError):
                owner.__exit__(None, None, None)
            assert "runtime_retained_unsettled" in owner.errors
    finally:
        for thread in maintenance:
            thread.join(timeout=5)
        if not owner.closed:
            with contextlib.suppress(RuntimeError, OSError):
                owner.__exit__(None, None, None)
        _assert_synthetic_sentinels(before)


_RESTARTED_HOST = r"""
import json, os, sys, time
from pathlib import Path
time.sleep(0.5)
target = sys.argv[sys.argv.index("--server") + 1]
registry = Path(os.environ["OMNIGENT_DATA_DIR"], "daemons", "owned-target.json")
registry.with_suffix(".tmp").write_text(json.dumps({
    "pid": os.getpid(), "target": target, "mode": "server", "server_url": target,
    "started_at": time.time(), "host_id": "restarted-host",
}))
registry.with_suffix(".tmp").replace(registry)
time.sleep(30)
"""


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
def test_provider_host_restart_waits_past_the_stopped_host_registry_record(tmp_path, monkeypatch):
    probe = _provider_probe()
    _, owner, run, before = _synthetic_owned_run(probe, tmp_path)
    host = tmp_path / "repo/.venv/bin/omnigent"
    host.parent.mkdir(parents=True)
    host.write_text(f"#!{sys.executable}\n{_RESTARTED_HOST}")
    host.chmod(0o700)
    monkeypatch.setattr(probe, "REPO", tmp_path / "repo")
    exited = subprocess.Popen([sys.executable, "-c", ""])
    exited.wait()
    stopped = {
        "pid": exited.pid,
        "target": run.url,
        "mode": "server",
        "server_url": run.url,
        "started_at": 1.0,
        "host_id": "stopped-host",
    }
    (run.runtime / "data/daemons").mkdir()
    (run.runtime / "data/daemons/owned-target.json").write_text(json.dumps(stopped))
    run.host_record = stopped

    def host_api(method, route, *args, **kwargs):
        assert (method, route) == ("GET", "/v1/hosts/restarted-host")
        return httpx.Response(
            200,
            json={"host_id": "restarted-host", "status": "online"},
            request=httpx.Request(method, "http://synthetic/host"),
        )

    run.request_http = host_api
    try:
        run.start_host("host-maintenance")
        assert run.host_record["host_id"] == "restarted-host"
        assert run.host_identity["pid"] == run.foreground_host.pid
        ready = json.loads((run.evidence / "host-maintenance-ready.json").read_text())
        assert ready["api"] == {"host_id": "restarted-host", "status": "online"}
    finally:
        if run.foreground_host:
            run.foreground_host.kill()
            run.foreground_host.wait(timeout=5)
        if run.host_log_handle:
            run.host_log_handle.close()
        with contextlib.suppress(RuntimeError, OSError):
            owner.__exit__(None, None, None)
        _assert_synthetic_sentinels(before)


_ATTACHED_CLIENT = r"""
import os, sys
print("Web UI: http://127.0.0.1/c/synthetic-session", flush=True)
sys.stdin.readline()
os.write(1, b"Resume with omnigent prime-native --resume synthetic-session\r\n" * 50)
"""


@pytest.mark.skipif(
    sys.platform != "darwin", reason="Darwin holds an exiting pty client until its output is read"
)
def test_attached_client_exits_while_delete_blocks_the_probe(tmp_path, monkeypatch):
    import select

    probe = _provider_probe()
    _, owner, run, before = _synthetic_owned_run(probe, tmp_path)
    spawn = probe.pexpect.spawn
    monkeypatch.setattr(
        probe.pexpect,
        "spawn",
        lambda command, argv, **kwargs: spawn(sys.executable, ["-c", _ATTACHED_CLIENT], **kwargs),
    )
    run.foreground_host_record = lambda: ({}, {})
    run.own_session = lambda session: setattr(run, "session_id", session)
    run.attach()
    queue = select.kqueue()
    try:
        queue.control(
            [
                select.kevent(
                    run.terminal.pid,
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD,
                    fflags=select.KQ_NOTE_EXIT,
                )
            ],
            0,
            0,
        )
        run.terminal.send("\r")
        assert queue.control([], 1, 5)
        exit_record = run.close_attachment()
        assert exit_record["exitstatus"] == 0
        transcript = (run.evidence / "terminal.txt").read_text()
        assert "Web UI: http://127.0.0.1/c/synthetic-session" in transcript
        assert transcript.count("Resume with omnigent prime-native") == 50
    finally:
        queue.close()
        if not run.terminal.closed:
            run.terminal.close(force=True)
        run.terminal = None
        run.session_id = None
        with owner:
            settlement = probe._ExternalSettlement(owner.allocation_id, "no_fixture", ())
            assert run.cleanup(settlement).status == "VERIFIED"
        _assert_synthetic_sentinels(before)


@pytest.mark.parametrize(
    "kind",
    [
        "refusal",
        "safety",
        "overloaded",
        "rate_limit",
        "server_error",
        "auth",
        "permission",
        "invalid_request",
        "malformed_response",
        "unknown",
    ],
)
def test_seed_diagnostic_projects_known_kind_without_payload(kind):
    probe = _provider_probe()
    value = probe._seed_diagnostic(
        [
            {
                "type": "provider_stream_failure",
                "details": {
                    "kind": kind,
                    "status": 401,
                    "raw": "FORBIDDEN_SENTINEL",
                    "requestId": "FORBIDDEN_SENTINEL",
                },
                "error": {
                    "code": "EACCES",
                    "message": "FORBIDDEN_SENTINEL",
                    "stack": "FORBIDDEN_SENTINEL",
                },
            }
        ],
        [],
        False,
        "error",
        True,
    )
    assert value["failure_kind"] == kind
    assert value["diagnostic_origin"] == "provider_stream_failure"
    assert value["phase"] == "provider_stream"
    assert value["http_status"] == "http_401"
    assert value["error_code"] == "EACCES"
    assert (
        value["kernel_ready"]
        == value["protocol_match"]
        == value["bootstrap_done"]
        == "unavailable"
    )
    assert value["projection_complete"] is False
    assert "FORBIDDEN_SENTINEL" not in json.dumps(value)


@pytest.mark.parametrize(
    "status,expected",
    [
        (400, "http_400"),
        (401, "http_401"),
        (403, "http_403"),
        (404, "http_404"),
        (408, "http_408"),
        (429, "http_429"),
        (500, "http_500"),
        (502, "http_502"),
        (503, "http_503"),
        (504, "http_504"),
        (529, "http_529"),
        (200, "other"),
        (None, "absent"),
        (True, "malformed"),
        ("FORBIDDEN_SENTINEL", "malformed"),
        ({}, "malformed"),
    ],
)
def test_seed_diagnostic_http_buckets(status, expected):
    value = _provider_probe()._seed_diagnostic(
        [{"type": "provider_stream_failure", "details": {"status": status}}],
        [],
        False,
        "error",
        True,
    )
    assert value["http_status"] == expected
    assert "FORBIDDEN_SENTINEL" not in json.dumps(value)


@pytest.mark.parametrize(
    "code,expected",
    [
        (item, item)
        for item in ("ENOENT", "EACCES", "EPERM", "ECONNREFUSED", "ECONNRESET", "ETIMEDOUT")
    ]
    + [
        (None, "absent"),
        (0, "other"),
        (True, "malformed"),
        ({}, "malformed"),
        pytest.param("FORBIDDEN_SENTINEL" * 1000, "other", id="overlong"),
    ],
)
def test_seed_diagnostic_code_buckets(code, expected):
    value = _provider_probe()._seed_diagnostic(
        [{"type": "agent_lifecycle_failure", "error": {"code": code}}], [], False, "error", True
    )
    assert value["error_code"] == expected
    assert value["phase"] == "agent_lifecycle"
    assert "FORBIDDEN_SENTINEL" not in json.dumps(value)


@pytest.mark.parametrize(
    "status,expected",
    [
        ("ok", "ok"),
        ("error", "error"),
        ("aborted", "aborted"),
        ("starting", "starting"),
        (None, "absent"),
        (True, "malformed"),
        pytest.param("FORBIDDEN_SENTINEL" * 1000, "malformed", id="overlong"),
    ],
)
@pytest.mark.parametrize(
    "is_error,expected_error",
    [
        (True, "true"),
        (False, "false"),
        (None, "absent"),
        (0, "malformed"),
        ("FORBIDDEN_SENTINEL", "malformed"),
    ],
)
def test_seed_diagnostic_tool_status_is_not_startup_proof(
    status, expected, is_error, expected_error
):
    value = _provider_probe()._seed_diagnostic(
        None,
        [
            {
                "details": {
                    "status": status,
                    "stdout": "FORBIDDEN_SENTINEL",
                    "bootstrap_done": "ok",
                },
                "isError": is_error,
                "content": "FORBIDDEN_SENTINEL",
            }
        ],
        True,
        "error",
        True,
    )
    assert value["ipython_done"] == expected
    assert value["ipython_is_error"] == expected_error
    assert value["ipython_call"] == "observed"
    assert value["phase"] == "unknown"
    assert value["phase_evidence"] == "tool_status_only"
    assert value["bootstrap_done"] == "unavailable"
    assert "FORBIDDEN_SENTINEL" not in json.dumps(value)


@pytest.mark.parametrize(
    "diagnostics,expected",
    [
        (None, "absent"),
        ([], "absent"),
        ({}, "malformed"),
        ([{}], "malformed"),
        ([{"type": []}], "malformed"),
        ([{"type": "FORBIDDEN_SENTINEL" * 1000}], "unknown"),
        ([{}] * 17, "malformed"),
        ([{"type": "agent_lifecycle_failure"}, {"type": "provider_stream_failure"}], "both"),
    ],
)
def test_seed_diagnostic_missing_malformed_and_ambiguous(diagnostics, expected):
    value = _provider_probe()._seed_diagnostic(diagnostics, [], False, "error", True)
    assert value["diagnostic_origin"] == expected
    assert value["phase"] == "unknown"
    assert value["projection_complete"] is False
    assert "FORBIDDEN_SENTINEL" not in json.dumps(value)


def test_seed_diagnostic_closed_schema_and_hostile_values():
    class Hostile:
        def __eq__(self, other):
            raise AssertionError("hostile equality")

        def __str__(self):
            raise AssertionError("hostile stringify")

        def __hash__(self):
            raise AssertionError("hostile hash")

    class HostileDict(dict):
        def get(self, *args):
            raise AssertionError("hostile getter")

    probe = _provider_probe()
    for diagnostics in ([HostileDict()], [{"type": Hostile()}], HostileDict()):
        value = probe._seed_diagnostic(diagnostics, [], False, "error", True)
        assert value["diagnostic_origin"] == "malformed"
    value = probe._seed_diagnostic(
        [
            {
                "type": "provider_stream_failure",
                "details": {"kind": Hostile(), "status": Hostile()},
                "error": {"code": Hostile()},
            }
        ],
        [],
        False,
        "error",
        True,
    )
    assert value["failure_kind"] == "unknown"
    assert value["http_status"] == value["error_code"] == "malformed"
    for field, allowed in probe._SEED_DIAGNOSTIC_ENUMS.items():
        for enum in allowed:
            assert probe._validate_seed_diagnostic({**value, field: enum})[field] == enum
        for malformed in (Hostile(), None, 0, "FORBIDDEN_SENTINEL" * 1000):
            with pytest.raises(ValueError, match="native_seed_diagnostic_enum"):
                probe._validate_seed_diagnostic({**value, field: malformed})
    with pytest.raises(ValueError, match="native_seed_diagnostic_schema"):
        probe._validate_seed_diagnostic({**value, "extra": "FORBIDDEN_SENTINEL"})
    del value["phase"]
    with pytest.raises(ValueError, match="native_seed_diagnostic_schema"):
        probe._validate_seed_diagnostic(value)


@pytest.mark.parametrize(
    "failure",
    [
        "retained",
        "missing_event",
        "rename",
        "replaced",
        "recreated",
        "invalid_watch",
        "credential_reappears",
        "prior_error",
        "api_unavailable",
        "http_failure",
    ],
)
@pytest.mark.skipif(sys.platform != "darwin", reason="Requires admitted Darwin vnode events")
def test_retirement_attempt_receipts_fail_closed(tmp_path, monkeypatch, failure):
    import shutil

    from omnigent.harnesses.prime_native import bridge

    probe = _provider_probe()
    _, owner, run, before = _synthetic_owned_run(probe, tmp_path)
    monkeypatch.setattr(bridge, "_COMPACT_ROOT", tmp_path / "compact-parent")
    compact = bridge._COMPACT_ROOT / "owned"
    paths = bridge.PrimeRuntimePaths(compact)
    paths.prepare()
    target = paths.agent_dir / "auth.json"
    target.write_text("SYNTHETIC COMPACT COPY")
    run.register_owned_credential(target)
    run.bridge_roots.add(compact)
    run.session_id = "synthetic-session"
    real_queue = probe.select.kqueue

    class Watch:
        def __init__(self):
            self.queue = real_queue()
            self.polls = 0

        def control(self, changes, maximum, timeout):
            events = self.queue.control(changes, maximum, timeout)
            if not changes:
                self.polls += 1
                if self.polls == 2 and failure == "missing_event":
                    return []
                if self.polls == 2 and failure == "invalid_watch":
                    event = events[0]
                    return [
                        SimpleNamespace(
                            ident=event.ident,
                            filter=event.filter,
                            flags=event.flags,
                            fflags=event.fflags,
                            data=1,
                        )
                    ]
            return events

        def close(self):
            self.queue.close()

    monkeypatch.setattr(probe.select, "kqueue", Watch)

    def delete(method, route, **kwargs):
        if failure in ("missing_event", "invalid_watch", "recreated"):
            shutil.rmtree(compact)
            if failure == "recreated":
                paths.prepare()
        elif failure in ("rename", "replaced"):
            compact.rename(compact.with_name("moved"))
            if failure == "replaced":
                paths.prepare()
        elif failure == "credential_reappears":
            target.write_text("FORBIDDEN_SENTINEL")
        return httpx.Response(
            503 if failure == "http_failure" else 200,
            request=httpx.Request(method, "http://synthetic/session"),
        )

    run.request_http = delete
    result = {
        "steps": [],
        "errors": ["remove credential copy: prior failure"] if failure == "prior_error" else [],
    }
    try:
        if failure == "retained":
            run._delete_owned_session(result)
        else:
            with pytest.raises((RuntimeError, httpx.HTTPStatusError)):
                run._delete_owned_session(result, unavailable=failure == "api_unavailable")
        receipt = json.loads((run.evidence / "retirement-attempts.json").read_text())
        assert receipt == json.loads(json.dumps(result["retirement_attempts"]))
        assert len(receipt) == 1
        record = receipt[0]
        assert record["allocation_id"] == owner.allocation_id
        assert record["session_id"] == "synthetic-session"
        assert record["schema"] == "native_retirement_attempt_v1"
        assert record["branch"] != "retired"
        assert compact not in run._retired_trees
        assert "FORBIDDEN_SENTINEL" not in json.dumps(receipt)
        if failure == "retained":
            assert record["branch"] == "retained_original"
            assert record["name_state"] == "original"
            assert record["held_root_matches"] is record["held_parent_matches"] is True
            assert record["http_status"] == 200
            assert record["event_flags"] == 0
        if failure == "missing_event":
            assert record["branch"] == "owned_tree_deletion_event_missing"
            assert record["name_state"] == "absent"
        if failure in ("api_unavailable", "prior_error"):
            assert record["delete_step"] is record["http_status"] is None
        assert owner.removed is None
    finally:
        with contextlib.suppress(RuntimeError):
            owner.__exit__(None, None, None)
        _assert_synthetic_sentinels(before)


@pytest.mark.parametrize("poll_pass", [1, 2])
@pytest.mark.parametrize("reason,error", [("error", None), ("stop", []), ("stop", False)])
def test_seed_diagnostic_is_bound_to_current_failure_and_typed_tool(
    tmp_path, poll_pass, reason, error
):
    probe = _provider_probe()
    operation, entries, items = _provider_reply(probe)
    entries[2:2] = [
        {
            "id": "call-entry",
            "type": "message",
            "parentId": "native-user",
            "message": {
                "role": "assistant",
                "stopReason": "toolUse",
                "content": [
                    {"type": "toolCall", "id": "call", "name": "ipython", "arguments": {}}
                ],
            },
        },
        {
            "id": "tool-entry",
            "type": "message",
            "parentId": "call-entry",
            "message": {
                "role": "toolResult",
                "toolCallId": "call",
                "toolName": "ipython",
                "content": [{"type": "text", "text": "FORBIDDEN_SENTINEL"}],
                "details": {"status": "error", "stderr": "FORBIDDEN_SENTINEL"},
                "isError": True,
            },
        },
    ]
    entries[-1]["parentId"] = "tool-entry"
    entries[-1]["message"].update(
        stopReason=reason,
        errorMessage=error,
        diagnostics=[
            {
                "type": "agent_lifecycle_failure",
                "error": {"code": "ENOENT", "message": "FORBIDDEN_SENTINEL"},
            }
        ],
    )
    run = _provider_capture_run(probe, operation, entries, tmp_path)
    pages = iter([entries[:2], entries] if poll_pass == 2 else [entries])
    run.journal = lambda: next(pages)
    run.items = lambda **kwargs: items
    with pytest.raises(RuntimeError, match="native_assistant_not_successful"):
        run.completed_reply(operation, "kernel-seed")
    text = (tmp_path / "kernel-seed-native-failure.json").read_text()
    receipt = json.loads(text)
    diagnostic = receipt["diagnostic"]
    assert diagnostic["operation"] == "kernel_seed"
    assert diagnostic["observation_scope"] == "same_operation_events"
    assert diagnostic["ipython_done"] == "error"
    assert diagnostic["ipython_is_error"] == "true"
    assert diagnostic["error_code"] == "ENOENT"
    assert diagnostic["phase"] == "agent_lifecycle"
    assert diagnostic["bootstrap_done"] == "unavailable"
    assert receipt["session_id"] == "conversation"
    assert receipt["native_final_position"] == 4
    assert receipt["stop_reason"] == reason
    assert receipt["error_present"] is (reason == "stop")
    assert receipt["error_class"] == ("malformed" if reason == "stop" else "absent")
    assert "FORBIDDEN_SENTINEL" not in text
    previous = json.loads((tmp_path / "kernel-seed-native-predicates.json").read_text())
    assert previous["observation_state"] == "previous_poll"
    assert "diagnostic" not in previous


def _mcp_capture(probe, phase="before"):
    invocation = probe.PhaseInvocation(phase, "call-nonce", "adapter-call")
    generation = probe.GenerationIdentity(
        1, "startup", probe.ProcessIdentity(70, 7.0, ("python",))
    )
    text = (
        probe.MCP_ERROR_PREFIX + "/owned/runner.log"
        if phase == "outage"
        else probe._mcp_result_text("run", generation, invocation)
    )
    prompt = "ADAPTER_MCP_PHASE " + json.dumps(
        {
            "invocation": dataclasses.asdict(invocation),
            "generation": dataclasses.asdict(generation),
        }
    )
    call = {
        "id": invocation.call_id,
        "name": probe.MCP_TOOL,
        "type": "toolCall",
        "arguments": invocation.arguments(),
    }
    native = (
        {"id": "user", "parentId": "baseline", "message": {"role": "user", "content": prompt}},
        {
            "id": "call",
            "parentId": "user",
            "message": {
                "role": "assistant",
                "api": "openai-completions",
                "provider": "verify",
                "content": [call],
            },
        },
        {
            "id": "result",
            "parentId": "call",
            "message": {
                "role": "toolResult",
                "toolCallId": invocation.call_id,
                "toolName": probe.MCP_TOOL,
                "isError": phase == "outage",
                "content": [{"type": "text", "text": text}],
            },
        },
        {
            "id": "final",
            "parentId": "result",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": invocation.acknowledgement(),
            },
        },
    )
    public = (
        {"type": "message", "role": "user", "status": "completed", "content": prompt},
        {
            "type": "function_call",
            "name": probe.MCP_TOOL,
            "call_id": invocation.call_id,
            "arguments": json.dumps(invocation.arguments()),
            "status": "completed",
            "response_id": "turn",
        },
        {
            "type": "function_call_output",
            "call_id": invocation.call_id,
            "output": text,
            "status": "completed",
            "response_id": "turn",
        },
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": invocation.acknowledgement()}],
        },
    )
    request = {
        "messages": [
            {"role": "user", "content": prompt},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": invocation.call_id,
                        "type": "function",
                        "function": {
                            "name": probe.MCP_TOOL,
                            "arguments": json.dumps(invocation.arguments()),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": invocation.call_id, "content": text},
        ]
    }
    ledger = (
        ()
        if phase == "outage"
        else (
            {
                "run_nonce": "run",
                "generation": json.loads(json.dumps(dataclasses.asdict(generation))),
                "arguments": invocation.arguments(),
                "sequence": 1,
                "result_text": text,
            },
        )
    )
    capture = probe._PhaseCapture(
        native,
        public,
        (request,),
        ledger,
        f"MCP tool dispatch failed for {probe.MCP_TOOL}" if phase == "outage" else "",
        _selected_root_identity(probe),
        probe.ToolAvailabilityObservation(phase, True, True),
        time.monotonic(),
    )
    return invocation, generation, capture


def test_mcp_success_and_outage_require_complete_correlated_records():
    probe = _adapter_probe()
    invocation, generation, capture = _mcp_capture(probe)
    result = probe._require_success("run", invocation, generation, capture)
    assert result.server_invocation.result_text == (
        '{"call_id": "adapter-call", "call_nonce": "call-nonce", "generation": 1, '
        '"phase": "before", "run_nonce": "run", "startup_nonce": "startup", '
        '"value": "ADAPTER_MCP_LITERAL_RESULT"}'
    )
    repeated = dataclasses.replace(capture, model_requests=capture.model_requests * 2)
    assert probe._require_success("run", invocation, generation, repeated) == result
    invocation, generation, capture = _mcp_capture(probe, "outage")
    outage = probe._require_outage("run", "http://loopback/mcp", generation, invocation, capture)
    assert outage.error_text == probe.MCP_ERROR_PREFIX + "/owned/runner.log"
    assert outage.availability.explicit_runtime_state == "NOTOBSERVED"


@pytest.mark.parametrize(
    "mutation",
    [
        "run",
        "generation",
        "startup",
        "id",
        "value",
        "whitespace",
        "duplicate_ledger",
        "public_missing",
        "public_id",
        "native_error",
        "native_parent",
        "conflicting_history",
        "stale_history",
        "advertisement",
    ],
)
def test_mcp_success_rejects_mutated_boundaries(mutation):
    probe = _adapter_probe()
    invocation, generation, capture = _mcp_capture(probe)
    if mutation == "run":
        with pytest.raises(RuntimeError):
            probe._require_success("wrong", invocation, generation, capture)
        return
    if mutation in {"generation", "startup"}:
        field = {"generation": "number", "startup": "startup_nonce"}[mutation]
        generation = dataclasses.replace(
            generation, **{field: 2 if field == "number" else "wrong"}
        )
    elif mutation in {"id", "value", "whitespace", "native_error", "native_parent"}:
        native = json.loads(json.dumps(capture.native_rows))
        if mutation == "id":
            native[2]["message"]["toolCallId"] = "wrong"
        elif mutation == "native_error":
            native[2]["message"]["isError"] = True
        elif mutation == "native_parent":
            native[3]["parentId"] = "baseline"
        else:
            native[2]["message"]["content"][0]["text"] += (
                " " if mutation == "whitespace" else "wrong"
            )
        capture = dataclasses.replace(capture, native_rows=tuple(native))
    elif mutation == "duplicate_ledger":
        capture = dataclasses.replace(capture, ledger_rows=capture.ledger_rows * 2)
    elif mutation == "public_missing":
        capture = dataclasses.replace(capture, public_items=(capture.public_items[0],))
    elif mutation == "public_id":
        public = json.loads(json.dumps(capture.public_items))
        public[2]["call_id"] = "wrong"
        capture = dataclasses.replace(capture, public_items=tuple(public))
    elif mutation in {"conflicting_history", "stale_history"}:
        request = json.loads(json.dumps(capture.model_requests[0]))
        if mutation == "conflicting_history":
            request["messages"][-1]["content"] = "wrong"
        else:
            request["messages"].append({"role": "user", "content": "unrelated"})
        capture = dataclasses.replace(capture, model_requests=(request,))
    else:
        capture = dataclasses.replace(
            capture, availability=probe.ToolAvailabilityObservation("before", False, True)
        )
    with pytest.raises(RuntimeError):
        probe._require_success("run", invocation, generation, capture)


@pytest.mark.parametrize("mutation", ["error_flag", "ledger", "runner", "public"])
def test_mcp_outage_rejects_incomplete_error(mutation):
    probe = _adapter_probe()
    invocation, generation, capture = _mcp_capture(probe, "outage")
    if mutation == "error_flag":
        rows = json.loads(json.dumps(capture.native_rows))
        rows[2]["message"]["isError"] = False
        capture = dataclasses.replace(capture, native_rows=tuple(rows))
    elif mutation == "ledger":
        capture = dataclasses.replace(capture, ledger_rows=({"call": "outage"},))
    elif mutation == "runner":
        capture = dataclasses.replace(capture, runner_delta="")
    else:
        capture = dataclasses.replace(capture, public_items=())
    with pytest.raises(RuntimeError):
        probe._require_outage("run", "endpoint", generation, invocation, capture)


@pytest.mark.parametrize(
    "field", ["kernel", "token", "count", "object_id", "socket_id", "socket_fileno"]
)
def test_mcp_memory_rejects_recreated_or_changed_state(field):
    probe = _adapter_probe()
    before = probe.KernelMemoryObservation(
        probe.ProcessIdentity(13, 3.0, ("python",)), "run", 42, 1, 2, 3
    )
    after = dataclasses.replace(before, count=43)
    probe._require_memory(before, after, 43)
    value = (
        probe.ProcessIdentity(14, 4.0, ("python",))
        if field == "kernel"
        else "wrong"
        if field == "token"
        else 99
    )
    with pytest.raises(RuntimeError, match="continuity"):
        probe._require_memory(before, dataclasses.replace(after, **{field: value}), 43)


def _fixture_env(probe):
    return {"PYTHONPATH": str(probe.REPO)}


def test_mcp_fixture_uses_only_explicit_child_configuration(tmp_path, monkeypatch):
    probe = _adapter_probe()
    monkeypatch.setattr(probe.os, "environ", {"OMNIGENT_TEST_AMBIENT": "must-not-reach-child"})
    original = probe.subprocess.Popen
    launches = []

    def launch(argv, **kwargs):
        launches.append((argv, kwargs["env"]))
        return original(argv, **kwargs)

    monkeypatch.setattr(probe.subprocess, "Popen", launch)
    child = probe.subprocess.run(
        [
            probe.sys.executable,
            "-c",
            "import json, os; print(json.dumps([os.environ.get('OMNIGENT_TEST_AMBIENT', "
            "'absent'), os.environ['PYTHONPATH']]))",
        ],
        env=_fixture_env(probe),
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(child.stdout) == ["absent", str(probe.REPO)]
    launches.clear()
    fixture = probe.McpFixture(probe.Evidence(tmp_path), _fixture_env(probe), "run")
    try:
        first = fixture.start_first()
        assert first.number == 1
        assert not fixture.refused()
        assert launches[0][0][0] == probe.sys.executable
        assert launches[0][1] == {"PYTHONPATH": str(probe.REPO)}
    finally:
        cleanup = fixture.close()
    assert cleanup.outputs_closed and cleanup.endpoint_refused and cleanup.errors == ()


def _fixture_outage(probe, fixture, first):
    invocation = probe.PhaseInvocation("outage", "outage-nonce", "outage-id")
    return probe.CompletedOutage(
        fixture.run_nonce,
        fixture.url,
        first,
        invocation,
        probe.MCP_ERROR_PREFIX + "/owned/runner.log",
        "/owned/runner.log",
        probe.ToolAvailabilityObservation("outage", True, True),
        _selected_root_identity(probe),
        time.monotonic(),
    )


def test_mcp_loopback_stop_replacement_and_repeated_settlement(tmp_path):
    probe = _adapter_probe()
    fixture = probe.McpFixture(probe.Evidence(tmp_path), _fixture_env(probe), "run")
    try:
        first = fixture.start_first()
        assert fixture.generations[0].started.identity == first
        assert not fixture.refused()
        with pytest.raises(RuntimeError, match="completed outage"):
            fixture.restart(_fixture_outage(probe, fixture, first))
        fixture.stop_first()
        assert fixture.refused()
        outage = _fixture_outage(probe, fixture, first)
        with pytest.raises(RuntimeError, match="completed outage"):
            fixture.restart(dataclasses.replace(outage, run_nonce="other"))
        second = fixture.restart(outage)
        assert second.number == 2
        assert second.startup_nonce != first.startup_nonce
        assert second.process != first.process
        assert fixture.generations[1].launched_at > outage.completed_at
    finally:
        cleanup = fixture.close()
    assert cleanup.outputs_closed and cleanup.endpoint_refused and cleanup.errors == ()
    assert len(cleanup.generations) == 2
    assert all(record["exited"] for record in cleanup.generations)
    assert fixture.close() == cleanup


@pytest.mark.parametrize("generation", [1, 2])
@pytest.mark.parametrize("failure", ["popen", "launch_record", "identity", "readiness"])
def test_mcp_partial_start_keeps_exact_handles_for_cleanup(
    tmp_path, monkeypatch, generation, failure
):
    probe = _adapter_probe()
    evidence = probe.Evidence(tmp_path)
    fixture = probe.McpFixture(evidence, _fixture_env(probe), "run")
    try:
        if generation == 2:
            first = fixture.start_first()
            fixture.stop_first()
        if failure == "popen":
            monkeypatch.setattr(
                probe.subprocess,
                "Popen",
                lambda *a, **k: (_ for _ in ()).throw(OSError("launch failed")),
            )
        elif failure == "launch_record":
            monkeypatch.setattr(
                evidence,
                "record_launch_start",
                lambda *a: (_ for _ in ()).throw(RuntimeError("record failed")),
            )
        elif failure == "identity":
            monkeypatch.setattr(
                probe.psutil,
                "Process",
                lambda *a: (_ for _ in ()).throw(RuntimeError("identity failed")),
            )
        else:
            monkeypatch.setattr(
                probe,
                "wait_for",
                lambda *a: (_ for _ in ()).throw(RuntimeError("readiness failed")),
            )
        with pytest.raises((OSError, RuntimeError)):
            if generation == 1:
                fixture.start_first()
            else:
                fixture.restart(_fixture_outage(probe, fixture, first))
        assert len(fixture.generations) == generation
        with pytest.raises(RuntimeError, match="has not completed startup"):
            _ = fixture.generations[-1].started
    finally:
        cleanup = fixture.close()
    assert cleanup.outputs_closed and cleanup.endpoint_refused
    assert all(record["exited"] for record in cleanup.generations)
    assert len(cleanup.errors) >= 1


def test_mcp_refusal_requires_connection_refused(tmp_path, monkeypatch):
    probe = _adapter_probe()
    fixture = probe.McpFixture(probe.Evidence(tmp_path), {}, "run")
    assert fixture.refused()
    monkeypatch.setattr(
        probe.socket,
        "create_connection",
        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timeout")),
    )
    with pytest.raises(TimeoutError):
        fixture.refused()
    assert fixture.close().errors


def test_model_constructor_closes_server_after_thread_start_failure(tmp_path, monkeypatch):
    probe = _adapter_probe()
    original = probe.ThreadingHTTPServer
    servers = []

    def server(*args, **kwargs):
        result = original(*args, **kwargs)
        servers.append(result)
        return result

    monkeypatch.setattr(probe, "ThreadingHTTPServer", server)
    monkeypatch.setattr(
        probe.threading.Thread,
        "start",
        lambda *a: (_ for _ in ()).throw(RuntimeError("thread failed")),
    )
    with pytest.raises(RuntimeError, match="thread failed"):
        probe.ModelFixture(tmp_path, tmp_path, "run", None, None)
    assert servers[0].fileno() == -1


def test_mcp_model_negative_control_validates_envelope_before_literal(tmp_path):
    probe = _adapter_probe()
    fixture = probe.ModelFixture(tmp_path, tmp_path, "run", None, "WRONG")
    try:
        invocation, _, capture = _mcp_capture(probe)
        request = capture.model_requests[0]
        request["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": probe.MCP_TOOL,
                    "parameters": {
                        "type": "object",
                        "required": ["value", "phase", "call_nonce", "call_id"],
                        "properties": {key: {"type": "string"} for key in invocation.arguments()},
                    },
                },
            }
        ]
        with pytest.raises(RuntimeError, match="mcp tool result differs"):
            fixture.reply_mcp(request)
        request["messages"][-1]["content"] += " "
        with pytest.raises(RuntimeError, match="generation-bound"):
            fixture.reply_mcp(request)
    finally:
        fixture.close()


def test_adapter_allocation_precedes_fixture_launch(tmp_path, monkeypatch):
    probe = _adapter_probe()
    requested, _ = _kernel_pair(probe)
    monkeypatch.setattr(probe.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path / "runtime"))
    (tmp_path / "runtime").mkdir()
    run = probe.AdapterRun(
        SimpleNamespace(
            expected_tool=None,
            expected_mcp=None,
            prime_path=Path("/prime"),
            kernel_python=Path("/kernel/bin/python"),
        ),
        probe.Evidence(tmp_path / "evidence"),
        requested,
    )
    assert run._fixture is None
    with pytest.raises(RuntimeError, match="has not completed startup"):
        _ = run.fixture
    assert run.mcp.generations == []
    assert run.server is None
    monkeypatch.setattr(
        probe,
        "ModelFixture",
        lambda *a: (_ for _ in ()).throw(RuntimeError("model startup failed")),
    )
    with pytest.raises(RuntimeError, match="model startup failed"):
        run.start()
    result = run.mcp.close()
    assert result.endpoint_refused and result.outputs_closed and result.errors == ()


def test_mcp_model_accepts_identical_history_and_rejects_another_execution(tmp_path):
    probe = _adapter_probe()
    fixture = probe.ModelFixture(tmp_path, tmp_path, "run", None, None)
    try:
        invocation, _, capture = _mcp_capture(probe)
        request = capture.model_requests[0]
        request["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": probe.MCP_TOOL,
                    "parameters": {
                        "type": "object",
                        "required": ["value", "phase", "call_nonce", "call_id"],
                        "properties": {key: {"type": "string"} for key in invocation.arguments()},
                    },
                },
            }
        ]
        initial = json.loads(json.dumps(request))
        initial["messages"] = initial["messages"][:1]
        delta, reason = fixture.reply_mcp(initial)
        assert reason == "tool_calls"
        assert delta["tool_calls"][0]["id"] == "adapter-call"
        with pytest.raises(RuntimeError, match="duplicate model"):
            fixture.reply_mcp(initial)
        expected = {"role": "assistant", "content": "ADAPTER_MCP_DONE call-nonce"}
        assert fixture.reply_mcp(request) == (expected, "stop")
        assert fixture.reply_mcp(request) == (expected, "stop")
    finally:
        fixture.close()


@pytest.mark.parametrize(
    "change",
    [
        None,
        {"pid": True},
        {"pid": 99},
        {"token": 1},
        {"count": "42"},
        {"count": True},
        {"object_id": None},
        {"socket_id": []},
        {"socket_fileno": False},
        {"socket_fileno": -1},
    ],
)
def test_mcp_memory_waiter_matches_a_json_prompt_with_escaped_code(tmp_path, monkeypatch, change):
    probe = _adapter_probe()
    root = _selected_root_identity(probe)

    class Run:
        evidence = SimpleNamespace(root=tmp_path)

        def wait_for_idle(self, _session):
            return {}

        def post_event(self, _scenario, _session, body):
            prompt = body["data"]["content"][0]["text"]
            self.spec = json.loads(prompt.removeprefix("ADAPTER_MCP_MEMORY "))
            self.prompt = prompt
            probe.write_json(
                tmp_path / "mcp-memory-before.json",
                {
                    "pid": 13,
                    "token": "token",
                    "count": 42,
                    "object_id": 101,
                    "socket_id": 102,
                    "socket_fileno": 9,
                }
                | (change or {}),
            )
            return SimpleNamespace(status_code=202)

        def wait_for_reply(self, _session, expected, prompt, _timeout):
            transcript = {"content": self.prompt, "reply": self.spec["ack"]}
            assert prompt in json.dumps(transcript)
            assert expected == self.spec["ack"]
            return transcript

        def selected_root_identity(self, _session):
            return root

    run = Run()
    if change is not None:
        with pytest.raises(RuntimeError, match="MCP memory"):
            probe._mcp_memory(run, object(), root, "before")
        return
    result = probe._mcp_memory(run, object(), root, "before")
    assert dataclasses.asdict(result) == {
        "kernel": dataclasses.asdict(root.roles.kernel),
        "token": "token",
        "count": 42,
        "object_id": 101,
        "socket_id": 102,
        "socket_fileno": 9,
    }


@pytest.mark.parametrize("failure", ["stop", "output_close"])
def test_mcp_cleanup_retains_stop_and_output_errors(tmp_path, monkeypatch, failure):
    probe = _adapter_probe()
    fixture = probe.McpFixture(probe.Evidence(tmp_path), _fixture_env(probe), "run")
    original_output = None
    try:
        fixture.start_first()
        handle = fixture.generations[0]
        if failure == "stop":
            monkeypatch.setattr(
                handle.process,
                "terminate",
                lambda: (_ for _ in ()).throw(RuntimeError("stop failed")),
            )
        else:
            original_output = handle.output

            class Output:
                attempts = 0

                @property
                def closed(self):
                    return original_output.closed

                def close(self):
                    self.attempts += 1
                    if self.attempts == 1:
                        raise RuntimeError("output failed")
                    original_output.close()

            handle.output = Output()
        first = fixture.close()
        second = fixture.close()
        assert first.errors and second.errors[: len(first.errors)] == first.errors
        assert second.outputs_closed and second.endpoint_refused
        assert all(record["exited"] for record in second.generations)
    finally:
        fixture.close()
        if original_output is not None:
            original_output.close()


def test_mcp_failed_phase_attempts_final_continuity_and_preserves_first_failure(
    tmp_path, monkeypatch
):
    probe = _adapter_probe()
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "tools": [
                    {
                        "name": probe.MCP_TOOL,
                        "parameters": {
                            "type": "object",
                            "required": ["value", "phase", "call_nonce", "call_id"],
                            "properties": {
                                key: {"type": "string"}
                                for key in ["value", "phase", "call_nonce", "call_id"]
                            },
                        },
                    }
                ]
            }
        )
    )
    root = dataclasses.replace(_selected_root_identity(probe), config_path=str(config))
    memory = probe.KernelMemoryObservation(root.roles.kernel, "run", 42, 1, 2, 3)
    run = SimpleNamespace(
        nonce="run",
        evidence=probe.Evidence(tmp_path),
        mcp=SimpleNamespace(
            generations=[
                SimpleNamespace(
                    started=SimpleNamespace(
                        identity=probe.GenerationIdentity(
                            1, "startup", probe.ProcessIdentity(70, 7.0, ("python",))
                        )
                    )
                )
            ]
        ),
        selected_root_identity=lambda _session: root,
    )

    def memory_read(_run, _session, _root, name, **kwargs):
        if name == "final":
            raise RuntimeError("final memory failed")
        return memory

    monkeypatch.setattr(probe, "_mcp_memory", memory_read)
    monkeypatch.setattr(
        probe,
        "_mcp_phase",
        lambda *args: (_ for _ in ()).throw(RuntimeError("first phase failed")),
    )
    with pytest.raises(RuntimeError, match="first phase failed"):
        probe.verify_mcp_reconnect(run, object())
    receipt = json.loads((tmp_path / "mcp-continuity.json").read_text())
    assert receipt["first_failure"] == "RuntimeError: first phase failed"
    assert receipt["memory_final_error"] == "RuntimeError: final memory failed"
    assert receipt["root_final"]["session_id"] == "session-1"


def test_mcp_journal_selects_native_session_before_parsing_other_logs(tmp_path):
    probe = _adapter_probe()
    root = dataclasses.replace(_selected_root_identity(probe), bridge_root=str(tmp_path))
    journal = tmp_path / "prime-session-1.jsonl"
    journal.write_text('{"type":"session","id":"prime-session-1"}\n{"id":"native-user"}\n')
    (tmp_path / "kernel-history.jsonl").write_text("\nnot a native journal\n")
    run = SimpleNamespace(
        prime_sessions=tmp_path, fixture=SimpleNamespace(conversation_logs=set())
    )
    assert probe._mcp_journal(run, root) == [
        {"type": "session", "id": "prime-session-1"},
        {"id": "native-user"},
    ]
    journal.write_text('{"type":"session","id":"prime-session-1"}\nmalformed\n')
    with pytest.raises(json.JSONDecodeError):
        probe._mcp_journal(run, root)


def test_mcp_success_rejects_after_phase_from_first_generation():
    probe = _adapter_probe()
    invocation, generation, capture = _mcp_capture(probe, "after")
    with pytest.raises(RuntimeError, match="phase generation"):
        probe._require_success("run", invocation, generation, capture)
