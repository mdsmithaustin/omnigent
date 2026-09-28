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

Prime is a fork of Pi that now develops independently, as described in
[Prime's README](https://github.com/PrimeIntellect-ai/prime-agent/blob/main/packages/coding-agent/README.md).
The adapters share
the extension inbox, message conversion, and tool relay transport. Prime has
its own launch, version check, session resume, and daemon shutdown code.
Pi's `--session` and `--list-models` are not Prime arguments. Use
`prime-agent model list` for Prime's credential-filtered model catalog.

This adapter does not qualify the broader durable controller and root
recovery contract in [the integration design](../designs/prime-native/CONTRACT.md).
It does not provide a Prime-side bridge or Agent Profile Kit integration.
Root tool policy hooks do not establish confinement for arbitrary Python
execution or recursive descendants.

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
