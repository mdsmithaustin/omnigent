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

## Disposable daemon qualification

`scripts/daemon_probe.py` uses the real public supervisor socket and native terminal alongside an isolated model fixture. It exercises actual Python execution, command deduplication, a lost reply, supervisor adoption, replacement, replay classification, worker recovery, and scoped shutdown. It never changes Prime's source or the production adapter transport.

Supply an absolute path to a Python environment containing the published `prime-agent-runtime` and its dependencies. Pass the environment's `bin/python` entry directly. Resolving that symlink to the host interpreter can lose the installed environment.

```sh
uv run --no-sync python .agents/skills/verify-prime-native/scripts/daemon_probe.py --prime-path /tmp/omnigent-prime-runtime/application/prime-agent --kernel-python /absolute/path/to/kernel-venv/bin/python
```

Require exit zero and every required scenario to say `VERIFIED` before admitting a daemon transport. Any nonzero exit, `NOT VERIFIED`, or `INCONCLUSIVE` result blocks admission. The tested unmodified Prime 0.9.6 artifact reports eight verified scenarios and three unverified scenarios, and exits 1. Replay classification and the tested worker recovery failed. Keep the extension transport until a published artifact passes the gate.

Run the same command with `--expected-tool WRONG`. Require exit 1 and a literal SEED tool-output mismatch in `failure.json`. Run it again with `--expected WRONG`. Require exit 1 and the completed duplicate command's literal side-effect mismatch. A different failure does not qualify either negative control.

The probe prints a `/tmp/prime-daemon-proof-*` evidence directory. Inspect `artifact.json`, `outcomes.json`, the raw wire records, file side effects, `owned/cleanup.json`, and the final hash inventory in `manifest.json`. OS PID and process start-time observations establish the tested local lifetime; they are not a portable public worker identity. The unrelated isolated daemon must remain responsive until its separate cleanup.
