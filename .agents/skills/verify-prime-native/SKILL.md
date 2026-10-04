---
name: verify-prime-native
description: Verify Omnigent's prime-native adapter through its real CLI, Prime terminal, and session HTTP API. Use when changing Prime launch, Pi bridge reuse, models, controls, resources, permissions, or cleanup.
---

# Verify Prime Native

Use this skill for the Omnigent adapter only. Read [the feature map](features/README.md) before choosing a scenario. The disposable daemon probe qualifies a proposed transport separately from the working extension adapter. Agent Profile Kit is outside this skill's scope. The [no-fork design](../../../designs/prime-native/NO_FORK.md) records both the proposal and its failed qualification gate.

## Launch

Run from the repository root. Install the documented development dependencies with `uv sync --group dev`. A nonzero exit means setup failed.

Run the executable helper with a real Prime binary.

```sh
.agents/skills/verify-prime-native/scripts/verify.py --prime-path /tmp/omnigent-prime-runtime/application/prime-agent
```

The helper starts a local Omnigent API server and drives `omnigent prime-native` in a PTY. It uses a local OpenAI-compatible model endpoint for deterministic replies. That endpoint replaces only the external model service. Prime, its extension, Omnigent, the host, and the runner remain real processes.

Readiness requires `/health` to answer and the native CLI to print the created conversation URL. The completed message proves that Prime loaded the extension and reached the model endpoint. The helper bounds waits by observed state. It tears down the processes it creates in `finally`.

The default binary path is `OMNIGENT_PRIME_PATH`, then `prime-agent` on PATH. The helper prints its evidence directory. A nonzero exit or any `FAIL` record means the scenario failed. A missing native command is a failed prerequisite.

## Doctor

Run this read-only inspection before driving an existing instance or after an unexpected result.

```sh
.agents/skills/verify-prime-native/scripts/verify.py --prime-path /tmp/omnigent-prime-runtime/application/prime-agent --doctor
```

Require the checkout path, Prime version, native CLI help, and model command support. The helper checks the actual binary. A nonzero exit means the instance is not ready for the mapped drive. Doctor success proves prerequisites only.

## Drive

The helper creates a session with `omnigent prime-native` and supplies its owned loopback server through `--server`. It obtains the conversation ID from the CLI's `Web UI` line. It submits a message through the session events API, then types a distinct message into the Prime terminal. It reads the resulting items through the session items API. `launch.json`, `session.json`, and the action files preserve the exact command, URL, and conversation ID.

For a manual session, type a prompt in the attached Prime terminal. Then submit a different prompt through the Omnigent conversation. Require both user messages and their assistant replies in the transcript. Use the exact server URL printed by your instance.

The helper detaches its owned tmux attachment and runs the public resume command. It requires the same Omnigent conversation, native Prime session ID, and live Prime process IDs. This proves reattachment to the living worker, not kernel persistence after a restart.

Read the matching feature file for model, permission, resource, and lifecycle checks. Record each entry point separately. An API reply does not establish terminal input, and a successful enqueue does not establish model completion.

## Evidence

Evidence lives under a fresh directory in `/tmp/prime-native-verify-*`. It survives cleanup. Capture the command, binary and checkout revisions, terminal transcript, submitted HTTP action, resulting session items, and cleanup outcome.

Exercise public CLI and HTTP routes. Do not replace the extension, invoke internal state setters, or use test-only routes. Compare assistant output with a literal expected reply. Run a wrong-result control when changing the verification helper.

For mutations, capture the action and independently inspect the resulting file or resource. A permission proof must show a denied operation and an absent marker file. Mocking the execution gate cannot prove enforcement.

The local model fixture proves transport, not model reasoning or real-provider authentication. Record those as gaps until a provider-backed run exists. The default helper does not execute tools. Use the adapter probe below for the supported controls and resources. Durable recovery requires the separate daemon gate and remains unqualified.

## Cleanup

Stop only the Prime daemon and Omnigent processes created by this run. Prime workers can survive TUI detach. Killing the PTY alone is insufficient. Use the run's isolated Prime configuration and daemon endpoint for shutdown.

The helper deletes its owned scratch session through the public API while the server is alive and requires Prime process absence. It then stops its server and host processes and removes scratch state. It retains its evidence directory. Require the final evidence manifest to exist and report whether any owned process remains. Never kill by executable name or shut down an unrelated Prime instance.

## Helpers

`scripts/verify.py` is executable and uses `uv run --no-sync python`. Its default invocation runs launch, doctor, the HTTP and terminal message scenarios, and cleanup. `--doctor` performs prerequisites only. `--expected WRONG` supplies a negative control and must exit nonzero when the fixture replies `PRIME_NATIVE_PONG`.

`--inspect-seconds 120` retains the live session for two minutes after the assertions, then runs cleanup. The helper prints the exact conversation URL. The API server skips the bundled web UI. To inspect the conversation in a browser, run the repository's Vite development server with `OMNIGENT_URL` set to that API server's origin. Evidence and assertions remain the same.

## Full adapter probe

`scripts/adapter_probe.py` drives the supported extension adapter through the real CLI, public HTTP routes, and Prime terminal. It executes a declared MCP tool and a bundled Python skill resource, changes live settings, compacts history, interrupts actual work, checks the RLM root boundary, and reattaches to a living Python kernel. It also creates a custom Prime agent without a wrapper label and checks its observed settings and catalog. The fixture replaces only the remote model service.

The probe admits the pinned macOS arm64 Prime 0.9.6 executable with SHA256 `27bfb28d75d3f0b1fe5674b042a5127d20cbb560d00c44b42aaa905d5600f876`. Other platform artifacts require their own admission and runtime proof. Install the development dependencies before running it. Supply a Python 3.11 or later environment with the published `prime-agent-runtime` package and its dependencies. Prime's managed environment is normally `~/.prime/agent/kernel-venv/bin/python`. Pass that environment's entry directly without resolving its symlink.

Run from the checkout you intend to verify. The explicit `PYTHONPATH` selects that checkout even when its development environment has an editable install elsewhere.

```sh
PYTHONPATH="$PWD" uv run --no-sync python .agents/skills/verify-prime-native/scripts/adapter_probe.py --prime-path /absolute/path/to/prime-agent --kernel-python /absolute/path/to/kernel-venv/bin/python
```

Require exit zero, `VERIFIED final`, every required scenario marked `VERIFIED`, and an empty `fatal` list in `outcomes.json`. Inspect `artifact.json` for imported source paths and the exact runtime. Each scenario records its public actions and literal observations. Inspect those records, the terminal transcripts, model requests, tool results, native compaction entry, child side effect, `cleanup.json`, and `final-report.md`. Require no owned survivors, forced cleanup, or cleanup errors.

`reattach.json` keeps the run-wide process census under `raw` and the requested conversation's private root under `selected`. Require the selected before and after records to match, including the binding, native session ID, and TUI, supervisor, worker, and kernel process identities. Unrelated Prime helper churn in `raw` does not change this result. `cleanup.json` records the exact owned host registry and `omnigent host stop --server <owned URL> --daemon-only` invocation. Require successful session DELETE statuses, a successful qualified host stop, `forced_native_fallback: false`, an empty `final_owned_census`, no cleanup errors, and copied owned logs for diagnosis. The final census rechecks identities seen at launch as well as a fresh exact-root scan, so an initially observed process that becomes unlisted still blocks a clean result while alive.

`source-before.json` and `source-after.json` independently inventory tracked and nonignored untracked regular files under `omnigent`, the probe drivers, and the environment package files. `source-integrity.json` must contain empty added, removed, and changed lists. A source drift, missing import origin, or finalization failure prevents `VERIFIED` even when every behavioral scenario passed. `launch-witnesses.json` records the actual child commands, working directories, checkout-selecting environment, and PIDs. Those records are separate from the probe process's import witnesses. `deployed-extension-identities.json` compares the live extension bytes with the packaged source. Require each recorded identity to match.

The final `manifest.json` hashes the retained evidence. `--production-ready` records optional orchestration input. It does not replace these source checks or qualify the run.

`kernel-identity.json` must come from code executed by the real `ipython` tool. Require its nonce, interpreter entry, environment prefix, loaded `rlm` source, and source hash to match the supplied kernel environment. Its PID must identify an owned `rlm.repl` process launched with that interpreter entry. A probe subprocess importing that environment cannot prove which interpreter the native worker used. A missing or mismatched kernel witness fails qualification.

Run each negative control separately by adding one of these options to the same command:

- `--expected-tool WRONG` must exit 1 at the literal Python memory-read mismatch.
- `--expected-mcp WRONG` must exit 1 at the literal declared MCP-result mismatch.
- `--expected-side-effect present` must exit 1 because the denied write's marker is absent.

Require the named mismatch in the failed scenario and zero owned survivors in each run. An earlier failure or a missing tool does not qualify the control. The default probe stops at the intended mismatch and still performs cleanup.

The probe qualifies root relayed tool denial. It does not prove confinement of arbitrary Python or descendants. Fixture model choices do not prove commercial provider authentication. Durable recovery, global control exclusivity, arbitrary dialogs, and whole-descendant settlement remain outside this probe's passing claim.

## Qualify authenticated finite waits

`scripts/provider_probe.py` uses configured xAI credentials and
`xai/grok-4.7`. It creates fresh private runtimes and copies credentials into
owned files. It preserves
the original auth file and checks unrelated Prime process identities. It does
not inspect pending input in unrelated terminals.

The probe supervises a public foreground host with its private clock settings.
It requires the native CLI to reuse that exact host. Automatic host-daemon
creation with `--server` filters the pane timeout before runner startup. The
probe still requires the actual runner environment and reaper startup log to
match each clock profile.

Supply the admitted full Prime 0.9.6 macOS arm64 bundle, the managed kernel's
`bin/python` entry, and your xAI auth file. Replace the absolute paths below.
Install the documented optional runtime dependencies first. Use
`--cooperative-cleanup` only in a controlled namespace with no hostile concurrent
namespace writer and with every recorded owned writer settled before removal.
The acknowledgement admits this precondition. Permissions and watches do not
exclude hostile same-UID writers, including the final check/syscall window.
That threat remains BLOCKED. If you cannot admit the precondition, do not run
this cleanup qualification.

```sh
uv sync --locked --extra all --group dev
PYTHONPATH="$PWD" uv run --no-sync python .agents/skills/verify-prime-native/scripts/provider_probe.py --prime-path /absolute/path/to/prime-agent --kernel-python /absolute/path/to/kernel-venv/bin/python --auth-source /absolute/path/to/auth.json --evidence-parent /tmp --case waits --cooperative-cleanup
```

A nonzero setup exit means prerequisites failed. Require probe exit zero,
every required normal-arm claim `VERIFIED`, a verified wrong-result control,
matching deployment hashes, and clean cleanup in every child receipt. An
absent required claim, cleanup error, forced fallback, or source drift fails
the result. Read the printed aggregate manifest and its hashed child receipts.

HTTP 202 proves Omnigent admission only. Require a separate native queue receipt
for the exact owned prompt while the tool is running. Match the worker, binding,
native session, subscription generation, and original attachment baseline. Its
original receive time must precede the actual tool end, with an absent adjacent
end marker. After completion, require the tool result, exact queued user entry,
and successful native final in that order. The public conversation must contain
the exact fresh public user prompt, matching literal reply, and complete ordered
tool evidence.

Prime writes the queued user entry after the tool finishes. Earlier runs failed
the journal-before-end requirement; preserve those FAILED receipts. The queue
requirement applies to fresh runs and does not reinterpret historical failures.

Prime assigns event numbers before filtering delivery to each subscriber. A gap
alone does not prove dropped input. Require a fresh native steering receipt in
the same generation above the matched attachment baseline. This observer does
not qualify exhaustive stream delivery or replay.

`--case runner` tests a 30-second runner watchdog with pane reaping disabled.
Its idle control must expire. Its actual 60-second Python tool must survive
more than 37 quiet seconds. `--case pane` disables the runner watchdog and
uses a 30-second pane timeout with the production 60-second scan cadence.
Its idle control must actually reap the native pane. Its 300-second Python
tool must survive at least 280 measured quiet seconds. Quiet observation sends
no routed requests or terminal input. Each positive arm then requires HTTP
steering, terminal reattachment, and the same living kernel value and socket.

The default `waits` case runs both pairs and a fresh wrong-memory arm. For a
separate negative control, replace `--case waits` with
`--case runner --expected-memory WRONG`. Require exit 1 at
`memory_read_mismatch` after the normal tool
completed, with clean cleanup. An earlier failure does not qualify the control.
`--case selector` checks observed native metadata and inferred eligibility only.
It cannot replace the pane idle control's actual reap evidence.

These finite tool waits observe native `running`. Genuine native `waiting`,
default one-hour thresholds, and indefinite waits remain unqualified. Pane
survival with fresh TUI output does not establish protection from native status
alone. Those limits have separate `NOT VERIFIED` observations and do not become
required passing claims. MCP reconnect is a separate qualification.

The provider probe uses one immutable reply baseline and fixed operation
deadline for seed, steering, terminal, and memory checks. Require exactly one
fresh public `message` with role `user` after that baseline and before the
selected public final. Concatenate its `input_text` blocks without trimming or
normalizing whitespace. The text must equal the owned prompt. Reject a baseline
match, duplicate match, match after the final, or another user message between
the match and final. Earlier different wait input is allowed. Missing or wrong
text remains pending until the fixed deadline. Malformed content rejects.

Inspect the captured match counts, IDs, absolute positions, freshness, prompt
equality, and final position. Keep `native_user_id`, `public_user_id`,
`native_reply_id`, and `public_reply_id` separate. User response IDs need not
equal final response IDs. Require the existing ordered native tool and public
projection checks too. An idle snapshot or completed carrier cannot prove the
reply. An interrupted final or incomplete public page fails the operation.
Each HTTP request, response body, and close has a bounded budget.

Inspect `cleanup.json` in every child. Require empty errors, no exact owned
survivors or private sockets, `forced_native_fallback: false`,
`credential_copies_absent: true`, `runtime_removed: true`, and unchanged source
and deployment evidence. Also require `runtime-settlement.json`,
`runtime-removal.json`, and `runtime-owner.json` for the same allocation.
The owner receipt must record removal, `closed: true`, and no errors. A cleanup
receipt alone cannot establish completed owner closure.

Scratch belongs to an external owner allocated before fallible run construction.
Its guard covers context entry and partial construction. Unknown construction
or settlement cannot invent an empty process census. Before recursive removal,
require settled processes, readers, handles, sockets, sanitized captures, source
checks, and explicit allocation-bound fixture settlement. Provider-only cases
supply `no_fixture`. A fixture caller must supply `settled_fixture` with retained
closure evidence, or `failed_fixture` with errors. Omitting settlement is invalid.
External compact roots require their own retirement receipts.

When session DELETE removes the owned Prime directory, `retired_owned_trees`
binds its original root and surviving parent identities to the exact session,
DELETE step, successful HTTP status, and Darwin deletion event. The outer scratch
removal instead records its checked descriptor-relative `rmdir` and held original
vnode proof. HTTP success or path absence alone proves neither removal.
See the [ownership and completion contract](../../../designs/prime-native/NO_FORK.md#provider-probe-ownership-and-completion)
for the Darwin predicate and its namespace-removal limits.

Keep failures and their receipts. Errors remain sticky even if a later check
succeeds. A failure before removal retains uncertain scratch. A failure after
removal retains removed-object provenance and blocks success. Retired credential
retries inspect only known witnessed targets. They do not rediscover or delete
recreated names, and they reject new descendants. Receipt or pin/watch closure
failure cannot become successful cleanup.

Require each child's `completion.json`. Its `result_sha256` and
`manifest_sha256` must match that child's retained files, and `passed` must agree
with its qualified observations. `qualified` in `result.json` or `manifest.json`
is provisional. Owner exit, receipts, and pin/watch closure precede publication.
Publication prepares, hashes, flushes, and closes artifacts before the final
completion rename. Missing completion or mismatched hashes blocks child, CLI,
and aggregate success. The expected wrong-memory child must have a committed
`passed: false` result at the named mismatch with verified cleanup. It is not a
successful normal case.

The retained `provider-waits-m4g99h4o` run passed all five cases under its original
checks on the admitted Prime artifact with configured Grok 4.7. Its driver SHA256
is `a2584ce9b149568e791f49ace5b7be7c332493770ad771cde18cb0e20d8335bb`.
That observer did not require the exact fresh public user prompt. Its cleanup
could report success while outer scratch remained, and its result publication
could precede mandatory finalization. Preserve the receipt as evidence for those
bytes, not proof of the repaired contract. The earlier
`provider-waits-ug3ppd91` run remains FAILED at pane idle cleanup.

The later parent run `provider-waits-gkknmv6b` passed all five cases at revision
`afe870ce93c9df8bccfa88be13bb54c39d64e5ae`. Its independent audit passed 2,616
assertions over 309 child artifacts and 897 selected source files, with all 72
recorded owned process identities absent. It used the same historical provider
driver hash and shares the prompt, outer-scratch, and publication limits above.
Earlier failed runs remain FAILED. These finite-wait receipts precede the MCP and extension repair. No finite-wait
rerun on those changed bytes is claimed. Changes to the probe, imported
application source, or admitted runtime require fresh qualification.

The prompt and ownership repair at `ecf1bb4824a28913377109cefc3107f018fdb21a`
has an independent scoped source verdict of PASS+NOTES and synthetic controls.
Its provider SHA256 is
`e6615c5e9bd099e2d0e8fba19d291f58f435b4eb70a01fbd3c3d1223a1ff799a`.
PR6's MCP caller, fixture settlement, publisher, readers, and owner pin are
migrated at `ad945096ed6da2c1ffe866a33841188f77770b4f` with an independent
scoped source verdict of PASS+NOTES. Its MCP SHA256 is
`b03472857f5cf666e026093ee664a6e58cff0858603e0e05a04bb1c24f7b7832`.
Fresh live WAIT and MCP qualification on final integrated bytes remains pending.
Changes to the probe, imported application source, or admitted runtime require
fresh qualification. Full S01-S04 and the
31-requirement program remain incomplete.

## Qualify regular HTTP MCP reconnect

`scripts/mcp_probe.py` drives one declared regular HTTP MCP tool through the
real Prime terminal, Omnigent session proxy, and configured xAI Grok 4.7 provider.
It owns a disposable two-generation MCP fixture. It does not replace the model
provider or call the fixture directly to simulate a native tool invocation.

Run from the checkout to qualify with its documented optional runtime dependencies
installed. Supply the admitted full Prime 0.9.6 macOS arm64 bundle and a kernel
environment containing the published Python runtime. Pass the environment's
`bin/python` entry without resolving its symlink. Supply configured xAI
credentials through `--auth-source` and keep evidence private. Do not copy
credential contents or raw provider payloads into public reports.
Use `--cooperative-cleanup` only when you can admit the controlled namespace
precondition in the finite-wait workflow above. Omission exits 2 before allocation.
The flag does not exclude hostile same-UID writers in the final check/syscall window.

```sh
PYTHONPATH="$PWD" uv run --no-sync python .agents/skills/verify-prime-native/scripts/mcp_probe.py --prime-path /absolute/path/to/prime-agent --kernel-python /absolute/path/to/kernel-venv/bin/python --auth-source /absolute/path/to/auth.json --evidence-parent /absolute/path/to/private-evidence --cooperative-cleanup
```

Use the printed receipt path directly. Fresh runs place `result.json`,
`manifest.json`, and `completion.json` in one `provider-mcp-*` directory under
`--evidence-parent`, without the historical `runner-mcp-*` child directory.
Require exit zero and `completion.json` with `passed: true`. Compare its
`result_sha256` and `manifest_sha256` with SHA256 of the retained result and
manifest bytes. Require no failure or finalization errors and all eight required
claims `VERIFIED` in `result.json`. The required claims are `actual_provider`, `declared_tool`, `generation_1_success`,
`completed_outage_error`, `generation_2_independent_success`,
`selected_root_continuity`, `kernel_and_memory_continuity`, and `owned_cleanup`.
A missing claim, non-`VERIFIED` claim, or nonzero exit fails qualification.
Require empty cleanup errors, no owned survivors, private sockets, or credential
copies, and `forced_native_fallback: false`. Also require
`credential_copies_absent: true` and `runtime_removed: true` in `cleanup.json`.
Result and manifest `qualified` values are provisional. No raw `passed` field
in a result or fixture receipt authorizes success. Missing completion, false
completion, or either mismatched hash fails qualification.

Inspect the retained sequence in order:

1. Match the deployed declared schema, generation-1 fixture ledger, native tool
   result, and provider continuation to the exact expected literal.
2. Confirm the fixture stopped and the endpoint refused connections. Require a
   completed native tool result with `isError: true` and the exact prefix
   `Request failed on the runner; see the runner log for details: `.
   Require no fixture invocation for the outage call.
3. Require generation 2 to start only after that native error completed. Match
   its unchanged endpoint and schema, fresh startup nonce, PID, and start time.
4. Match a separate declared call to the generation-2 ledger, native result,
   and provider continuation. Reusing the generation-1 literal fails the case.
5. Compare selected root and kernel identities before and after recovery.
   Require the existing memory object to advance from 41 to 42 with its original
   token and open socket. Recreating the value or socket fails continuity.
6. Inspect `fixture-cleanup.json`. Require both generation records with exited
   processes, closed outputs, `endpoint_refused: true`, and empty errors.
7. Match the fixture allocation ID to `runtime-removal.json` and
   `runtime-owner.json`. Require removal, `closed: true`, and no owner errors.
   Inspect `runtime-settlement.json` in that same evidence directory for clean
   runtime settlement. Require unchanged source and deployed-extension evidence.

Use `declared-schema.json`, `before-witness.json`, `outage-witness.json`,
`outage-start.json`, `after-witness.json`, the generation startup and call
records, `continuity.json`, kernel records, `fixture-cleanup.json`, and
`cleanup.json`. `artifact.json`, `mcp-artifact.json`, the source inventories,
and `manifest.json` bind the source and runtime used. `completion.json` binds
that result and manifest to the completed owner lifetime. The selector
observation is source-inferred eligibility, not a live registry census.

The fixture supplies settlement only after every real generation is reaped,
its PID/start identity is absent, all output handles are closed, and the endpoint
refuses connections. Child output goes directly to files. The fixture owns no
parent reader or capture thread. Exited child processes settle their internal
threads and descriptors. Unexpected process pipes reject settlement.
`fixture-cleanup.json` must be written before the allocation-bound
`settled_fixture` value can authorize runtime removal. Startup, stop, close,
endpoint, or receipt errors stay sticky and yield `failed_fixture`. A later
successful check cannot erase an earlier failure. MCP rejects `no_fixture`,
missing settlement, or settlement from another allocation. Failed or unknown
settlement retains scratch and fails qualification, even when processes exited.

The scratch owner is allocated before fallible MCP construction. Owner entry,
partial construction, scenario failure, and owner exit remain guarded. The
scenario returns an unpublished draft. Credential finalization, owner closure,
and required receipts finish before publication commits `completion.json`.
A later owner failure preserves the first scenario failure and records the
finalization error. Failed committed cases require matching completion hashes
with `passed: false`. Incomplete publication cannot authorize a committed case.
Preserve retained scratch and failure evidence for diagnosis. Do not reinterpret
those receipts as successful cleanup.

The retained `provider-mcp-nhuedn43/runner-mcp-gimauabc` run reports all eight
required claims verified on the repaired working tree above `afe870ce93c9df8bccfa88be13bb54c39d64e5ae`.
Its hashes identify the executed bytes, not a subsequently created commit.
The three earlier actual MCP receipts remain FAILED. This historical case
qualifies regular HTTP native error transport and recovery on those bytes only.
It predates allocation-bound settlement and completion publication. It cannot
qualify the migrated owner contract. The exact provider and MCP hashes above
have completed independent scoped source review with PASS+NOTES. Fresh live
WAIT and MCP qualification on final integrated bytes remains pending.
The historical case does not qualify real MRTR callbacks or approval-state recovery, stdio, all servers or providers, or
the whole N19 requirement. See [the acceptance record](../../../designs/prime-native/NO_FORK.md#regular-http-mcp-reconnect-qualification)
for the remaining matrix.

## Disposable daemon qualification

`scripts/daemon_probe.py` uses the real public supervisor socket and native terminal alongside an isolated model fixture. It exercises actual Python execution, command deduplication, a lost reply, supervisor adoption, replacement, replay classification, worker recovery, and scoped shutdown. It never changes Prime's source or the production adapter transport.

Supply an absolute path to a Python environment containing the published `prime-agent-runtime` and its dependencies. Pass the environment's `bin/python` entry directly. Resolving that symlink to the host interpreter can lose the installed environment.

```sh
uv run --no-sync python .agents/skills/verify-prime-native/scripts/daemon_probe.py --prime-path /tmp/omnigent-prime-runtime/application/prime-agent --kernel-python /absolute/path/to/kernel-venv/bin/python
```

Require exit zero and every required scenario to say `VERIFIED` before admitting a daemon transport. Any nonzero exit, `NOT VERIFIED`, or `INCONCLUSIVE` result blocks admission. The tested unmodified Prime 0.9.6 artifact reports eight verified scenarios and three unverified scenarios, and exits 1. Replay classification and the tested worker recovery failed. Keep the extension transport until a published artifact passes the gate.

Run the same command with `--expected-tool WRONG`. Require exit 1 and a literal SEED tool-output mismatch in `failure.json`. Run it again with `--expected WRONG`. Require exit 1 and the completed duplicate command's literal side-effect mismatch. A different failure does not qualify either negative control.

The probe prints a `/tmp/prime-daemon-proof-*` evidence directory. Inspect `artifact.json`, `outcomes.json`, the raw wire records, file side effects, `owned/cleanup.json`, and the final hash inventory in `manifest.json`. OS PID and process start-time observations establish the tested local lifetime; they are not a portable public worker identity. The unrelated isolated daemon must remain responsive until its separate cleanup.
