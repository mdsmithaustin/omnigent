# Prime Native

`omnigent prime-native` connects Prime Agent's terminal to an Omnigent
conversation. Messages sent from either interface appear in the Omnigent
transcript. The harness identifier is `prime-native` and the CLI-created agent
is `prime-native-ui`.

## Launch and reattach

Install Prime Agent **0.9.6 or newer** and `tmux` on macOS or Linux. Omnigent
chooses the transcript bridge from the binary's `--help`. A release that lists
`--extension` (0.9.x) loads the Omnigent extension. That path is deprecated and
is removed in Omnigent 0.19.0. A release without it, such as the 0.10.0 Rust
build, runs with a [daemon bridge](#prime-010-daemon-bridge) instead. Releases
other than 0.9.6 and 0.10.0 are not separately qualified. Stop and the 0.10
bridge require daemon protocol 7 and schema revision 30. Configure Prime's authentication
before launching.
For xAI, run `/login` in Prime and select the xAI subscription or API-key entry.
Complete the selected flow as described in the
[pinned Prime 0.9.6 provider guide](https://github.com/PrimeIntellect-ai/prime-agent/blob/e260085dd8f742e0def3d871860c9a888b114851/packages/coding-agent/docs/providers.md#xai-grok).
Saved authentication is separate from the current provider and model labels.
A listed or selected model does not prove that a request will succeed.
When the host's normal Prime `auth.json` exists, every launch links the
conversation's `agent/auth.json` to it, replacing a legacy private copy or
an existing link without modifying the old link's target. Prime saves refreshed
logins through that link, so all conversations share the host login. Omnigent
seeds `settings.json` and `models.json` from the host's configuration only when each source file exists
and its private conversation copy is absent. It does not change those source
files. Resuming a conversation preserves existing private copies, so later host
settings changes are not automatically recopied. Configure the host's source
directory before creating a new conversation. Prime resolves authentication from that configuration.
Launch is rejected on native Windows because the published default supervisor
named pipe does not identify a private conversation for scoped shutdown.

To use an existing Python environment, set `PRIME_AGENT_KERNEL_PYTHON` to its
absolute `bin/python` entry. That environment must contain Prime's published
`prime-agent-runtime` and required packages. Start the host with this setting
before creating the session. Existing hosts and native workers retain their
launch environment. Without the setting, Prime manages its default kernel
environment.

```sh
omnigent prime-native
```

The command starts or connects to the configured Omnigent server, prints a
conversation URL, and attaches the terminal. To use an existing server, pass
`--server` with its URL. To select a Prime executable outside PATH, set
`OMNIGENT_PRIME_PATH` to its absolute path. `PRIME_AGENT_CODING_AGENT_DIR` selects
the source configuration directory; its default is `~/.prime/agent`.

Arguments after `--` reach Prime. For example, `--offline` disables Prime Cloud
requests while a configured model provider still supplies completions.

```sh
omnigent prime-native -- --offline
```

Close the attached terminal window to leave the attachment. The underlying
Prime terminal and worker keep running. Reattach with the Prime Native
conversation picker.

```sh
omnigent prime-native --resume
```

`--resume` also accepts the conversation ID printed by Omnigent for direct
attachment. If the native terminal exited, Omnigent uses Prime's captured
session ID and `--resume` to reopen saved history. A restarted worker does
not preserve Python memory.

Use the conversation's stop control to end its work. Exiting the required
Prime terminal also stops the private daemon. Omnigent owns Prime's session,
extension, daemon, and interactive mode arguments, so pass-through arguments
cannot override them.

If a terminal launch is still in progress, stop returns 503 and keeps its
ownership record. Retry after launch settles. A successful stop waits for the
private runtime's processes and sockets to disappear. Terminal closure failures
return 503 and retain the exact terminal owner, including failed launches.

## Change settings and control a turn

The model picker uses Prime's available registry. Before launch, the host reads
`prime-agent model list` with Prime's credentials. A live session reports its
own registry through the extension on 0.9.x and through the daemon's
`get_available_models` on 0.10. Model identifiers contain the exact
`provider/model` pair. Pi credentials and curated model lists are not a fallback
when Prime discovery fails.

For a live conversation, use its model and reasoning-effort controls. The public
API applies the same controls through `PATCH /v1/sessions/{id}` with
`model_override` and `reasoning_effort`. These controls also apply to a custom
agent whose executor uses `prime-native`, without a wrapper label. An explicit
harness override toward or away from Prime takes precedence over a stale wrapper
label. A combined request changes the model
before the effort. Prime's native observations update the displayed settings,
including changes made in the terminal. A delayed acknowledgment cannot replace
a newer terminal choice. Clearing a live setting or writing it with `silent`
is unsupported.

Omnigent maps `none` to Prime's `off` and `ultra` to `max`. Prime may clamp the
requested effort for the chosen model. The displayed value is the effective
native level.

A settings acknowledgment requires a native callback whose observation reaches
Omnigent with HTTP 2xx. Rejected or missing publication returns 504 after a
possible mutation. Prime 0.9.6 emits no callback when a requested model or effort
is already active, so those requests can also return 504. Omnigent does not
invent an observation or replay the control. Prime 0.10 emits no settings
event, so its bridge reads the setting back with `get_state` and publishes that
observation itself.

On 0.9.x each callback publication has a one-second deadline within the
control's remaining budget. On 0.10 the control's whole remaining budget bounds
the daemon commands and the publication. A slower response can return 504 even
if the server later stores the observation. Inspect the displayed native state
before another control.

Send `{"type":"compact"}` or `{"type":"interrupt"}` to
`POST /v1/sessions/{id}/events` for the corresponding control. On 0.9.x,
compaction waits for Prime's completion callback. On 0.10 it returns when the
daemon answers `compact`. A compaction Prime refuses is rejected, and a failure
after compaction starts shows as a failed compaction status from Prime's
`compaction_end` event. A session that is too short produced both on 0.10.0.
Interrupt returns admission before the native aborted result settles. Messages
also receive HTTP 202 for admission. Delivery to the inbox, which the extension
or the 0.10 bridge reads, does not complete a native turn.

Rejected controls return a non-success response. A missing live binding returns
503. An expired acknowledgment after possible mutation returns 504 and is not
retried. A combined settings failure retains the observed partial result.
Inspect the error's `outcomes` for the controls already attempted. Each runner
serializes its admitted settings in order. Independent terminal
selections remain outside that queue, and interrupt bypasses it.

## Use declared resources

A Prime Native agent receives its custom instructions after Prime's base
instructions. Omnigent appends framework instructions through the shared
runtime composer. The adapter does not copy framework policy into Prime.

An agent's declared MCP tools use Omnigent's existing session proxy. On Prime
0.9.x the shared extension advertises the discovered tools and the relay executes
them. On Prime 0.10 they are not yet advertised (see the
[daemon bridge](#prime-010-daemon-bridge)). Discovery failures appear in the server or runner logs. The
native terminal still starts when an additive MCP server is unavailable.

Relayed tool callbacks throw when dispatch returns an error or the live root
binding is unavailable. The native SDK turns that exception into a completed
error tool result. Local task operations also throw without a live root binding.
Calls rejected for a missing binding do not consume root controls or mutate
the root task list.

After successful discovery, the retained MCP connection can recover on a later
call even when an earlier call exhausted reconnect attempts. A failed first
initialization does not enable that recovery. Prime receives a completed native
tool error when runner dispatch fails. A subsequent eligible call can recover
the connection after the server returns. The existing ordinary-call circuit breaker
still applies. See [MCP recovery and retries](AGENT_YAML_SPEC.md#mcp-recovery-and-retries)
for serialized calls, startup cancellation, teardown timing, and MRTR limits.

Prime Native's
[qualification record](../designs/prime-native/NO_FORK.md#regular-http-mcp-reconnect-qualification)
covers one real regular HTTP case. Actual MRTR recovery and stdio restart remain
outside that measured case. The [remaining roadmap](#remaining-roadmap) also
records the earlier finite-wait qualification.

Bundled skills use `load_skill` and `read_skill_file`. A Python resource delivered
by those tools can execute in Prime's native `ipython` kernel. Configure resources
in an [agent specification](AGENT_YAML_SPEC.md) and start a session with that
agent. The `omnigent prime-native` shortcut creates the `prime-native-ui` agent.
It does not select a custom agent bundle.

Omnigent evaluates relayed root tools against the agent's policy. That gate does
not confine arbitrary Python code or recursive descendants.

## State and compatibility

An owner must use explicit [Stop](NATIVE_STOP.md) and verify current native
shutdown before forking with a different agent. A reader can then fork the
source. Ordinary resume clears that proof; same-agent history forks remain
available. Unknown or failed closure keeps a different-agent fork blocked.

Conversation state lives under `$OMNIGENT_DATA_DIR/prime-native/`, or
`~/.omnigent-mdsmithaustin/prime-native/` when the variable is unset. Each conversation has
its own settings and models, temporary directory, daemon socket, and saved
sessions. Authentication follows the shared-login behavior described under
[Launch and reattach](#launch-and-reattach). When that socket path would exceed
the Unix limit, Omnigent uses a private digest directory under `/tmp/mdp-<uid>/`.
Stop retains saved sessions. Conversation DELETE attempts adapter-state removal after shutdown.
Runner cleanup is best effort. A successful response can leave state when the
runner is unavailable or removal fails; it does not prove observed retirement.
Removing a conversation's auth link leaves the host login intact. Runtime
validation allows a symlink only at `agent/auth.json`; other runtime files
still reject symlinks.

Every fork installation for the same user shares `/tmp/mdp-<uid>/`. Each
digest is derived from the conversation's runtime path under its installation's
data directory, so installations never share a runtime directory. No
per-installation level is added because the socket-path headroom is small. A
cleanup sweep stops another installation's runtime only after its owner and
terminal have exited, and never deletes that installation's directory. Only
the owning installation removes a runtime directory, after its server confirms
the conversation was deleted.

Releases that predate shared authentication reject the auth link. After a
rollback to such a release, linked conversations cannot launch or delete their
runtime until you remove their `agent/auth.json` link. Remove only the link,
not the host auth file it points to.

Deletion keeps a tombstone of the conversation's native admission. After the
runtime has no owner, terminal, or private process, startup and host
maintenance ask its recorded server whether that admission was deleted. They
remove the runtime only on an exact `{"deleted": true}` answer for the recorded
epoch. Any other answer or failure keeps the runtime.
Each later conversation deletion purges its workspace's tombstones older than
30 days. The purge reads only each tombstone's deletion time, so a tombstone
written by another release no longer blocks deleting other conversations. A
tombstone without a readable deletion time is purged by the next deletion. A
runtime whose host has not checked before its tombstone is purged stays on
disk. The tombstone is stored as a closed admission, so a server rolled back to
a release without tombstones reads it as revoked.

Prime's main terminal is eligible for native pane idle cleanup. By default,
the runner checks every 60 seconds and closes the pane after one hour of
observed inactivity. Set `OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S` in the runner
environment to change that window; `0` disables pane reaping. Active turns,
observed native `running` or `waiting` status, and recent terminal output or
viewer input protect the pane. An attached viewer alone
does not. Closing the pane shuts down its private Prime runtime while retaining
saved history. Resume after that shutdown does not preserve Python memory.
Detaching preserves the living kernel only while the runtime remains alive.

Startup and host maintenance stop orphaned Prime runtimes whose recorded owner
is dead in the same process namespace and boot. A live terminal protects its
runtime even after its runner exits. Recovery retains saved sessions and private
configuration for resume, except for the deleted conversations described above.
Unknown or legacy ownership records remain untouched.
Cleanup sends Prime's public shutdown command to the qualified private
supervisor socket. Omnigent clears transient ownership only after that socket
and its owned processes stop. If ownership or shutdown cannot be proved, it
retains the runtime for a later cleanup attempt. The public
`prime-agent shutdown` CLI holds a per-user admission lease that can block
another Prime startup even with a private temporary directory. Omnigent uses
the private socket for session cleanup. Native Windows launch is rejected
because Prime's default named pipe is shared.

macOS can hide the arguments and environment of setuid helpers and exiting
processes. Cleanup recognizes an unreadable process through its verified PID
and start time or those of an owned ancestor, and waits for it to exit.
Cleanup also waits for an unreadable candidate without that verified identity
or lineage, but never signals it. If the shutdown deadline expires, the runtime
is retained. The error lists every remaining blocker category: unresolved
processes with their PID, name, and status, surviving owned process PIDs, and
live socket paths. An unreadable status appears as `status unreadable`.

The current launch path reserves and prepares the private runtime, then calls
`stop_prime_runtime` before dispatching even a fresh terminal. Ownership discovery
enumerates same-user Prime and matching Python candidates and reads
`process.environ()` before deciding whether they belong to this conversation.
The shutdown wait repeats that scan. This is an existing lifecycle limitation,
not a guarantee of inspection limited to recorded owned identities. Native
terminal and HTTP verification remains blocked when global process-environment
inspection is prohibited. A separate external observer does not bypass this caller.

Prime is a fork of Pi that now develops independently, as described in
[Prime's README](https://github.com/PrimeIntellect-ai/prime-agent/blob/e260085dd8f742e0def3d871860c9a888b114851/packages/coding-agent/README.md).
On 0.9.x the adapters share
the extension inbox, message conversion, and tool relay transport. On 0.10 the
bridge reads the same inbox and posts the same event shapes from its own
converter, and relay tools reach Prime through the shared `serve-mcp` shim. Prime
has its own launch, version check, session resume, and daemon shutdown code.
Pi's `--session` and `--list-models` are not Prime arguments. Use
`prime-agent model list` for Prime's credential-filtered model catalog.

Prime loads external extensions into recursive RLM children. Omnigent binds its
extension effects to the public session header's root depth. Children continue
executing in Prime. Their extension instances do not consume root controls or
publish their transcript as the Omnigent root conversation. Missing or invalid
depth is unknown and does not admit a root binding. On 0.10 the bridge attaches
only to a root session (RLM depth 0) under the conversation's session directory,
so child sessions' events are not mirrored as the root conversation.

The published daemon failed the replay-cursor and tested worker-recovery gates,
so the 0.9.x adapter keeps the extension transport. Prime 0.10 has no extension,
so its bridge accepts that limit and recovers missed messages from the attach
snapshot. Durable recovery across
missed events, global control exclusivity, completion-dependent declared children,
and whole-descendant settlement remain unqualified. Agent Profile Kit is outside
this implementation. Root policy hooks do not confine arbitrary Python code or
recursive descendants. [The acceptance matrix](../designs/prime-native/NO_FORK.md)
records each original requirement and its remaining evidence.

### Prime 0.10 daemon bridge

Prime 0.10 cannot load extensions. Omnigent starts it with a private daemon
socket, and a bridge in the terminal wrapper attaches to the root session (RLM
depth 0) that the TUI shows under the conversation's session directory. One
Omnigent session mirrors one Prime chat: the chat the bridge first attaches to,
or on a resume launch the chat recorded as the session's UUID. When `/new` or
`/resume` in the TUI moves to a different chat, the bridge mirrors nothing from
it, keeps web messages queued, rejects model, effort, and compact requests
(interrupt still stops a turn running in the original chat), and the conversation shows a notice to start a new session from the
web UI or return to the original chat in the terminal. When the TUI returns to
the original chat, the queued messages are delivered and a second notice says
so. When Omnigent has to relaunch Prime, `omnigent prime-native --resume`
reopens the recorded chat. Web messages are sent as steered prompts and appear
in the TUI as normal user turns.
Each web message reaches Prime at most once. If the daemon connection fails
before Prime confirms a prompt, the conversation shows that the message may not
have arrived, and it is not resent. Model, effort, compact, and interrupt map to
the daemon's `set_model`, `set_thinking_level`, `compact`, and `abort` commands.
After an abort, an interrupt also sends `resume_queue` so queued input is not
left paused. An Esc in the TUI pauses input the same way, but a steered web
message is still admitted while input is paused. If Prime refuses a web message,
the bridge retries it, sending `resume_queue` first when the refusal says input
is suspended, and reports it dropped after five refused attempts. Resume uses
Prime's session UUID.

The daemon reports no event for a model or effort chosen in the TUI, so the web
UI learns it when that turn ends. After a lost daemon connection the bridge
reattaches and posts the messages it missed as whole messages, without streamed
deltas. A turn that ended while the bridge was detached shows as idle, or as
failed when it was aborted or ended in a model error. A turn that started while
the bridge was detached shows as running. When Omnigent cannot be reached, the
bridge retries each post briefly. At the end of the turn it reattaches and posts
the conversation items that did not arrive, and the session status if its latest
post failed. If Omnigent is still unreachable, the bridge tries again while
Prime is idle, first after 1 second and then doubling the wait up to 30 seconds.
Usage, compaction progress, interrupt notices, streamed deltas, and
dropped-message notices lost in an outage are not re-posted. Only the bridge
that currently holds the conversation's control binding reads web messages and
answers controls. A new conversation's Prime settings skip the first-run prompt
and leave trace sharing off, unless the host settings already answer that
prompt. The bridge writes its diagnostics to `bridge.log` in the conversation's
runtime directory, rotated at 1 MB with one backup.

Omnigent's relay tools (`sys_os_*`, session, comment, and the other built-in
relay surface) reach Prime 0.10 as an `omnigent` MCP server. At launch Omnigent
adds a stdio entry to the conversation's own `settings.json` that runs the shared
`serve-mcp` shim against the session relay. Your other `mcpServers` entries are
kept, and your host `~/.prime/agent/settings.json` is never written. With relay
tools, the conversation's copy replaces any `omnigent` server of yours; without
them, such a server is left alone. A conversation `settings.json` that is not a
JSON object registers no relay tools and logs a runner warning. Prime lists
the server in its prompt and the model calls it from the `ipython` tool, for
example `await mcp.call_tool("omnigent", "sys_os_read", {...})`. Calls run through
the server's MCP proxy, so TOOL_CALL policy still applies: a denied call raises
`McpToolError` with the policy reason. Prime 0.10.0 showed no approval prompt for
these calls.

Prime 0.10 lacks these 0.9.x features:

- Omnigent policy cannot block Prime's own tool calls, such as `ipython`,
  because Prime 0.10 has no tool-call hook. Only the relay tools above are
  policy-gated. When the agent declares tool-call or tool-result policies, or
  function policies that declare no phase, the launch logs a warning in the
  runner log, and the conversation shows a `native_policy_not_enforced` notice
  that names them. The agent that `omnigent prime-native` creates declares none.
- The injected task list is not registered, and is not planned: Prime 0.10 has no native todo tool or hooks.
- An interrupt during a running tool may leave Prime's last stop reason
  `toolUse`, which the bridge reports as idle rather than interrupted. This is
  not yet observed on 0.10.
- Tools from an agent's declared MCP servers are not advertised to Prime,
  because the shim serves only the relay's built-in tool list.
- The web UI can miss part of a reply's live stream. If the bridge falls 1,024
  session events behind, for example while posts to Omnigent stall during a long
  reply, it drops those events, logs a warning in `bridge.log`, and reattaches.
  The reply then appears whole from the snapshot.
- The TUI has no `/omnigent` command and no Omnigent status line or title, and
  none is planned. On 0.9.x the extension showed the conversation URL there. Use
  the `Web UI:` link that `omnigent prime-native` prints before it attaches.

## Remaining roadmap

The supported adapter has measured launch, messages, controls, declared resources,
root tool denial, and living-kernel reattachment. Finite native `running` waits
passed on parent revision `afe870ce93c9df8bccfa88be13bb54c39d64e5ae`.
The later MCP working-tree repair passed one declared regular HTTP reconnect
case with configured xAI credentials and Grok 4.7. Finite waits have not been
rerun on those changed MCP and extension bytes. Each receipt identifies its
source, so neither result qualifies an untested final commit. Earlier provider
and MCP source units passed independent cumulative review
at `094a84e198a5ebdbb5e2c6e8934b189b43fce777`. The private composition with
actual merged PR5 requires fresh independent source and documentation review.
The verification workflow records
the current hashes separately from historical PASS+NOTES verdicts.
Fresh live WAIT and MCP receipts on final integrated bytes remain pending.

The next qualification priorities follow the gaps in the
[acceptance matrix](../designs/prime-native/NO_FORK.md#original-acceptance-criteria):

1. Extend N19 beyond regular HTTP. Exercise real MRTR callbacks and opaque
   approval retries across restart, stdio restart, wider error and cancellation
   paths, deadlines, server classes, and providers.
2. Extend N17 beyond finite `running` tools. Prove genuine `waiting`, status-only
   protection during complete output silence, default one-hour thresholds, and
   indefinite waits. Qualify gateway retry behavior separately.
3. Resolve the published daemon replay and worker-recovery failures before
   claiming N08 or N09. Global control exclusivity, completion-dependent declared
   children, and whole-descendant settlement remain gaps under N10, N15, and N18.
4. Qualify the remaining controls, rich output, approvals, descendant visibility,
   and external containment individually. Kit remains external to this adapter.

The original 31-item program remains incomplete. The workflows below repeat the
bounded passing cases and retain failures as separate evidence.

## Verify the adapter

The project skill drives a real Prime binary, Omnigent server, host, runner,
and terminal. A local model endpoint returns a fixed reply to prove message
transport without provider credentials. The ordinary drive's finalizer enumerates
global process candidates and reads process environments. Do not run the drive
when that inspection is prohibited.

For isolated public metadata checks, follow
[Doctor mode](../.agents/skills/verify-prime-native/SKILL.md#doctor) for commands,
receipts, privacy limits, and the bounds of its prerequisite proof.

```sh
uv sync --group dev
.agents/skills/verify-prime-native/scripts/verify.py --prime-path /tmp/omnigent-prime-runtime/application/prime-agent
```

Supply the actual path to your Prime binary, 0.9.6 or 0.10. For 0.10, also run
`--scenario relay` to exercise the relay tools and a policy denial. Require exit
code zero and `result.json` with `PASS` in the printed evidence directory. Inspect
`terminal-items.json` for both submitted messages and both
`PRIME_NATIVE_PONG` replies. `cleanup.json` must report no owned survivors.
`reattach.json` records the same native identity and live process IDs across
detach and resume. The helper retains its evidence after removing scratch state.
The relay run records `relay-items-1.json` and `relay-items-2.json` instead of the
terminal and reattach files; the verification skill lists what to require in them.

See [the verification skill](../.agents/skills/verify-prime-native/SKILL.md)
and its feature map for qualification gaps and additional scenarios.

The broader adapter driver executes real tools and a living Python kernel. It
uses disposable resources and a local model fixture, so commercial provider
credentials are not required. Supply the path to an interpreter with Prime's
published Python runtime installed.

```sh
PYTHONPATH="$PWD" uv run --no-sync python .agents/skills/verify-prime-native/scripts/adapter_probe.py --prime-path /absolute/path/to/prime-agent --kernel-python /absolute/path/to/kernel-venv/bin/python
```

Require exit code zero, every required scenario marked `VERIFIED` in
`outcomes.json`, an empty `fatal` list, and no owned survivors or forced cleanup
in `cleanup.json`. Inspect the retained tool results and actions. Require the
before and after source inventories to match in `source-integrity.json`, and
the live extension bytes to match in `deployed-extension-identities.json`.
Inspect `kernel-identity.json` for the supplied interpreter, environment prefix,
loaded `rlm` source, and owned kernel PID. The probe checks that identity against
the process records and retains the literal memory results across reattachment.
A readiness receipt is optional orchestration input,
not a prerequisite for public use. This broader probe pins the tested macOS
arm64 artifact. See the [full probe workflow](../.agents/skills/verify-prime-native/SKILL.md#full-adapter-probe)
for the runtime prerequisites, negative controls, and evidence inventory.
The [authenticated finite-wait workflow](../.agents/skills/verify-prime-native/SKILL.md#qualify-authenticated-finite-waits)
uses configured xAI credentials and Grok 4.7. Its historical five-case run on
`c22045ea9` passed its original checks with actual 60-second and 300-second Python
tools, matched idle controls, HTTP steering, and reattachment to the same living
kernel. The driver SHA256 was
`a2584ce9b149568e791f49ace5b7be7c332493770ad771cde18cb0e20d8335bb`.
Those bytes did not require exact public prompt ownership and could report
cleanup success while outer scratch remained. Their result publication could
also precede mandatory finalization. Preserve the historical receipts and their
limits. Earlier journal and pane cleanup failures remain FAILED.

The later parent run
`provider-waits-gkknmv6b` passed five cases on revision `afe870ce93c9df8bccfa88be13bb54c39d64e5ae`,
with actual 60-second and 300-second Python tools, matched runner and pane idle
controls, HTTP steering, and reattachment to the same living kernel. Its receipt distinguishes finite native
`running` from unqualified genuine `waiting` and status-only protection during
silence. The workflow requires copied-auth removal and exact owned-process
absence. It does not inspect unrelated terminal input. This run used the same
historical driver hash and shares the prompt, outer-scratch, and publication
limits above.

Current verification requires the exact native queue receipt before the tool
ends, followed by the tool result, queued user entry, and successful native reply.
HTTP 202 proves admission only. The public conversation must contain exactly one
fresh user message whose concatenated `input_text` equals the prompt, before the
selected final and with no intervening user. Native and public user and final
IDs remain independent. Require the ordered tool evidence and literal reply too.

The provider command now requires `--cooperative-cleanup`. Admit that flag only
for a controlled namespace with settled owned writers and no hostile concurrent
namespace writer. The flag, private permissions, and watches do not exclude
hostile same-UID interference in the final check/syscall window. That gap remains
BLOCKED. Cleanup proves scoped namespace removal, not physical erasure or
snapshot destruction.

Require each child's settlement, removal, and owner-closure receipts, plus
`completion.json` with matching result and manifest hashes. A provisional
`qualified` value is insufficient. The external scratch owner covers fallible
construction and requires explicit fixture settlement before removal. Errors
remain sticky, including failures after removal or during receipt and handle
closure. See the [verification workflow](../.agents/skills/verify-prime-native/SKILL.md#qualify-authenticated-finite-waits)
for the command, required receipt fields, and retained-failure checks.

The [qualification record](../designs/prime-native/NO_FORK.md#provider-probe-ownership-and-completion) owns the historical PR5 and PR6
composition identities, helper hashes, and source-review counts. Those results
do not qualify the current source. Fresh final-byte WAIT and MCP receipts remain
required after source or context changes.

The later audited `provider-waits-d_tb1jmg` run at
`4248f5dafee18884c8d66caa2f1480f1c619f58e` also remains FAILED. All five native
replies failed at kernel seed, before WAIT, with false committed completions.
All five lack external compact-root retirement authority. The precise DELETE
and filesystem branches and native cause remain unknown. The first case also
has a sticky `unrelated_user_process_identity_changed` failure with unknown
cause. Preserve these allocations and historical receipts. Fresh MCP execution
on the repaired source has not run.

The [qualification record](../designs/prime-native/NO_FORK.md#provider-probe-ownership-and-completion) owns the basic-response
observations and their limits, including the later direct Prime CLI response.
That response does not qualify the Omnigent bridge or lifecycle.

For a failed reply, inspect the current `<operation>-native-failure.json`.
The separate predicates receipt marks stale state as `previous_poll`. Missing,
null, and empty-string errors are absent; non-string errors are malformed and
present. The bounded failure observation contains no native error text, sample,
or hash. Its coarse class does not establish provider authentication or cause.

The failure receipt's nested `diagnostic` uses a closed finite schema. It
projects known structured diagnostic types, kinds, status and code buckets,
and matched same-operation ipython status and `isError`. It exports no raw
payload or unknown-value hash. Startup, protocol, and bootstrap flags remain
unavailable without a typed producer. Diagnostic categories do not attest the
backend cause, and previous-poll predicates cannot populate the current failure.

Qualified cleanup admits root, parent, and vnode witnesses before public DELETE
while the dedicated runner can still perform owned teardown. Public Stop
remains a separate control qualification and a cleanup attempt after DELETE
failure. A successful Stop fallback cannot clear the DELETE failure.
`retirement-attempts.json` records bounded allocation and session
bindings, reached phase, status, held identities, no-follow name state, validated
event flags, and allowlisted branches on every retirement-attempt exit.
Unknown observations remain explicit. The [verification workflow](../.agents/skills/verify-prime-native/SKILL.md#qualify-authenticated-finite-waits)
owns the retirement requirements during DELETE and after maintenance removal. There is no
manual external-root reclamation. Sticky errors, complete settlement, captures,
and owner exit before committed publication remain required. The cooperative
namespace precondition still leaves hostile same-UID final-syscall interference
BLOCKED. See the verification skill for the exact finite observation contract.

The owner captures the expected CLI log alias as metadata only, then removes
it only after writer settlement and matching identity checks. See the
[ownership and failure contract](../designs/prime-native/NO_FORK.md#provider-probe-ownership-and-completion)
for exact receipt requirements and the retained failed WAIT record.

Genuine native
`waiting`, status-only protection during complete output silence, one-hour and
indefinite waits, and the full S01-S04 guarantees remain unqualified.
The separate daemon probe is a qualification gate and currently exits nonzero
for the published runtime's known gaps.

To repeat the declared regular HTTP MCP restart case, run this command from the
checkout to qualify. Supply the admitted Prime 0.9.6 binary, the kernel
environment's `bin/python` entry without resolving its symlink, saved xAI
subscription OAuth credentials, and a private evidence directory.
Admit `--cooperative-cleanup` only under the controlled namespace precondition above.

```sh
PYTHONPATH="$PWD" uv run --no-sync python .agents/skills/verify-prime-native/scripts/mcp_probe.py --prime-path /absolute/path/to/prime-agent --kernel-python /absolute/path/to/kernel-venv/bin/python --auth-source /absolute/path/to/auth.json --evidence-parent /absolute/path/to/private-evidence --cooperative-cleanup
```

Follow the printed receipt path directly into the fresh `provider-mcp-*`
directory. Require exit zero and all eight required claims `VERIFIED` in
`result.json`. Require `completion.json` with `passed: true` and compare its
`result_sha256` and `manifest_sha256` with the retained files. Missing completion,
false completion, or either mismatched hash fails. The result's provisional
`qualified` value and raw `passed` fields are not success authority.
Require verified cleanup with no errors, survivors,
private sockets, credential copies, or forced fallback. A nonzero exit, missing
claim, or non-`VERIFIED` claim fails qualification. Require fixture closure for
both generations, runtime settlement in the same evidence directory, and
allocation-matched removal and closed-owner receipts. A failed fixture receipt retains scratch and blocks success.
Owner exit precedes completion publication. Inspect the completed native
outage error before generation 2 starts, the new server PID and nonce, matching
native, provider, and fixture results, and the same root and kernel process
after recovery, with the seeded token, a count advancing from 41 to 42, and an
open socket. Those fields do not prove object or socket identity. Follow the [MCP verification workflow](../.agents/skills/verify-prime-native/SKILL.md#qualify-regular-http-mcp-reconnect)
for the exact sequence and evidence files. This case does not qualify MRTR,
stdio, all servers, or all providers.
