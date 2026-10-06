from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = r"""
const assert = require("assert").strict;
const fs = require("fs");
const path = require("path");
const root = process.argv[2];
const inboxDir = path.join(root, "inbox");
const primeControlsDir = path.join(root, "controls");
const bindingFile = path.join(primeControlsDir, "binding.json");
fs.mkdirSync(inboxDir);
const configPath = path.join(root, "config.json");
fs.writeFileSync(configPath, JSON.stringify({ inboxDir, primeControlsDir,
  serverUrl: "http://test", sessionId: "s", tools: [{ name: "root_tool" }] }));
process.env.OMNIGENT_EXTENSION_NATIVE_CONFIG = configPath;
const posted = [];
global.fetch = async (url, options) => {
  posted.push({ url: String(url), body: JSON.parse(options.body) });
  return { ok: true, status: 200, json: async () => ({
    result: { content: [{ type: "text", text: "root relay result" }] },
    action: "allow",
  }) };
};
const timers = new Map();
let timerId = 0;
global.setInterval = (fn) => { timers.set(++timerId, fn); return timerId; };
global.clearInterval = (id) => timers.delete(id);
const flush = () => new Promise(resolve => setImmediate(resolve));
async function tick() { for (const fn of timers.values()) fn(); await flush(); }
const extension = require(process.argv[1]);
function instance(header) {
  const handlers = {}, tools = {}, commands = {}, delivered = [], mutations = [], statuses = [];
  const ctx = {
    sessionManager: {
      getHeader: () => header, getSessionId: () => "native-root", getBranch: () => [],
    },
    model: { provider: "verify", id: "fixture" },
    modelRegistry: { getAvailable: () => [{ provider: "verify", id: "fixture" }] },
    isIdle: () => true, abort: () => mutations.push("abort"),
    ui: { setStatus: (...args) => statuses.push(args), notify: (...args) => statuses.push(args) },
  };
  const pi = {
    on: (name, handler) => { handlers[name] = handler; },
    registerCommand: (name, command) => { commands[name] = command; },
    registerTool: tool => { tools[tool.name] = tool; },
    getAllTools: () => Object.values(tools),
    getThinkingLevel: () => "medium",
    setThinkingLevel: async value => {
      mutations.push(value);
      await handlers.thinking_level_select({ level: value }, ctx);
    },
    sendUserMessage: (text, options) => delivered.push({ text, ...options }),
  };
  extension(pi);
  return { ctx, pi, handlers, tools, commands, delivered, mutations, statuses };
}
const rootInstance = instance({ rlmDepth: 0, parentSession: "ordinary-fork" });
const binding = () => fs.readFileSync(bindingFile, "utf8");
const controlId = "1".padStart(32, "0");
const controlFile = path.join(primeControlsDir, "requests", `${controlId}.json`);
const resultFile = path.join(primeControlsDir, "results", `${controlId}.json`);
const inboxFile = path.join(inboxDir, "message.json");
function queue() {
  fs.writeFileSync(inboxFile, JSON.stringify({
    id: "message", type: "user_message", content: "root input" }));
  fs.writeFileSync(controlFile, JSON.stringify({ id: controlId, type: "effort", level: "high",
    incarnation: JSON.parse(binding()).incarnation, expiresAt: Date.now() + 10000 }));
}
async function exercise(i, ctx = i.ctx) {
  const message = { role: "assistant", content: [{ type: "text", text: "private child output" }],
    usage: { input: 12, output: 7 }, timestamp: 123, stopReason: "stop" };
  const todos = [{ id: 1, title: "child task", description: "child", status: "completed" }];
  const payloads = {
    session_tree: {}, model_select: { model: ctx.model }, thinking_level_select: { level: "high" },
    agent_start: {}, turn_start: { turnIndex: 0 },
    message_update: { assistantMessageEvent: {
      type: "text_delta", delta: "private child delta", contentIndex: 0 } },
    tool_execution_start: {},
    tool_call: { toolName: "bash", toolCallId: "child-call", input: { command: "echo child" } },
    tool_result: { toolName: "manage_todo_list", toolCallId: "child-call", details: { todos } },
    tool_execution_end: { toolName: "bash", toolCallId: "child-call",
      result: { content: [{ type: "text", text: "child result" }] } },
    input: { text: "private child input" }, message_end: { message },
    turn_end: { message, toolResults: [] }, agent_end: { messages: [message] },
  };
  for (const [name, event] of Object.entries(payloads)) await i.handlers[name](event, ctx);
  await i.commands.omnigent.handler("", ctx);
  await flush();
}
async function relay(i, ctx = i.ctx) {
  return i.tools.root_tool.execute("call", { text: "payload" }, undefined, undefined, ctx);
}
async function assertRootWorks() {
  await tick();
  assert.deepEqual(rootInstance.delivered, [{ text: "root input", deliverAs: "followUp" }]);
  assert.deepEqual(rootInstance.mutations, ["high"]);
  assert.equal(JSON.parse(fs.readFileSync(resultFile)).status, "applied");
  assert.deepEqual((await relay(rootInstance)).content,
    [{ type: "text", text: "root relay result" }]);
  await rootInstance.handlers.input({ text: "root transcript" }, rootInstance.ctx);
  assert.equal(posted.at(-1).body.data.item_data.content[0].text, "root transcript");
}
"""


def run_extension(tmp_path: Path, script: str) -> None:
    node = shutil.which("node")
    assert node, "Node is required for Prime ownership regressions"
    extension = (
        Path(__file__).resolve().parents[1]
        / "omnigent/resources/pi_native/omnigent_pi_native_extension.js"
    )
    result = subprocess.run(
        [
            node,
            "-e",
            HARNESS
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


@pytest.mark.parametrize(
    "header",
    [
        "{ rlmDepth: 1 }",
        "{ rlmDepth: 3 }",
        "undefined",
        "null",
        "{}",
        "Object.assign([], { rlmDepth: 0 })",
        '"malformed"',
        "{ rlmDepth: -1 }",
        "{ rlmDepth: 0.5 }",
        '{ rlmDepth: "0" }',
        "{ rlmDepth: false }",
        "{ rlmDepth: NaN }",
        "{ rlmDepth: Infinity }",
        "{ get rlmDepth() { throw Error('bad header'); } }",
    ],
)
def test_non_root_callbacks_leave_root_binding_and_projection_untouched(
    tmp_path: Path, header: str
) -> None:
    run_extension(
        tmp_path,
        f"""
await rootInstance.handlers.session_start({{}}, rootInstance.ctx);
queue();
const before = binding(), posts = posted.length, timerCount = timers.size;
const child = instance({header});
await child.handlers.session_start({{ reason: "startup" }}, child.ctx);
await exercise(child);
assert.equal(binding(), before);
assert.equal(posted.length, posts);
assert.equal(timers.size, timerCount);
assert.deepEqual(child.statuses, []);
assert.equal(fs.existsSync(controlFile), true);
assert.equal(fs.existsSync(inboxFile), true);
assert.equal(fs.existsSync(resultFile), false);
assert.equal((await relay(child)).isError, true);
assert.equal(posted.length, posts);
if (child.handlers.session_shutdown) await child.handlers.session_shutdown({{}}, child.ctx);
assert.equal(binding(), before);
assert.equal(timers.size, timerCount);
await assertRootWorks();
""",
    )


@pytest.mark.parametrize(
    "header_access",
    [
        "delete i.ctx.sessionManager",
        "i.ctx.sessionManager.getHeader = () => { throw Error('unavailable'); }",
    ],
)
def test_missing_or_throwing_manager_fails_closed(tmp_path: Path, header_access: str) -> None:
    run_extension(
        tmp_path,
        f"""
const i = rootInstance;
{header_access};
await i.handlers.session_start({{}}, i.ctx);
await exercise(i);
assert.equal(fs.existsSync(bindingFile), false);
assert.deepEqual(posted, []);
assert.equal((await relay(i)).isError, true);
const live = instance({{ rlmDepth: 0 }});
await live.handlers.session_start({{}}, live.ctx);
assert.deepEqual((await relay(live)).content, [{{ type: "text", text: "root relay result" }}]);
""",
    )


def test_unknown_callbacks_and_execution_context_cannot_borrow_root_relay(tmp_path: Path) -> None:
    run_extension(
        tmp_path,
        r"""
await exercise(rootInstance);
assert.equal((await relay(rootInstance)).isError, true);
assert.deepEqual(posted, []);
assert.equal(timers.size, 0);
await rootInstance.handlers.session_start({}, rootInstance.ctx);
queue();
const child = instance({ rlmDepth: 1 });
const posts = posted.length;
await exercise(rootInstance, child.ctx);
assert.equal((await relay(rootInstance, child.ctx)).isError, true);
assert.equal((await rootInstance.tools.root_tool.execute("call", {})).isError, true);
const taskResult = await rootInstance.tools.manage_todo_list.execute("call",
  { operation: "write", todoList: [] }, undefined, undefined, child.ctx);
assert.equal(taskResult.isError, true);
assert.equal(posted.length, posts);
await assertRootWorks();
const result = await rootInstance.tools.manage_todo_list.execute("call", {
  operation: "write", todoList: [
    { id: 1, title: "root task", description: "root", status: "completed" }],
}, undefined, undefined, rootInstance.ctx);
assert.equal(result.details.todos[0].title, "root task");
assert.deepEqual(posted.at(-1).body.data.todos,
  [{ content: "root task", status: "completed", activeForm: "root" }]);
""",
    )


def test_shutdown_and_replacement_preserve_only_current_root_binding(tmp_path: Path) -> None:
    run_extension(
        tmp_path,
        r"""
await rootInstance.handlers.session_start({}, rootInstance.ctx);
const old = JSON.parse(binding()).incarnation;
const replacement = instance({ rlmDepth: 0 });
await replacement.handlers.session_start({}, replacement.ctx);
const current = JSON.parse(binding()).incarnation;
assert.notEqual(old, current);
await tick();
assert.equal(JSON.parse(binding()).incarnation, current);
await rootInstance.handlers.session_shutdown({}, rootInstance.ctx);
assert.equal(JSON.parse(binding()).incarnation, current);
assert.deepEqual((await relay(replacement)).content,
  [{ type: "text", text: "root relay result" }]);
await replacement.handlers.session_shutdown({}, replacement.ctx);
assert.equal(fs.existsSync(bindingFile), false);
const posts = posted.length;
await tick();
await exercise(replacement);
assert.equal((await relay(replacement)).isError, true);
assert.equal(posted.length, posts);
assert.equal(timers.size, 0);
""",
    )


def test_root_policy_and_tool_approval_still_relay(tmp_path: Path) -> None:
    run_extension(
        tmp_path,
        r"""
await rootInstance.handlers.session_start({}, rootInstance.ctx);
const baseFetch = global.fetch;
const mcpRequests = [];
let policy = "POLICY_ACTION_DENY";
global.fetch = async (url, options) => {
  if (String(url).endsWith("/policies/evaluate")) {
    return { ok: true, json: async () => ({ result: policy, reason: "root policy" }) };
  }
  if (String(url).endsWith("/mcp")) {
    mcpRequests.push(JSON.parse(options.body));
    if (mcpRequests.length === 1) return { ok: true, json: async () => ({ result: {
      resultType: "input_required", inputRequests: { approval: {} }, requestState: "pending",
    } }) };
  }
  return baseFetch(url, options);
};
const verdict = await rootInstance.handlers.tool_call(
  { toolName: "bash", toolCallId: "native", input: { command: "echo root" } }, rootInstance.ctx);
assert.deepEqual(verdict, { block: true, reason: "root policy" });
policy = "POLICY_ACTION_ALLOW";
assert.deepEqual((await relay(rootInstance)).content,
  [{ type: "text", text: "root relay result" }]);
assert.equal(mcpRequests.length, 2);
assert.deepEqual(mcpRequests[1].params, {
  name: "root_tool", arguments: { text: "payload" }, requestState: "pending",
  inputResponses: { approval: { action: "accept" } },
});
""",
    )


def test_failed_root_binding_start_does_not_admit_callbacks_or_tools(tmp_path: Path) -> None:
    run_extension(
        tmp_path,
        r"""
const mkdir = fs.mkdirSync;
fs.mkdirSync = () => { throw Error("unwritable binding"); };
await assert.rejects(rootInstance.handlers.session_start({}, rootInstance.ctx),
  /unwritable binding/);
fs.mkdirSync = mkdir;
await exercise(rootInstance);
assert.equal((await relay(rootInstance)).isError, true);
assert.equal(fs.existsSync(bindingFile), false);
assert.deepEqual(posted, []);
await rootInstance.handlers.session_start({}, rootInstance.ctx);
queue();
await assertRootWorks();
""",
    )
