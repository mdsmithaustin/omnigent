from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from omnigent.harnesses.prime_native.catalog import PrimeModelRef, parse_model_table
from omnigent.harnesses.prime_native.controls import (
    Compact,
    ControlStatus,
    Interrupt,
    PrimeExtensionBinding,
    SetEffort,
    SetModel,
)

TABLE = (
    "provider  model    context  max-out  thinking  images\n"
    "verify    fixture  32.8K    1.0K     no        no    \n"
)


def test_public_catalog_preserves_provider_and_model() -> None:
    assert parse_model_table(TABLE) == [PrimeModelRef("verify", "fixture")]
    assert (
        parse_model_table(
            "No models available. Use /login to log into a provider via OAuth or API key. See:\n"
            "  /prime/docs/providers.md\n  /prime/docs/models.md\n"
        )
        == []
    )


@pytest.mark.parametrize(
    "output",
    [
        "",
        "provider model\n",
        TABLE.splitlines()[0],
        TABLE + TABLE.splitlines()[1],
        TABLE.replace("32.8K", "?????"),
        TABLE.replace("no        no", "yes       maybe"),
        TABLE + "warning: partial catalog\n",
        TABLE.replace("verify    fixture", "verify fixture"),
    ],
)
def test_public_catalog_rejects_malformed_and_duplicate_rows(output: str) -> None:
    with pytest.raises(ValueError):
        parse_model_table(output)


def test_catalog_uses_configured_executable_and_ambient_prime_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.harnesses.prime_native.catalog import model_options

    executable = tmp_path / "prime-agent"
    executable.write_text(
        f"#!{os.sys.executable}\n"
        "import os, sys\n"
        "if sys.argv[1:] == ['--version']: print('0.9.6')\n"
        "else:\n"
        " assert sys.argv[1:] == ['model', 'list']\n"
        " assert os.environ['PRIME_AGENT_CODING_AGENT_DIR'] == '/ambient-prime'\n"
        f" print({TABLE!r})\n"
    )
    executable.chmod(0o700)
    monkeypatch.setenv("OMNIGENT_PRIME_PATH", str(executable))
    monkeypatch.setenv("PRIME_AGENT_CODING_AGENT_DIR", "/ambient-prime")
    assert model_options() == [
        {"id": "verify/fixture", "model": "verify/fixture", "displayName": "verify/fixture"}
    ]


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError(),
        subprocess.TimeoutExpired("prime", 1),
        subprocess.CalledProcessError(1, "prime"),
    ],
)
def test_catalog_does_not_fallback_on_command_failure(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    from omnigent.harnesses.prime_native import catalog

    monkeypatch.setattr(catalog, "resolve_prime_executable", lambda: "/prime-agent")

    def fail(*args: object, **kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(catalog.subprocess, "run", fail)
    with pytest.raises(type(failure)):
        catalog.model_options()


def live_binding(root: Path) -> Path:
    controls = root / "controls"
    (controls / "requests").mkdir(parents=True)
    (controls / "results").mkdir()
    (controls / "binding.json").write_text(
        json.dumps(
            {
                "incarnation": "live",
                "pid": os.getpid(),
                "heartbeat": time.time() * 1000,
            }
        )
    )
    return controls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("control", "reply", "expected"),
    [
        (
            SetModel(PrimeModelRef("verify", "fixture")),
            {"status": "applied", "model": "verify/fixture"},
            (ControlStatus.APPLIED, "verify/fixture", None),
        ),
        (
            SetEffort("ultra"),
            {"status": "applied", "effort": "low"},
            (ControlStatus.APPLIED, None, "low"),
        ),
        (
            Interrupt(),
            {"status": "interrupt_accepted"},
            (ControlStatus.INTERRUPT_ACCEPTED, None, None),
        ),
        (
            Compact(),
            {"status": "rejected", "detail": "failed"},
            (ControlStatus.REJECTED, None, None),
        ),
        (SetEffort("high"), {"status": "applied"}, (ControlStatus.UNKNOWN, None, None)),
        (
            SetEffort("high"),
            {"status": "applied", "effort": "high", "incarnation": "stale"},
            (ControlStatus.UNKNOWN, None, None),
        ),
    ],
)
async def test_control_waits_for_matching_effective_result(
    tmp_path: Path, control, reply, expected
) -> None:
    controls = live_binding(tmp_path)

    async def resident() -> dict:
        for _ in range(100):
            requests = list((controls / "requests").glob("*.json"))
            if requests:
                request = requests[0]
                payload = json.loads(request.read_text())
                request.unlink()
                (controls / "results" / request.name).write_text(
                    json.dumps(
                        {
                            "id": payload["id"],
                            "incarnation": payload["incarnation"],
                            **reply,
                        }
                    )
                )
                return payload
            await asyncio.sleep(0.005)
        raise AssertionError("No control reached the resident extension")

    task = asyncio.create_task(resident())
    outcome = await PrimeExtensionBinding(tmp_path).execute(control, timeout_s=1)
    payload = await task
    assert (
        outcome.status,
        outcome.model.selector if outcome.model else None,
        outcome.effort,
    ) == expected
    if isinstance(control, SetEffort) and control.effort == "ultra":
        assert payload["level"] == "max"
    assert list((controls / "requests").iterdir()) == []
    assert list((controls / "results").iterdir()) == []


@pytest.mark.asyncio
async def test_missing_ack_is_unknown_and_never_replayed(tmp_path: Path) -> None:
    controls = live_binding(tmp_path)
    outcome = await PrimeExtensionBinding(tmp_path).execute(Compact(), timeout_s=0.08)
    assert outcome.status == ControlStatus.UNKNOWN
    assert outcome.http_status == 504
    await asyncio.sleep(0.1)
    assert list((controls / "requests").iterdir()) == []


@pytest.mark.asyncio
async def test_dead_or_missing_binding_cannot_admit_control(tmp_path: Path) -> None:
    assert (
        await PrimeExtensionBinding(tmp_path).execute(Interrupt())
    ).status == ControlStatus.UNAVAILABLE
    controls = live_binding(tmp_path)
    (controls / "binding.json").write_text(
        json.dumps(
            {
                "incarnation": "dead",
                "pid": os.getpid(),
                "heartbeat": 0,
            }
        )
    )
    assert (
        await PrimeExtensionBinding(tmp_path).execute(SetEffort("high"))
    ).status == ControlStatus.UNAVAILABLE
    assert list((controls / "requests").iterdir()) == []


@pytest.mark.asyncio
async def test_message_readiness_is_bounded_and_uses_live_control_binding(tmp_path: Path) -> None:
    binding = PrimeExtensionBinding(tmp_path)
    assert await binding.wait_until_ready(timeout_s=0.01) is False
    controls = live_binding(tmp_path)
    assert await binding.wait_until_ready(timeout_s=0) is True
    (controls / "binding.json").write_text("[]")
    assert await binding.wait_until_ready(timeout_s=0) is False


EXTENSION_HARNESS = r"""
const assert = require("assert").strict;
const fs = require("fs");
const path = require("path");
const root = process.argv[2];
const inboxDir = path.join(root, "inbox");
const primeControlsDir = path.join(root, "controls");
fs.mkdirSync(inboxDir);
const configPath = path.join(root, "config.json");
fs.writeFileSync(configPath, JSON.stringify({ inboxDir, primeControlsDir,
  serverUrl: "http://test", sessionId: "s" }));
process.env.OMNIGENT_EXTENSION_NATIVE_CONFIG = configPath;
const posted = [];
const events = (type) => posted.filter(e => e.type === type);
global.fetch = async (url, options) => {
  if (String(url).endsWith("/events")) posted.push(JSON.parse(options.body));
  return { ok: true, status: 202, json: async () => ({}) };
};
const timers = [];
global.setInterval = (fn) => { timers.push(fn); return timers.length; };
global.clearInterval = () => {};
const handlers = {};
let level = "medium";
let calls = [];
let aborted = 0;
let compactOptions;
let activeController = null;
const models = [{ provider: "verify", id: "fixture" }, { provider: "other", id: "fixture" }];
const ctx = {
  sessionManager: { getHeader: () => ({ rlmDepth: 0 }) },
  model: models[0], modelRegistry: { getAvailable: () => models,
  getAll: () => { throw Error("unfiltered catalog"); } },
  isIdle: () => activeController === null,
  get signal() { return activeController?.signal; },
  abort: () => { aborted++; activeController?.abort(); },
  compact: (options) => { compactOptions = options; },
};
const pi = {
  on: (name, handler) => { handlers[name] = handler; }, registerCommand() {},
  setModel: async (model) => {
    calls.push(`${model.provider}/${model.id}`); ctx.model = model;
    await handlers.model_select({ model, source: "set" }, ctx); return true;
  },
  setThinkingLevel: async (value) => {
    level = value === "max" ? "low" : value;
    await handlers.thinking_level_select({ level }, ctx);
  },
  getThinkingLevel: () => level,
};
require(process.argv[1])(pi);
let sequence = 0;
const flush = () => new Promise((resolve) => setImmediate(resolve));
async function tick() { for (const timer of timers) timer(); await flush(); }
async function startLoop() {
  const controller = new AbortController();
  activeController = controller;
  await handlers.agent_start({}, ctx);
  return controller;
}
async function endLoop(messages) {
  activeController = null;
  await handlers.agent_end({ messages }, ctx);
}
function enqueue(type, data = {}) {
  const id = (++sequence).toString(16).padStart(32, "0");
  const binding = JSON.parse(fs.readFileSync(path.join(primeControlsDir, "binding.json")));
  const record = { id, type, incarnation: binding.incarnation, expiresAt: Date.now() + 10000,
  ...data };
  fs.writeFileSync(path.join(primeControlsDir, "requests", `${id}.json`), JSON.stringify(record));
  return id;
}
function result(id) { return JSON.parse(fs.readFileSync(path.join(primeControlsDir, "results",
  `${id}.json`))); }
async function request(type, data) { const id = enqueue(type, data); await tick();
  return result(id); }
async function waitResult(id) {
  for (let n = 0; n < 400; n++) {
    if (fs.existsSync(path.join(primeControlsDir, "results", `${id}.json`))) return result(id);
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  throw Error("No bounded control result");
}
"""


def run_extension(tmp_path: Path, script: str) -> None:
    node = shutil.which("node")
    assert node, "Node is required for Prime extension regressions"
    extension = (
        Path(__file__).resolve().parents[1]
        / "omnigent/resources/pi_native/omnigent_pi_native_extension.js"
    )
    result = subprocess.run(
        [
            node,
            "-e",
            EXTENSION_HARNESS
            + "\n(async () => {\n"
            + script
            + "\n})().catch(error => { console.error(error); process.exit(1); });",
            str(extension),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_extension_applies_exact_model_and_observes_clamped_effort(tmp_path: Path) -> None:
    run_extension(
        tmp_path,
        r"""
await handlers.session_start({}, ctx);
assert.equal(events("external_reasoning_effort_change")[0].data.reasoning_effort, "medium");
const selected = await request("model", { provider: "other", modelId: "fixture" });
assert.equal(selected.status, "applied"); assert.equal(selected.model, "other/fixture");
const effort = await request("effort", { level: "max" });
assert.equal(effort.status, "applied"); assert.equal(effort.effort, "low");
await handlers.model_select({ model: models[0], source: "restore" }, ctx);
await handlers.thinking_level_select({ level: "off" }, ctx);
assert.deepEqual(
  events("external_model_change").map(e => e.data.model),
  ["verify/fixture", "other/fixture", "verify/fixture"]);
assert.deepEqual(
  events("external_reasoning_effort_change").map(e => e.data.reasoning_effort),
  ["medium", "low", "none"]);
assert.deepEqual(
  events("external_model_options")[0].data.models.map(m => m.id),
  ["verify/fixture", "other/fixture"]);
""",
    )


@pytest.mark.parametrize(
    "invalid",
    [
        '{ provider: "missing", modelId: "fixture" }',
        '{ modelId: "fixture" }',
        '{ provider: "verify", modelId: "fixture", expiresAt: 1 }',
        '{ provider: "verify", modelId: "fixture", incarnation: "stale" }',
    ],
)
def test_extension_rejects_invalid_expired_and_stale_controls(
    tmp_path: Path, invalid: str
) -> None:
    run_extension(
        tmp_path,
        f"""
await handlers.session_start({{}}, ctx);
assert.equal((await request("model", {invalid})).status, "rejected");
assert.deepEqual(calls, []);
assert.equal((await request("model", {{ provider: "verify", modelId: "fixture" }})).status,
  "applied");
assert.deepEqual(calls, ["verify/fixture"]);
""",
    )


def test_extension_rejects_ambiguous_or_unauthenticated_model_and_missing_api(
    tmp_path: Path,
) -> None:
    run_extension(
        tmp_path,
        r"""
await handlers.session_start({}, ctx);
models.push(models[0]);
assert.equal((await request("model", { provider: "verify", modelId: "fixture" })).status,
  "rejected");
models.pop(); pi.setModel = async () => false;
assert.equal((await request("model", { provider: "verify", modelId: "fixture" })).status,
  "rejected");
delete pi.setModel;
assert.equal((await request("model", { provider: "verify", modelId: "fixture" })).status,
  "unavailable");
delete pi.setThinkingLevel;
assert.equal((await request("effort", { level: "high" })).status, "unavailable");
delete ctx.compact;
assert.equal((await request("compact")).status, "unavailable");
""",
    )


@pytest.mark.parametrize(
    "callback, expected",
    [
        ('compactOptions.onComplete({ summary: "short" })', "applied"),
        ("compactOptions.onComplete()", "unknown"),
        ("compactOptions.onComplete({})", "unknown"),
        ('compactOptions.onError(new Error("failed"))', "rejected"),
    ],
)
def test_compact_waits_for_callback_and_interrupt_bypasses_it(
    tmp_path: Path, callback: str, expected: str
) -> None:
    run_extension(
        tmp_path,
        f'''
await handlers.session_start({{}}, ctx);
await startLoop();
const id = enqueue("compact"); await tick();
assert.equal(fs.existsSync(path.join(primeControlsDir, "results", `${{id}}.json`)), false);
assert.equal((await request("interrupt")).status, "interrupt_accepted");
assert.equal(aborted, 1);
assert.deepEqual(
  events("external_session_status").map(e => e.data.status),
  ["running"]);
{callback}; await flush();
assert.equal(result(id).status, "{expected}");
await tick(); assert.equal(aborted, 1);
assert.deepEqual(
  events("external_compaction_status").map(e => e.data.status),
  ["in_progress", "{"completed" if expected == "applied" else "failed"}"]);
''',
    )


def test_extension_observes_control_and_terminal_tool_phase_interrupts(
    tmp_path: Path,
) -> None:
    run_extension(
        tmp_path,
        r"""
await handlers.session_start({}, ctx);
const controlled = await startLoop();
const controlledResponse = events("external_session_status").at(-1).data.response_id;
assert.equal((await request("interrupt")).status, "interrupt_accepted");
assert.equal(controlled.signal.aborted, true);
await endLoop([
  { role: "assistant", stopReason: "toolUse" },
  { role: "toolResult", isError: true,
    content: [{ type: "text", text: "Tool execution aborted" }] },
]);
assert.deepEqual(
  events("external_session_status").map(e => [e.data.status, e.data.response_id]),
  [["running", controlledResponse], ["failed", controlledResponse]]);
assert.deepEqual(
  events("external_session_interrupted").map(e => e.data.response_id),
  [controlledResponse]);

const terminal = await startLoop();
const terminalResponse = events("external_session_status").at(-1).data.response_id;
terminal.abort();
await endLoop([{ role: "assistant", stopReason: "toolUse" }]);
assert.deepEqual(
  events("external_session_status").slice(-2).map(e => [e.data.status, e.data.response_id]),
  [["running", terminalResponse], ["failed", terminalResponse]]);
assert.deepEqual(
  events("external_session_interrupted").map(e => e.data.response_id),
  [controlledResponse, terminalResponse]);
assert.equal(aborted, 1);
""",
    )


def test_extension_admission_and_tool_errors_do_not_infer_interruption(
    tmp_path: Path,
) -> None:
    run_extension(
        tmp_path,
        r"""
await handlers.session_start({}, ctx);
ctx.abort = () => { aborted++; };
const admitted = await startLoop();
assert.equal((await request("interrupt")).status, "interrupt_accepted");
assert.equal(admitted.signal.aborted, false);
await endLoop([{ role: "assistant", stopReason: "toolUse" }]);

await startLoop();
await endLoop([
  { role: "assistant", stopReason: "toolUse" },
  { role: "toolResult", isError: true,
    content: [{ type: "text", text: "ordinary tool failure" }] },
]);
assert.deepEqual(
  events("external_session_status").map(e => e.data.status),
  ["running", "idle", "running", "idle"]);
assert.deepEqual(events("external_session_interrupted"), []);
assert.equal(aborted, 1);
""",
    )


def test_extension_interrupt_requires_the_current_non_aborted_loop_signal(
    tmp_path: Path,
) -> None:
    run_extension(
        tmp_path,
        r"""
await handlers.session_start({}, ctx);
assert.equal((await request("interrupt")).status, "unavailable");
assert.equal(aborted, 0);

const retained = await startLoop();
activeController = new AbortController();
assert.equal((await request("interrupt")).status, "unavailable");
activeController = null;
assert.equal((await request("interrupt")).status, "unavailable");
assert.equal(aborted, 0);
await endLoop([{ role: "assistant", stopReason: "stop" }]);

const alreadyAborted = await startLoop();
alreadyAborted.abort();
assert.equal((await request("interrupt")).status, "unavailable");
assert.equal(aborted, 0);
await endLoop([{ role: "assistant", stopReason: "toolUse" }]);
assert.equal(retained.signal.aborted, false);
""",
    )


def test_extension_consumes_interrupted_signal_before_duplicate_or_next_loop(
    tmp_path: Path,
) -> None:
    run_extension(
        tmp_path,
        r"""
await handlers.session_start({}, ctx);
const first = await startLoop();
first.abort();
const messages = [{ role: "assistant", stopReason: "toolUse" }];
await endLoop(messages);
await handlers.agent_end({ messages }, ctx);
assert.equal(events("external_session_interrupted").length, 1);
assert.deepEqual(
  events("external_session_status").map(e => e.data.status),
  ["running", "failed"]);

const second = await startLoop();
await endLoop([{ role: "assistant", stopReason: "toolUse" }]);
assert.equal(second.signal.aborted, false);
assert.equal(events("external_session_interrupted").length, 1);
assert.deepEqual(
  events("external_session_status").map(e => e.data.status),
  ["running", "failed", "running", "idle"]);
""",
    )


def test_extension_shutdown_discards_the_active_loop_signal(tmp_path: Path) -> None:
    run_extension(
        tmp_path,
        r"""
await handlers.session_start({}, ctx);
const stale = await startLoop();
await handlers.session_shutdown({}, ctx);
stale.abort();
activeController = null;
await handlers.session_start({}, ctx);
assert.equal((await request("interrupt")).status, "unavailable");
await handlers.agent_end({ messages: [{ role: "assistant", stopReason: "toolUse" }] }, ctx);
assert.deepEqual(events("external_session_interrupted"), []);
assert.deepEqual(
  events("external_session_status").map(e => e.data.status),
  ["running"]);
assert.equal(aborted, 0);
""",
    )


def test_extension_failed_and_aborted_loops_never_publish_successful_idle(tmp_path: Path) -> None:
    run_extension(
        tmp_path,
        r"""
await handlers.session_start({}, ctx);
for (const stopReason of ["error", "aborted", "stop"]) {
  await startLoop();
  await endLoop([{ role: "assistant", stopReason }]);
}
assert.deepEqual(
  events("external_session_status").map(e => e.data.status),
  ["running", "failed", "running", "failed", "running", "idle"]);
assert.equal(events("external_session_interrupted").length, 1);
""",
    )


def test_extension_waits_for_observed_idle_after_quiet_work(tmp_path: Path) -> None:
    run_extension(
        tmp_path,
        r"""
await handlers.session_start({}, ctx);
ctx.isIdle = () => false;
await startLoop();
await endLoop([{ role: "assistant", stopReason: "stop" }]);
await tick();
assert.deepEqual(
  events("external_session_status").map(e => e.data.status),
  ["running", "waiting"]);
ctx.isIdle = () => true; await tick(); await flush();
assert.deepEqual(
  events("external_session_status").map(e => e.data.status),
  ["running", "waiting", "idle"]);
""",
    )


def test_extension_consumes_once_when_native_api_throws(tmp_path: Path) -> None:
    run_extension(
        tmp_path,
        r"""
await handlers.session_start({}, ctx);
let attempts = 0;
ctx.compact = () => { attempts++; throw new Error("failed"); };
assert.equal((await request("compact")).status, "unknown");
await tick(); await tick();
assert.equal(attempts, 1);
assert.deepEqual(events("external_compaction_status").map(e => e.data.status),
  ["in_progress", "failed"]);
""",
    )


def test_extension_rechecks_expiry_after_queued_settings(tmp_path: Path) -> None:
    run_extension(
        tmp_path,
        r"""
await handlers.session_start({}, ctx);
let release;
pi.setModel = () => new Promise(resolve => { release = resolve; });
const model = enqueue("model", { provider: "verify", modelId: "fixture" });
await tick();
const effort = enqueue("effort", { level: "high", expiresAt: Date.now() + 10 });
await tick();
await new Promise(resolve => setTimeout(resolve, 20));
await handlers.model_select({ model: models[0], source: "set" }, ctx);
release(true); await flush(); await flush();
assert.equal(result(model).status, "applied");
assert.equal(result(effort).status, "rejected");
assert.equal(level, "medium");
""",
    )


def test_shutdown_drops_pending_control_results_and_compaction_projection(tmp_path: Path) -> None:
    run_extension(
        tmp_path,
        r"""
await handlers.session_start({}, ctx);
const id = enqueue("compact"); await tick();
assert.deepEqual(events("external_compaction_status").map(e => e.data.status), ["in_progress"]);
await handlers.session_shutdown({}, ctx);
compactOptions.onComplete({ summary: "late" }); await flush();
await tick();
assert.equal(fs.existsSync(path.join(primeControlsDir, "binding.json")), false);
assert.equal(fs.existsSync(path.join(primeControlsDir, "results", `${id}.json`)), false);
assert.deepEqual(events("external_compaction_status").map(e => e.data.status), ["in_progress"]);
""",
    )


@pytest.mark.parametrize(
    "failure",
    [
        "return { ok: true, status: 500 };",
        "return { ok: false, status: 401 };",
        'throw Error("network");',
        "return { ok: true };",
        'return { ok: true, status: "202" };',
        "return new Promise(() => {});",
    ],
)
@pytest.mark.parametrize("kind", ["model", "effort"])
def test_setting_publication_failure_is_unknown_and_next_control_recovers(
    tmp_path: Path, kind: str, failure: str
) -> None:
    event_type = "external_model_change" if kind == "model" else "external_reasoning_effort_change"
    run_extension(
        tmp_path,
        f'''
await handlers.session_start({{}}, ctx);
const baseFetch = global.fetch;
const failedType = "{event_type}";
global.fetch = async (url, options) => {{
  if (JSON.parse(options.body).type === failedType) {{ {failure} }}
  return baseFetch(url, options);
}};
const id = enqueue("{kind}", {{ provider: "other", modelId: "fixture", level: "high",
  expiresAt: Date.now() + 80 }});
await tick();
assert.equal((await waitResult(id)).status, "unknown");
assert.equal({"ctx.model.provider" if kind == "model" else "level"},
  "{"other" if kind == "model" else "high"}");
global.fetch = baseFetch;
assert.equal((await request("effort", {{ level: "low" }})).status, "applied");
await tick();
assert.equal(level, "low");
assert.equal(events("external_reasoning_effort_change").at(-1).data.reasoning_effort, "low");
assert.deepEqual(calls, {'["other/fixture"]' if kind == "model" else "[]"});
''',
    )


@pytest.mark.parametrize("kind", ["model", "effort"])
@pytest.mark.parametrize("callback", ["missing", "late", "same-value", "same-value-missing"])
def test_setting_ack_requires_real_callback_after_setter_return(
    tmp_path: Path, kind: str, callback: str
) -> None:
    missing = "missing" in callback
    same_value = callback.startswith("same-value")
    provider = "verify" if same_value else "other"
    effort = "medium" if same_value else "high"
    event_type = "external_model_change" if kind == "model" else "external_reasoning_effort_change"
    observed = (
        {"model": f"{provider}/fixture"} if kind == "model" else {"reasoning_effort": effort}
    )
    run_extension(
        tmp_path,
        f'''
await handlers.session_start({{}}, ctx);
posted.length = 0;
pi.setModel = async model => {{ ctx.model = model; return true; }};
pi.setThinkingLevel = value => {{ level = value; }};
const id = enqueue("{kind}", {{ provider: "{provider}", modelId: "fixture", level: "{effort}",
  expiresAt: Date.now() + 80 }});
await tick();
assert.equal(fs.existsSync(path.join(primeControlsDir, "results", `${{id}}.json`)), false);
if ({str(not missing).lower()}) {{
  await new Promise(resolve => setTimeout(resolve, 20));
  if ("{kind}" === "model") await handlers.model_select({{ model: ctx.model }}, ctx);
  else await handlers.thinking_level_select({{ level }}, ctx);
}}
const receipt = await waitResult(id);
assert.equal(receipt.status, "{"unknown" if missing else "applied"}");
assert.equal(ctx.model.provider, "{provider if kind == "model" else "verify"}");
assert.equal(level, "{effort if kind == "effort" else "medium"}");
assert.deepEqual(events("{event_type}").map(e => e.data),
  {json.dumps([] if missing else [observed])});
''',
    )


@pytest.mark.parametrize(
    "model_callback, setter_result", [(True, "true"), (False, "true"), (True, "false")]
)
def test_model_clamp_requires_all_callback_publications(
    tmp_path: Path, model_callback: bool, setter_result: str
) -> None:
    run_extension(
        tmp_path,
        f'''
await handlers.session_start({{}}, ctx);
const baseFetch = global.fetch;
global.fetch = async (url, options) => {{
  if (JSON.parse(options.body).type === "external_reasoning_effort_change")
    return {{ status: 500 }};
  return baseFetch(url, options);
}};
pi.setModel = async model => {{
  level = "low";
  await handlers.thinking_level_select({{ level }}, ctx);
  ctx.model = model;
  if ({str(model_callback).lower()}) await handlers.model_select({{ model }}, ctx);
  return {setter_result};
}};
const id = enqueue("model", {{ provider: "other", modelId: "fixture",
  expiresAt: Date.now() + 80 }});
await tick();
assert.equal((await waitResult(id)).status, "unknown");
assert.equal(level, "low");
assert.equal(ctx.model.provider, "other");
assert.equal(events("external_model_change").at(-1).data.model,
  "{"other/fixture" if model_callback else "verify/fixture"}");
''',
    )


def test_child_shutdown_on_root_instance_does_not_close_binding(tmp_path: Path) -> None:
    run_extension(
        tmp_path,
        r"""
await handlers.session_start({}, ctx);
const child = { ...ctx, sessionManager: { getHeader: () => ({ rlmDepth: 1 }) } };
await handlers.session_shutdown({}, child);
assert.equal((await request("effort", { level: "low" })).status, "applied");
assert.equal(level, "low");
assert.equal(events("external_reasoning_effort_change").at(-1).data.reasoning_effort, "low");
""",
    )


def test_startup_observation_cannot_satisfy_a_missing_native_callback(tmp_path: Path) -> None:
    run_extension(
        tmp_path,
        r"""
const baseFetch = global.fetch;
let release;
global.fetch = (url, options) => {
  if (JSON.parse(options.body).type === "external_reasoning_effort_change")
    return new Promise(resolve => { release = () => resolve({ status: 202 }); });
  return baseFetch(url, options);
};
const startup = handlers.session_start({}, ctx);
await flush();
pi.setModel = async model => { ctx.model = model; return true; };
const id = enqueue("model", { provider: "other", modelId: "fixture",
  expiresAt: Date.now() + 80 });
await tick();
release(); await startup;
assert.equal(events("external_model_change").at(-1).data.model, "other/fixture");
assert.equal((await waitResult(id)).status, "unknown");
assert.equal(ctx.model.provider, "other");
""",
    )


def test_startup_and_tui_publications_recover_from_hanging_fetch(tmp_path: Path) -> None:
    run_extension(
        tmp_path,
        r"""
const baseFetch = global.fetch;
let releaseLate;
global.fetch = (url, options) => {
  if (JSON.parse(options.body).type === "external_reasoning_effort_change")
    return new Promise(resolve => { releaseLate = resolve; });
  return baseFetch(url, options);
};
await handlers.session_start({}, ctx);
assert.equal(events("external_model_change").at(-1).data.model, "verify/fixture");
const abandoned = releaseLate;
const tui = handlers.thinking_level_select({ level: "high" }, ctx);
await flush();
global.fetch = baseFetch;
const newer = handlers.model_select({ model: models[1] }, ctx);
await Promise.all([tui, newer]);
assert.deepEqual(events("external_model_change").map(e => e.data.model),
  ["verify/fixture", "other/fixture"]);
assert.equal((await request("effort", { level: "low" })).status, "applied");
abandoned({ status: 202 }); releaseLate({ status: 202 }); await flush();
assert.equal(level, "low");
assert.equal(events("external_reasoning_effort_change").at(-1).data.reasoning_effort, "low");
""",
    )


def test_effort_receipt_uses_callback_value_instead_of_getter(tmp_path: Path) -> None:
    run_extension(
        tmp_path,
        r"""
await handlers.session_start({}, ctx);
pi.getThinkingLevel = () => "high";
const receipt = await request("effort", { level: "max" });
assert.equal(receipt.status, "applied");
assert.equal(receipt.effort, "low");
assert.equal(level, "low");
assert.equal(events("external_reasoning_effort_change").at(-1).data.reasoning_effort, "low");
""",
    )


def test_expired_hanging_setter_retains_native_execution_order(tmp_path: Path) -> None:
    run_extension(
        tmp_path,
        r"""
await handlers.session_start({}, ctx);
await startLoop();
let release;
pi.setModel = model => {
  calls.push(`${model.provider}/${model.id}`);
  return new Promise(resolve => { release = () => { ctx.model = model; resolve(true); }; });
};
const first = enqueue("model", { provider: "other", modelId: "fixture",
  expiresAt: Date.now() + 40 });
await tick();
const second = enqueue("effort", { level: "low" }); await tick();
await new Promise(resolve => setTimeout(resolve, 60));
assert.equal(level, "medium");
assert.equal(ctx.model.provider, "verify");
assert.equal((await request("interrupt")).status, "interrupt_accepted");
release(); await flush();
assert.equal((await waitResult(first)).status, "unknown");
assert.equal((await waitResult(second)).status, "applied");
assert.equal(ctx.model.provider, "other");
assert.equal(level, "low");
assert.deepEqual(calls, ["other/fixture"]);
""",
    )


def test_shutdown_settles_hanging_publication_and_suppresses_retired_receipt(
    tmp_path: Path,
) -> None:
    run_extension(
        tmp_path,
        r"""
await handlers.session_start({}, ctx);
const baseFetch = global.fetch;
global.fetch = (url, options) => {
  if (JSON.parse(options.body).type === "external_model_change") return new Promise(() => {});
  return baseFetch(url, options);
};
const retired = enqueue("model", { provider: "other", modelId: "fixture" });
await tick();
assert.equal(ctx.model.provider, "other");
await handlers.session_shutdown({}, ctx);
await flush();
assert.equal(fs.existsSync(path.join(primeControlsDir, "results", `${retired}.json`)), false);
global.fetch = baseFetch;
await handlers.session_start({}, ctx);
assert.equal((await request("effort", { level: "low" })).status, "applied");
assert.equal(level, "low");
assert.equal(events("external_reasoning_effort_change").at(-1).data.reasoning_effort, "low");
""",
    )
