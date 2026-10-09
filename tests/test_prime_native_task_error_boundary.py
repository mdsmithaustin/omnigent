from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.harnesses.pi_native.test_pi_native_extension import _MCP_SOURCE_SEMANTIC_PROVENANCE
from tests.test_prime_native_root_binding import run_extension

SDK_SOURCE_SEMANTICS = r"""
async function primeExecute(prepared) {
  const updates = [];
  let accepting = true;
  try {
    const result = await prepared.tool.execute(prepared.toolCall.id, prepared.args,
      undefined, update => { if (accepting) updates.push(Promise.resolve(update)); });
    accepting = false;
    await Promise.all(updates);
    return { result, isError: false };
  } catch (error) {
    accepting = false;
    await Promise.all(updates).catch(() => {});
    return { result: { content: [{ type: "text", text: error instanceof Error
      ? error.message : String(error) }],
      details: {} }, isError: true };
  }
}
async function primeFinalize(prepared, executed, after) {
  let { result, isError } = executed;
  const patch = await after({ toolCall: prepared.toolCall, args: prepared.args, result, isError });
  if (patch) {
    result = { content: patch.content ?? result.content, details: patch.details ?? result.details,
      terminate: patch.terminate ?? result.terminate };
    isError = patch.isError ?? isError;
  }
  return { toolCall: prepared.toolCall, result, isError };
}
function primeMessage(finalized) {
  return { role: "toolResult", toolCallId: finalized.toolCall.id,
    toolName: finalized.toolCall.name,
    content: finalized.result.content, details: finalized.result.details,
      isError: finalized.isError };
}
async function piExecute(prepared) {
  const updateEvents = [];
  let acceptingUpdates = true;
  try {
    const result = await prepared.tool.execute(prepared.toolCall.id, prepared.args, undefined,
      update => { if (acceptingUpdates) updateEvents.push(Promise.resolve(update)); });
    acceptingUpdates = false;
    await Promise.all(updateEvents);
    return { result, isError: false };
  } catch (error) {
    acceptingUpdates = false;
    await Promise.all(updateEvents);
    return { result: { content: [{ type: "text", text: error instanceof Error
      ? error.message : String(error) }],
      details: {} }, isError: true };
  } finally { acceptingUpdates = false; }
}
async function piFinalize(prepared, executed, after) {
  let result = executed.result;
  let isError = executed.isError;
  const afterResult = await after({ toolCall: prepared.toolCall, args: prepared.args, result,
    isError });
  if (afterResult) {
    result = { ...result, content: afterResult.content ?? result.content,
      details: afterResult.details ?? result.details, usage: afterResult.usage ?? result.usage,
      terminate: afterResult.terminate ?? result.terminate };
    isError = afterResult.isError ?? isError;
  }
  return { toolCall: prepared.toolCall, result, isError };
}
function piMessage(finalized) {
  return { role: "toolResult", toolCallId: finalized.toolCall.id,
    toolName: finalized.toolCall.name,
    content: finalized.result.content ?? [], details: finalized.result.details,
    usage: finalized.result.usage, isError: finalized.isError };
}
const sdk = consumer.startsWith("prime")
  ? { execute: primeExecute, finalize: primeFinalize, message: primeMessage }
  : { execute: piExecute, finalize: piFinalize, message: piMessage };

"""


@pytest.mark.parametrize("consumer", ["prime-0.9.6", "pi-0.84.2"])
def test_local_task_denials_follow_selected_sdk_source_semantics(
    tmp_path: Path, consumer: str
) -> None:
    provenance = _MCP_SOURCE_SEMANTIC_PROVENANCE[consumer]
    assert provenance["sdk_execution"] is False
    assert provenance["sources"]
    run_extension(
        tmp_path,
        "const consumer = "
        + json.dumps(consumer)
        + ";\n"
        + SDK_SOURCE_SEMANTICS
        + r"""
const unavailable = "Omnigent tools require a live Prime root binding";
const initial = [{ id: 1, title: "root task", description: "root", status: "completed" }];
const denied = [{ id: 2, title: "denied task", description: "child", status: "in-progress" }];
let sequence = 0, continuations = 0, payloadReads = 0;
const write = { operation: "write", get todoList() { payloadReads += 1; return denied; } };
async function consume(i, name, args, ctx, expectedError, expectedText) {
  const toolCall = { id: `call-${++sequence}`, name, arguments: args };
  const prepared = { toolCall, args, tool: { execute: (...values) =>
    i.tools[name].execute(...values, ctx) } };
  const executed = await sdk.execute(prepared);
  const observations = [];
  const finalized = await sdk.finalize(prepared, executed, async event => {
    const observed = { toolName: name, toolCallId: toolCall.id, input: args,
      content: event.result.content, details: event.result.details, isError: event.isError };
    observations.push(observed);
    return i.handlers.tool_result(observed, ctx);
  });
  const message = sdk.message(finalized);
  assert.deepEqual({ role: message.role, id: message.toolCallId, name: message.toolName,
    isError: message.isError }, { role: "toolResult", id: toolCall.id, name,
    isError: expectedError });
  assert.equal(observations.length, 1);
  assert.equal(observations[0].isError, expectedError);
  if (expectedText !== undefined) assert.equal(message.content[0].text, expectedText);
  const model = history => {
    assert.equal(history.length, 1);
    assert.equal(history[0].toolCallId, toolCall.id);
    assert.equal(history[0].isError, expectedError);
    continuations += 1;
    return { role: "assistant", stopReason: "stop", content: [{ type: "text",
      text: JSON.stringify([history[0].toolCallId, history[0].isError]) }] };
  };
  assert.equal(model([message]).content[0].text,
    JSON.stringify([toolCall.id, expectedError]));
  return message;
}
async function deny(i, ctx) {
  const posts = posted.length, reads = payloadReads;
  const before = fs.existsSync(bindingFile) ? binding() : null;
  const files = fs.existsSync(primeControlsDir)
    ? fs.readdirSync(primeControlsDir).sort() : [];
  await consume(i, "manage_todo_list", write, ctx, true, unavailable);
  await consume(i, "root_tool", {}, ctx, true, unavailable);
  await flush();
  assert.equal(posted.length, posts);
  assert.equal(payloadReads, reads);
  assert.equal(fs.existsSync(bindingFile) ? binding() : null, before);
  assert.deepEqual(fs.existsSync(primeControlsDir)
    ? fs.readdirSync(primeControlsDir).sort() : [], files);
}
async function rootWorks(i) {
  const result = await consume(i, "manage_todo_list", { operation: "write", todoList: initial },
    i.ctx, false, "Task plan updated (1 task).");
  assert.deepEqual(result.details, { operation: "write", todos: initial });
  assert.deepEqual(posted.filter(entry => entry.body.type === "external_session_todos")
    .at(-1).body.data.todos,
    [{ content: "root task", status: "completed", activeForm: "root" }]);
  const read = await consume(i, "manage_todo_list", { operation: "read" }, i.ctx, false);
  assert.deepEqual(read.details, { operation: "read", todos: initial });
  await consume(i, "root_tool", {}, i.ctx, false, "root relay result");
}
await consume(rootInstance, "root_tool", {}, rootInstance.ctx, true, unavailable);
assert.deepEqual(posted, []);
await rootInstance.handlers.session_start({}, rootInstance.ctx);
await rootWorks(rootInstance);
const child = instance({ rlmDepth: 1 });
await deny(rootInstance, child.ctx);
await deny(rootInstance, undefined);
const unchanged = await consume(rootInstance, "manage_todo_list", { operation: "read" },
  rootInstance.ctx, false);
assert.deepEqual(unchanged.details.todos, initial);
const baseFetch = global.fetch;
global.fetch = async (url, options) => String(url).endsWith("/mcp")
  ? { ok: true, status: 200, json: async () => ({ result: {
    content: [{ type: "text", text: unavailable }], isError: false } }) }
  : baseFetch(url, options);
await consume(rootInstance, "root_tool", {}, rootInstance.ctx, false, unavailable);
global.fetch = baseFetch;
fs.unlinkSync(bindingFile);
await deny(rootInstance, rootInstance.ctx);
const replacement = instance({ rlmDepth: 0 });
await replacement.handlers.session_start({}, replacement.ctx);
await rootWorks(replacement);
await deny(rootInstance, rootInstance.ctx);
await replacement.handlers.session_shutdown({}, replacement.ctx);
await deny(replacement, replacement.ctx);
const failed = instance({ rlmDepth: 0 });
const mkdir = fs.mkdirSync;
fs.mkdirSync = () => { throw Error("unwritable binding"); };
await assert.rejects(failed.handlers.session_start({}, failed.ctx),
  { name: "Error", message: "unwritable binding" });
fs.mkdirSync = mkdir;
const failedPosts = posted.length;
await consume(failed, "root_tool", {}, failed.ctx, true, unavailable);
assert.equal(posted.length, failedPosts);
assert.equal(fs.existsSync(bindingFile), false);
const fresh = instance({ rlmDepth: 0 });
await fresh.handlers.session_start({}, fresh.ctx);
await rootWorks(fresh);
await rootInstance.handlers.session_shutdown({}, rootInstance.ctx);
await failed.handlers.session_shutdown({}, failed.ctx);
await fresh.handlers.session_shutdown({}, fresh.ctx);
assert.equal(fs.existsSync(bindingFile), false);
assert.equal(timers.size, 0);
assert.equal(payloadReads, 0);
assert.equal(continuations, 23);
""",
    )
