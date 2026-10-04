# Attach, resume, and stop

Users can detach their terminal and return to their Prime session. Explicit stop must end the work owned by that session.

## Sub-features

- Fresh launch creates one Prime Native conversation.
- Reattachment uses the existing terminal when it is alive.
- Detaching the tmux attachment leaves the required TUI and worker alive.
- Exiting the required TUI shuts down its private daemon and workers.
- Interrupt stops the active turn and permits a subsequent turn.
- Explicit stop tears down the owned daemon and workers.
- Startup and maintenance stop dead-owner runtimes while preserving saved sessions and private configuration.
- A live terminal protects its runtime after the launch owner exits.
- A worker restart must not claim that Python memory survived.

## How to get to it (user POV)

Launch `omnigent prime-native`. Close the attachment window, then run `omnigent prime-native --resume` and select that conversation. Use the session's stop control to end work.

## Driving it with the PTY and HTTP API

Preconditions are a session created by this run and its isolated Prime daemon endpoint.

- Capture the conversation and native session IDs after launch. Detach and resume. Require the same Omnigent conversation and native identity.
- Create a Python value, detach, and inspect it after reattachment. Require the literal value in the same living worker. Transcript persistence does not prove kernel persistence.
- Interrupt a bounded active turn through the Omnigent control. Require an interrupted outcome and a successful subsequent reply.
- Stop the owned session. Inspect its daemon status and owned process IDs. Require no surviving worker or kernel. Preserve evidence after removing scratch state.
- Stop while terminal creation is in progress. Require HTTP 503, retained launch ownership, and no idle status. Retry after launch settles and require owned process and socket absence.
- For orphan recovery, terminate only the isolated session's wrapper and terminal without normal shutdown. Run startup or host maintenance. Require no owned daemon or worker, retained saved sessions and configuration, and zero recoveries on a second sweep. Repeat with the terminal alive and require its runtime to survive.

## Gotchas

Prime itself persists workers after the TUI exits. The adapter explicitly shuts down its private daemon when the required TUI exits. Detaching the tmux attachment keeps that TUI alive.

The default helper detaches the owned tmux attachment, resumes the same conversation, and requires the same native session ID and owned Prime process IDs. It also checks session deletion and owned process absence. The full adapter probe retains a literal Python value across reattachment in the same living kernel. It observes separate native interrupted events for API abort and terminal Ctrl+C, then requires a distinct successful reply after each. Saved-history resume after a worker restart does not preserve that kernel. Stable root recovery and uncertain-command reconciliation require separate published-daemon conformance proof. An Omnigent lease cannot fence arbitrary native Prime clients. These limits are explicit in the [no-fork design](../../../../designs/prime-native/NO_FORK.md).

The full adapter probe requires the selected TUI, supervisor, worker, and kernel identities to match before and after reattachment. It retains other private-root processes as diagnostics. Its cleanup record includes the exact owned host stop, session DELETE statuses, retained logs, and final owned process census. The final census rechecks initially observed process identities even if a fresh scan misses them. A failed DELETE, forced native fallback, or surviving owned process fails cleanup even if another finalizer succeeds. Scoped Prime shutdown uses the qualified private supervisor socket; native Windows launch is rejected because the published default named pipe is shared.
