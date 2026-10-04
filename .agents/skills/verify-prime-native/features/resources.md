# Instructions, skills, and tools

Users expect Prime to retain its base behavior while receiving the chosen agent instructions and tools.

## Sub-features

- Appended instructions retain Prime's base system instructions.
- A selected text skill produces a distinctive observable result.
- A Python-backed skill executes in the real kernel.
- An MCP tool returns its literal expected result.
- A declared regular HTTP MCP tool reports a completed native outage error and
  succeeds on a later independent call after the fixture restarts.
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

## Repeat the regular HTTP MCP reconnect case

Run from the checkout to qualify with the documented runtime dependencies
installed. Use the admitted Prime 0.9.6 macOS arm64 binary, the kernel
`bin/python` entry without resolving its symlink, configured xAI credentials,
and a private evidence directory.

```sh
uv run --no-sync python .agents/skills/verify-prime-native/scripts/mcp_probe.py --prime-path /absolute/path/to/prime-agent --kernel-python /absolute/path/to/kernel-venv/bin/python --auth-source /absolute/path/to/auth.json --evidence-parent /absolute/path/to/private-evidence
```

Require exit zero, `passed: true`, and all eight required claims `VERIFIED` in
`result.json`. A missing claim, non-`VERIFIED` claim, or nonzero exit fails.
Require verified cleanup with empty errors and no owned survivors, private
sockets, credential copies, or forced fallback.

Follow [the MCP workflow](../SKILL.md#qualify-regular-http-mcp-reconnect) to compare
schema, fixture, native, and provider witnesses. Require the completed native
outage error before generation 2 starts, then a separate declared call with the
new server PID and nonce. Require the same selected root and kernel object,
with the count advancing from 41 to 42 and the original socket still open.
The retained `provider-mcp-nhuedn43/runner-mcp-gimauabc` run verified this bounded
case. Earlier actual MCP failures remain FAILED.

Real MRTR callbacks and opaque approval retries across restart, stdio restart,
wider errors and cancellation timing, other server classes, and other providers
remain unqualified. See [MCP recovery and retries](../../../../docs/AGENT_YAML_SPEC.md#mcp-recovery-and-retries)
for the shared connection contract. The real HTTP proof does not turn controlled
Pi 0.84.2 source-semantic checks with `sdk_execution: false` into Pi CLI execution.

## Gotchas

Pi tool policy hooks do not establish confinement for arbitrary Python or recursive descendants. The adapter probe denies the root's relayed `sys_os_write` call from HTTP and terminal input. That result proves only those attempted operations. Full shared-client execution policy remains unqualified. The [no-fork design](../../../../designs/prime-native/NO_FORK.md) records the remaining public-hook and external containment proofs.

These scenarios remain gaps in the default helper, which uses `--no-tools`. The [adapter probe](../SKILL.md#full-adapter-probe) exercises the declared MCP tool, bundled skill resources, canonical instructions, and root denial through the production adapter.
