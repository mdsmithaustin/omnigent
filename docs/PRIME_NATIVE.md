# Prime Native

`omnigent prime-native` connects Prime Agent's terminal to an Omnigent
conversation. Messages sent from either interface appear in the Omnigent
transcript. The harness identifier is `prime-native` and the CLI-created agent
is `prime-native-ui`.

## Launch and reattach

Install Prime Agent **0.9.6** and `tmux`. This adapter requires that exact Prime
version on macOS and Linux. Configure Prime's login or `models.json` before
launching. Omnigent copies `auth.json`, `settings.json`, and `models.json` into
private conversation state without changing the source files.
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
private runtime's processes and sockets to disappear.

## Change settings and control a turn

The model picker uses Prime's available registry. Before launch, the host reads
`prime-agent model list` with Prime's credentials. A live session reports its
own registry through the extension. Model identifiers contain the exact
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
invent an observation or replay the control.

Each callback publication has a one-second deadline within the control's
remaining budget. A slower response can return 504 even if the server later
stores the observation. Inspect the displayed native state before another
control.

Send `{"type":"compact"}` or `{"type":"interrupt"}` to
`POST /v1/sessions/{id}/events` for the corresponding control. Compaction waits
for Prime's completion callback. Interrupt returns admission before the native
aborted result settles. Messages also receive HTTP 202 for admission. Delivery
to the extension inbox does not complete a native turn.

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

An agent's declared MCP tools use Omnigent's existing session proxy. Prime
advertises the discovered tools through the shared extension, and the relay
executes them. Discovery failures appear in the server or runner logs. The
native terminal still starts when an additive MCP server is unavailable.

Bundled skills use `load_skill` and `read_skill_file`. A Python resource delivered
by those tools can execute in Prime's native `ipython` kernel. Configure resources
in an [agent specification](AGENT_YAML_SPEC.md) and start a session with that
agent. The `omnigent prime-native` shortcut creates the `prime-native-ui` agent.
It does not select a custom agent bundle.

Omnigent evaluates relayed root tools against the agent's policy. That gate does
not confine arbitrary Python code or recursive descendants.

## State and compatibility

Conversation state lives under `$OMNIGENT_DATA_DIR/prime-native/`, or
`~/.omnigent/prime-native/` when the variable is unset. Each conversation has
its own Prime configuration, temporary directory, daemon socket, and saved
sessions. When that socket path would exceed the Unix limit, Omnigent uses a
private digest directory under `/tmp/ogp-<uid>/`. Stop retains saved
sessions; deleting the conversation removes its adapter state after shutdown.

Startup and host maintenance stop orphaned Prime runtimes whose recorded owner
is dead in the same process namespace and boot. A live terminal protects its
runtime even after its runner exits. Recovery retains saved sessions and private
configuration for resume. Unknown or legacy ownership records remain untouched.
Cleanup sends Prime's public shutdown command to the qualified private
supervisor socket. Omnigent clears transient ownership only after that socket
and its owned processes stop. If ownership or shutdown cannot be proved, it
retains the runtime for a later cleanup attempt. The public
`prime-agent shutdown` CLI holds a per-user admission lease. Omnigent uses
the private socket for session cleanup. Native Windows launch is rejected
because Prime's default named pipe is shared.

Prime is a fork of Pi that now develops independently, as described in
[Prime's README](https://github.com/PrimeIntellect-ai/prime-agent/blob/main/packages/coding-agent/README.md).
The adapters share
the extension inbox, message conversion, and tool relay transport. Prime has
its own launch, version check, session resume, and daemon shutdown code.
Pi's `--session` and `--list-models` are not Prime arguments. Use
`prime-agent model list` for Prime's credential-filtered model catalog.

Prime loads external extensions into recursive RLM children. Omnigent binds its
extension effects to the public session header's root depth. Children continue
executing in Prime. Their extension instances do not consume root controls or
publish their transcript as the Omnigent root conversation. Missing or invalid
depth is unknown and does not admit a root binding.

The published daemon failed the replay-cursor and tested worker-recovery gates.
The adapter therefore keeps the extension transport. Durable recovery across
missed events, global control exclusivity, completion-dependent declared children,
and whole-descendant settlement remain unqualified. Agent Profile Kit is outside
this implementation. Root policy hooks do not confine arbitrary Python code or
recursive descendants. [The acceptance matrix](../designs/prime-native/NO_FORK.md)
records each original requirement and its remaining evidence.

## Verify the adapter

The project skill drives a real Prime binary, Omnigent server, host, runner,
and terminal. A local model endpoint returns a fixed reply to prove message
transport without provider credentials.

```sh
uv sync --group dev
.agents/skills/verify-prime-native/scripts/verify.py --prime-path /tmp/omnigent-prime-runtime/application/prime-agent
```

Supply the actual path to your Prime 0.9.6 binary. Require exit code zero and
`result.json` with `PASS` in the printed evidence directory. Inspect
`terminal-items.json` for both submitted messages and both
`PRIME_NATIVE_PONG` replies. `cleanup.json` must report no owned survivors.
`reattach.json` records the same native identity and live process IDs across
detach and resume. The helper retains its evidence after removing scratch state.

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
The separate daemon probe is a qualification gate and currently exits nonzero
for the published runtime's known gaps.
