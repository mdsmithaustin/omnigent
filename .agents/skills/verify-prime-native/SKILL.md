---
name: verify-prime-native
description: Verify Omnigent's prime-native adapter through its real CLI, Prime terminal, and session HTTP API. Use when changing Prime launch, Pi bridge reuse, model discovery, messages, permissions, or cleanup.
---

# Verify Prime Native

Use this skill for the Omnigent adapter only. Read [the feature map](features/README.md) before choosing a scenario. Prime-side bridge work and Agent Profile Kit are outside this skill's scope.

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

The local model fixture proves transport, not model reasoning or real-provider authentication. Record those as gaps until a provider-backed run exists. The full durable binding and controller contract in `designs/prime-native/CONTRACT.md` requires separate qualification.

## Cleanup

Stop only the Prime daemon and Omnigent processes created by this run. Prime workers can survive TUI detach. Killing the PTY alone is insufficient. Use the run's isolated Prime configuration and daemon endpoint for shutdown.

The helper deletes its owned scratch session through the public API while the server is alive and requires Prime process absence. It then stops its server and host processes and removes scratch state. It retains its evidence directory. Require the final evidence manifest to exist and report whether any owned process remains. Never kill by executable name or shut down an unrelated Prime instance.

## Helpers

`scripts/verify.py` is executable and uses `uv run --no-sync python`. Its default invocation runs launch, doctor, the HTTP and terminal message scenarios, and cleanup. `--doctor` performs prerequisites only. `--expected WRONG` supplies a negative control and must exit nonzero when the fixture replies `PRIME_NATIVE_PONG`.

`--inspect-seconds 120` retains the live session for two minutes after the assertions, then runs cleanup. The helper prints the exact conversation URL. The API server skips the bundled web UI. To inspect the conversation in a browser, run the repository's Vite development server with `OMNIGENT_URL` set to that API server's origin. Evidence and assertions remain the same.
