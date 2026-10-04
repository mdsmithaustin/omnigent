# Instructions, skills, and tools

Users expect Prime to retain its base behavior while receiving the chosen agent instructions and tools.

## Sub-features

- Appended instructions retain Prime's base system instructions.
- A selected text skill produces a distinctive observable result.
- A Python-backed skill executes in the real kernel.
- An MCP tool returns its literal expected result.
- A denied tool call creates no marker file.

## How to get to it (user POV)

Start a Prime Native session with the intended agent specification, skills, and policy. Ask Prime to use the selected skill or tool through the terminal or conversation.

## Driving it with the PTY and HTTP API

Preconditions are a fixture workspace, installed skill resources, and the declared policy.

- Append a unique instruction marker. Ask Prime to report it. Inspect the model request to confirm the marker and Prime's base prompt are both present.
- Supply a real skill file to Prime through the pass-through `--skill` option. Record its absolute path and content hash, ask for its distinctive behavior, and inspect the result.
- Load a Python-backed skill. Require a real kernel result and its expected file side effect. A plain model reply is insufficient.
- Call a harmless MCP tool and compare its result with a literal expected value. A wrong-result control must fail.
- Deny a marker write, then attempt it through both interfaces. Require the denial event and absence of the marker. Record descendant and native-client bypass coverage separately.

## Gotchas

Pi tool policy hooks do not establish confinement for arbitrary Python or recursive descendants. A denied root `ipython` call proves only that attempted root operation. Full shared-client execution policy requires the Prime-side bridge work excluded from this task.

These scenarios remain gaps in the default helper. It uses `--no-tools` and does not prove tool execution, skills, permissions, or kernel behavior.
