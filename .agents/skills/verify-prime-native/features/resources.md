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

## Repeat the local-model MCP reconnect case

Run the existing [adapter command](../SKILL.md#full-adapter-probe) with the admitted
Prime binary and kernel entry. Require exit zero, all ten scenarios `VERIFIED`,
an empty `fatal` list, and clean cleanup. In the `mcp_tool` records, require a
real first success, reaped generation 1 and connection refusal, a completed
native outage error, and then a separate success from generation 2 at the same
endpoint. Each successful before and after call must have one row in its generation
ledger. Match its complete text and call IDs across native, public, local Chat
Completions, and server records. For the outage, require matching native, public,
and Chat Completions error text and call IDs, with native `isError: true`.
Require no outage invocation or outage row in either generation ledger.
Public outputs do not require an error flag.

Require the same schema and selected root across all phases. Inspect
`mcp-continuity.json` for the original token, object, socket, descriptor, and
kernel while the count advances from 42 to 43. No missing object is reseeded.
Run the same command separately with `--expected-mcp WRONG`. Require exit 1
specifically at `mcp tool result differs` after the first real call, with clean
settlement of every allocated generation and the model thread.

The phase catalog records distinguish actual listing and model advertisement
from readiness. Explicit unavailable and restored readiness remain
`NOTOBSERVED`. Original N19 remains partial. The fixture uses stateless HTTP.
It does not qualify stateful sessions, stdio, MRTR or approval recovery,
commercial provider authentication, or full31. Outer runtime scratch and evidence
remain retained at the reported paths.

## Repeat the regular HTTP MCP reconnect case

Run from the checkout to qualify with the documented runtime dependencies
installed. Use the admitted Prime 0.9.6 macOS arm64 binary, the kernel
`bin/python` entry without resolving its symlink, configured xAI credentials,
and a private evidence directory. Admit `--cooperative-cleanup` only in a
controlled namespace with settled owned writers and no hostile concurrent
namespace writer. The acknowledgement does not exclude hostile same-UID writers.

```sh
PYTHONPATH="$PWD" uv run --no-sync python .agents/skills/verify-prime-native/scripts/mcp_probe.py --prime-path /absolute/path/to/prime-agent --kernel-python /absolute/path/to/kernel-venv/bin/python --auth-source /absolute/path/to/auth.json --evidence-parent /absolute/path/to/private-evidence --cooperative-cleanup
```

Follow the printed `result.json` path in the fresh `provider-mcp-*` directory.
Require exit zero, all eight required claims `VERIFIED`, and `completion.json`
with `passed: true`. Its `result_sha256` and `manifest_sha256` must match the
retained result and manifest. A missing or false completion, mismatched hash,
missing claim, non-`VERIFIED` claim, or nonzero exit fails. The result's
`qualified` value and any raw `passed` field are not success authority.
Require verified cleanup with empty errors and no owned survivors, private
sockets, credential copies, or forced fallback. Require a written
`fixture-cleanup.json` for both exited generations, closed outputs, and a refused
endpoint. Match its allocation ID to the runtime removal and closed-owner
receipts. Inspect `runtime-settlement.json` in the same evidence directory.
Failed closure or receipt writing produces `failed_fixture`, retains scratch, and blocks success. Owner exit must
finish before completion publication. The workflow below lists the exact fields.

Follow [the MCP workflow](../SKILL.md#qualify-regular-http-mcp-reconnect) to compare
schema, fixture, native, and provider witnesses. Require the completed native
outage error before generation 2 starts, then a separate declared call with the
new server PID and nonce. Require the same selected root and kernel identity,
with the seeded token, the count advancing from 41 to 42, and an open socket.
These fields do not prove object or socket identity.
The retained `provider-mcp-nhuedn43/runner-mcp-gimauabc` run verified this bounded
case on its recorded bytes. Earlier actual MCP failures remain FAILED.
The owner-contract migration has completed independent scoped source review
with PASS+NOTES for the exact provider and MCP hashes recorded in the workflow.
Fresh final-byte WAIT and MCP receipts remain pending. Historical evidence does
not qualify the migrated helper.

Real MRTR callbacks and opaque approval retries across restart, stdio restart,
wider errors and cancellation timing, other server classes, and other providers
remain unqualified. See [MCP recovery and retries](../../../../docs/AGENT_YAML_SPEC.md#mcp-recovery-and-retries)
for the shared connection contract. The real HTTP proof does not turn controlled
Pi 0.84.2 source-semantic checks with `sdk_execution: false` into Pi CLI execution.

## Gotchas

Pi tool policy hooks do not establish confinement for arbitrary Python or recursive descendants. The adapter probe denies the root's relayed `sys_os_write` call from HTTP and terminal input. That result proves only those attempted operations. Full shared-client execution policy remains unqualified. The [no-fork design](../../../../designs/prime-native/NO_FORK.md) records the remaining public-hook and external containment proofs.

These scenarios remain gaps in the default helper, which uses `--no-tools`. The [adapter probe](../SKILL.md#full-adapter-probe) exercises the declared MCP tool, bundled skill resources, canonical instructions, and root denial through the production adapter.
