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
- Bundle a declared skill with the chosen agent. Call `load_skill` and require its distinctive text and resource advertisement in the delivered result.
- Call `read_skill_file` with the declared `skill_name` and resource `path`. Execute the delivered Python resource in the real `ipython` kernel. Require its nonce-bearing result and independent file side effect. Reading the original fixture directly cannot prove resource delivery.
- Call a harmless MCP tool and compare its result with a literal expected value. A wrong-result control must fail.
- Deny a marker write, then attempt it through both interfaces. Require the denial event and absence of the marker. Record descendant and native-client bypass coverage separately.

## Gotchas

Pi tool policy hooks do not establish confinement for arbitrary Python or recursive descendants. The adapter probe denies the root's relayed `sys_os_write` call from HTTP and terminal input. That result proves only those attempted operations. Full shared-client execution policy remains unqualified. The [no-fork design](../../../../designs/prime-native/NO_FORK.md) records the remaining public-hook and external containment proofs.

These scenarios remain gaps in the default helper, which uses `--no-tools`. The [adapter probe](../SKILL.md#full-adapter-probe) exercises the declared MCP tool, bundled skill resources, canonical instructions, and root denial through the production adapter.
