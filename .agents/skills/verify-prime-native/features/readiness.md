# Readiness and model choices

Users launch Prime Native and select models that Prime can use with its own credentials.

## Sub-features

- A missing Prime binary fails before a turn without another adapter launching.
- The native command identifies Prime Native.
- An empty authenticated catalog reports no available models.
- One or many available models retain their provider and model IDs.
- A selected model and reasoning effort reach the worker.

## How to get to it (user POV)

Run `omnigent prime-native --help`, launch `omnigent prime-native`, or choose Prime Native when starting an Omnigent session. In Prime, use `/login` and the model picker.

## Driving it with the CLI and HTTP API

Preconditions are the repository environment and a real Prime binary.

- Run the helper with `--doctor`. Require a Prime version and native command help. A nonzero exit means failure.
- Run `prime-agent model list` with isolated Prime configuration. Require its known table columns when models exist. Distinguish Prime's recognized empty-catalog response from a failed command or malformed output.
- Launch with a fixture provider and model. Read the session's model options and active model after extension readiness. Require the selected provider/model ID.
- Repeat with a missing `OMNIGENT_PRIME_PATH`. Require a clear failure and no Prime worker from that attempt.

## Gotchas

Prime 0.9.6 removed Pi's `--list-models`. Use `model list`. A catalog without credentials is empty. Doctor success does not prove a turn or a provider login.

The default helper proves version admission, the literal `verify/fixture` model catalog row, and that model completing turns. The adapter probe adds two exact provider/model choices and changes model and effort through the public API and native terminal. Parser tests cover malformed and empty catalogs. Real-provider login and a live empty-catalog session remain separate qualification gaps.
