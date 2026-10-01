# Messages through Prime and Omnigent

Users can send a message from the Prime terminal or the Omnigent conversation and observe the reply in both.

## Sub-features

- A fresh terminal starts with no conversation messages.
- A submitted message appears while the model runs.
- A completed message has a nonempty assistant reply.
- A provider error appears as an error without a false completion claim.
- A follow-up sent during a turn preserves delivery order.
- Long text reaches Prime without truncation.

## How to get to it (user POV)

Run `omnigent prime-native` and type in the terminal. Open the printed conversation URL to send from Omnigent. `--server` selects an existing server.

## Driving it with the PTY and HTTP API

Preconditions are an owned server, ready Prime terminal, and a selected model.

- Run `.agents/skills/verify-prime-native/scripts/verify.py`. It posts a unique HTTP prompt, then types a different prompt into the PTY. Require both user messages and two literal `PRIME_NATIVE_PONG` replies in `terminal-items.json`. Capture both actions separately.
- Inspect `terminal.txt` for the terminal reply. Transcript persistence alone does not prove the terminal rendered it.
- Make the fixture return a provider error. Require a visible error and no successful assistant reply for that action.
- Send a follow-up during a bounded active turn. Require both replies in order. Record this as a gap unless driven.

## Gotchas

The shared Pi file protocol acknowledges enqueue before Prime finishes. An empty inbox, successful POST, or idle harness process is insufficient proof. Match replies to the submitted action.

Public messages return HTTP 202 for admission. Prime's internal delivery return
cannot publish `response.completed` or complete a declared child. The extension
observes the actual native turn. Requests with `stream=true` also retain admission
semantics for Prime.

The default helper covers HTTP and terminal completion. Empty, loading, provider error, queued follow-up, and long-text states remain gaps until separately driven.
