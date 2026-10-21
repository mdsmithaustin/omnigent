# Prime 0.10 daemon bridge

Prime 0.10 is a Rust rewrite with no `--extension`. The 0.9 adapter ran the shared
Pi extension inside Prime to mirror the transcript, take web input, and apply
controls. On 0.10 an Omnigent-side bridge does those jobs over Prime's daemon
socket, the only interface that shares one live session with the visible TUI.

The 0.9 extension path stays for prime-agent builds whose `--help` lists
`--extension`. It is deprecated and is removed in Omnigent 0.20.0.

## Why the daemon, and its known limit

[NO_FORK.md](../prime-native/NO_FORK.md) rejected the daemon for 0.9.6 because
attach ignored replay cursors and a killed worker did not recover. Prime 0.10
still reports `replay.status: "unavailable"`. The extension alternative is gone,
so the bridge accepts the limit and recovers by snapshot: after any reconnect it
posts the snapshot messages it has not posted, keyed by stable `source_id`s, so
a repeat post is harmless. A conversation item whose post finds Omnigent
unreachable leaves the posted set. When the latest session status post fails
that way, the bridge keeps that status. The bridge re-attaches after the next
`agent_end`, so the snapshot posts the items again and the kept status follows
them. While a re-post still fails, the bridge retries whenever no agent loop is
open, waiting 1 s and doubling the wait up to 30 s. Model and effort changes are
posted again at the next attach or `agent_end`. Usage, compaction, interrupt,
delta, and dropped-message posts lost that way are not recovered. Worker-crash
recovery stays unqualified.

`--mode rpc`, `--mode json`, and `--mode acp` each run a private session that no
TUI can attach to. `prime-agent send` delivers an agent message, not user input.

## Observed 0.10 facts the design relies on

Probe logs live outside the repo; each fact below was observed on 0.10.0 darwin-arm64.

- `PRIME_AGENT_CODING_AGENT_DIR` relocates settings, logs, daemon workers and
  sessions. The adapter already points it at `<root>/agent`, so `set_model`
  writes the per-session copy, never `~/.prime/agent/settings.json`.
- `PRIME_AGENT_DAEMON_SOCKET` makes the TUI start its own daemon on that path.
  The root session appears in `list` within about 60 ms with
  `rlmDepth: 0`, `runtimeKind: "top-level"`, and `sessionFile` under `--session-dir`.
- Wire format: NDJSON. Hello `{"type":"daemon_hello","protocol":{"name":"prime-agent.daemon","version":7},"schemaRevision":30,...}`.
  The `schemaId` hash differs per build (0.9.6 `...f908f493c9e1`, 0.10.0 `...8e4b17c2a9f5`).
  Command envelope `{"type":"command","id":I,"protocol":P,"clientId":C,"command":{"id":I,"type":T,...}}`,
  reply `{"id":I,"type":"response","command":T,"success":bool,"data"|"error"}`.
- `attach {activeSessionId}` returns a snapshot (`messages[]`) and then streams
  `{"type":"session_event","activeSessionId":A,"event":E,"meta":{"sequence":N}}`
  for turns from any client. `E.type` is one of `agent_start`, `turn_start`,
  `message_start|update|end`, `tool_execution_start|end`, `turn_end`, `agent_end`,
  `compaction_start|end`. These are Pi's RPC event shapes.
- `prompt {activeSessionId, message, streamingBehavior?}` renders in the TUI as a
  normal user turn. `abort` ends the turn with `stopReason: "aborted"`, after which
  plain prompts fail with "queued session input is suspended" until `resume_queue`
  is sent (it replies `success:false` "No queued work to resume" and still clears
  it). Esc in the TUI pauses input the same way. A `steer` prompt is still
  admitted and clears the pause; `get_state` shows nothing of it.
- `/new` in the TUI starts a separate runtime: a new `rlmDepth: 0` id with its own
  `sessionId`, and an attach to the old id receives nothing for it. `/resume` in
  the TUI, while the earlier runtime is still live, returns to that runtime's id.
  `list` reports `attachedClients` per session, counting the TUI and any other
  attached client, this bridge included, which shows the root the TUI is on.
  A run of the bridge against a real TUI through `/new` and `/resume` posted each
  turn once and PATCHed the new resume id there and back to a recording fake
  server. The real server refuses to overwrite `external_session_id`, so the
  conversation keeps the first session's UUID.
- `set_model` and `set_thinking_level` emit no event. `get_state` reflects them.
- `compact` emits `compaction_start` and `compaction_end` (with `errorMessage` on failure).
- `--resume <session UUID>` restores the conversation under a new runtime id;
  the 12-hex runtime id does not resume. Key on `sessionId`.
- `kill` and `shutdown` leave an attached TUI running; the host ends it.
- There is no pre-tool-call hook. Policy denial of Prime's own tools is not
  possible on 0.10.

## Data shape

```
DaemonClient            NDJSON over AF_UNIX; validated hello; command() -> data;
                        events() async iterator of session_event envelopes.
TranscriptState         response_id, turn counter, per-message stream ids,
                        source_ids already posted, cumulative usage.
reduce(state, event)    pure; returns (state, [omnigent event]) using the same
                        /events shapes the extension posted.
reconcile(state, msgs,   pure; returns events for snapshot messages whose
  streaming)            source_ids are not yet posted. It closes a loop that
                        ended while detached, and opens as running a loop
                        that started while detached
                        (omnigent/harnesses/prime_native/transcript.py).
run_bridge(root)        lives in the terminal wrapper next to the Prime process:
                        attach to the root the TUI shows, then a session loop
                        (events -> POST /events) and one task per tick step:
                        binding.json heartbeat, controls/requests -> daemon
                        commands -> controls/results, inbox -> prompt (plus
                        input_ready), and re-attach when the TUI's root
                        changes, or when posts failed, no agent loop is open,
                        and the retry backoff has passed. The inbox checks the
                        TUI's root before each prompt.
                        The first beat claims binding.json while the
                        wrapper's owner claim names this process; after that
                        the beat, inbox, and control answers also require
                        binding.json to name this bridge.
```

Controls map one to one: `model` -> `set_model` then `get_state`; `effort` ->
`set_thinking_level` then `get_state`; `compact` -> `compact`; `interrupt` ->
`abort` then `resume_queue`. The bridge posts the model and effort change events
itself because the daemon emits none.

## Relay tools on 0.10

Prime 0.10 reads `mcpServers` from the agent dir's `settings.json` and exposes
each enabled server to the model through the kernel's `mcp` object, not as native
tools. The launch writes an `omnigent` stdio entry (`type`, `command`, `args`,
and an `{"env": "OMNIGENT_DATA_DIR"}` reference) into the per-session copy, keeping
the user's other entries and dropping the entry it wrote when the session has no
relay tools. The command is the shared shim `python -I -m omnigent.harnesses.claude_native.bridge
serve-mcp --bridge-dir <root>`; the launch also seeds the token-only `bridge.json`
it reads. Prime passes a stdio child only `HOME`, `PATH`, `TMPDIR` and `{"env": NAME}`
references, rejecting literal env values and unset references. Calls reach the
runner's relay and then the server's MCP proxy, which runs TOOL_CALL policy.
Observed on 0.10.0: no approval prompt in the TUI, and a denied call raises
`McpToolError`.

## Known gaps on 0.10

- Policy hard-blocks of Prime's own tool calls are not enforced. A launch whose
  spec has tool-call or tool-result policies, or function policies that declare
  no phase, logs a warning and posts a `native_policy_not_enforced` error item
  naming them.
- An interrupt during a running tool can leave the last assistant stop reason
  `toolUse`; the bridge reports that turn idle, not interrupted. Not yet
  observed on 0.10.
- Tools from an agent's declared MCP servers are not advertised: the `serve-mcp`
  shim lists only the relay's built-in tools (`tool_relay.json`).
- The injected `task` todo tool is unavailable.
- Model or effort changed in the TUI reaches the web UI only on the next
  `get_state` poll at a turn boundary.
- Events missed during a disconnect are recovered as whole messages, without deltas.
  When 1024 events wait unread, the daemon client logs a warning, drops them, and
  closes the connection, so they are recovered the same way.
- The TUI has no `/omnigent` command, status line, or title; the extension set them.
