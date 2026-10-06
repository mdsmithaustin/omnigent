# Live Prime controls

Use a fresh owned Prime terminal so it loads the current extension. An older
resident extension without control receipts is unavailable. Preserve the
native terminal and capture the public API action and its observed result.

## Model and effort

Repeat the public controls with a custom agent whose executor resolves to
`prime-native` and whose session has no `omnigent.wrapper` label. Require its
native identity and the same model catalog. A successful settings PATCH may
also update its title. Rejected settings must preserve the observed model and
effort. An explicit harness override involving Prime determines the control
contract even when an old wrapper label remains.

The host uses Prime's public `model list` command for prelaunch discovery. The
resident extension uses the credential-filtered `getAvailable` registry. A
discovery failure cannot use Pi credentials or a curated fallback. A known empty
registry is distinct from a failed command.

Apply an exact `provider/model` with `PATCH /v1/sessions/{id}` and
`model_override`. Apply `reasoning_effort` through the same route. Require the
native selection, effective effort, and current GET projection. Prime clamps
effort for the chosen model. `none` maps to native `off`, and `ultra` maps to
`max`. Change a setting independently in the terminal and require its updated
projection.

For a combined PATCH, require model before effort. Reject an unavailable model,
invalid effort, cleared live setting, or silent live setting write. Record a
partial failure without rolling back a newer terminal selection. A delayed
acknowledgment is not authoritative active state.

Require the actual native callback and HTTP 2xx publication before accepting a
settings receipt. Rejecting a model or effort observation after mutation must
produce `unknown`, with no replay. A model switch that clamps effort needs both
publications. A later healthy control must work after publication failure.
Prime 0.9.6 emits no callback for an already active value. Record the resulting
504 as an observation limit rather than inventing callback evidence.

Concurrent settings admitted by one runner must execute in admission order.
Interrupt must still reach the active loop while a setting waits. This local
ordering does not fence terminal users or other producers.

## Compact and interrupt

Send `{"type":"compact"}` to `POST /v1/sessions/{id}/events`. The successful
response retains the route's HTTP 202 status and reports `queued: false` only
after Prime's completion callback. Require the native compaction result.
Missing API, callback error, malformed result, and a lost acknowledgment are
non-success. Uncertain compaction must not be replayed.

Send `{"type":"interrupt"}` during actual work. Require HTTP 202 and
`interrupt_accepted`. That response cannot publish an idle completion or a
settled-child result. Require the later native interrupted event and a usable
follow-up turn. Keep running and waiting panes resident through the inactivity
reaper. An aborted or error loop cannot become successful idle.

Include a Python tool that is still running when interrupted. Prime may leave
the preceding assistant's stop reason as `toolUse`. The extension retains the
public abort signal for that root loop and observes its later end. A terminal
abort uses the same observation. Abort admission alone proves no loop ended.

## Evidence limits

The adapter probe drives the real controls. Focused tests cover missing,
expired, stale, malformed, and partial outcomes that need controlled callbacks.
An unavailable binding returns 503 before control mutation. A possibly applied
control with no usable result returns 504 and is not retried. All required probe
scenarios must say `VERIFIED` and the run must exit zero.

The independent native TUI remains outside Omnigent's settings queue. These
controls do not qualify global exclusivity, per-prompt model atomicity, arbitrary
dialogs, or whole-descendant settlement.
