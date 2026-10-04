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
- For orphan recovery, terminate only the isolated session's wrapper and terminal without normal shutdown. Run startup or host maintenance. Require no owned daemon or worker, retained saved sessions and configuration, and zero recoveries on a second sweep. Repeat with the terminal alive and require its runtime to survive.

## Gotchas

Prime itself persists workers after the TUI exits. The adapter explicitly shuts down its private daemon when the required TUI exits. Detaching the tmux attachment keeps that TUI alive.

The default helper detaches the owned tmux attachment, resumes the same conversation, and requires the same native session ID and owned Prime process IDs. It also checks session deletion and owned process absence. Saved-history resume after a worker restart, interruption, and kernel persistence remain gaps until driven. Stable root recovery, controller leases, and uncertain-command reconciliation require the excluded Prime-side work.
