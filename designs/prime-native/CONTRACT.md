# Prime-native feature contract

**Historical proposal.** The active [no-fork design](NO_FORK.md) supersedes this document. The Prime fork, Prime-side bridge, global controller promise, and P1-Prime sequence below are historical proposal context. Do not execute this plan or treat its capabilities as qualified.

## Contract status

This document specifies proposed behavior and required acceptance evidence.
Every capability remains unqualified until its evidence exists at an exact implementation revision.
The reviewed source pins and current limitations appear in [DESIGN.md](DESIGN.md).

## Binding fields

The following logical fields define a proposed durable binding.
The wire encoding is not selected by this reference.

| Field | Meaning |
| --- | --- |
| `binding_id` | Stable identity of the declared Omnigent role or direct Prime run |
| `omnigent_session_id` | Omnigent conversation identity when Omnigent owns the declared role |
| `prime_root_id` | Prime's stable root-session identity |
| `execution_host_id` | Admitted host or sandbox that owns the worker |
| `canonical_cwd` | Approved workspace identity |
| `resource_digest` | Immutable profile resources and dependency manifest |
| `profile_release_digest` | Kit release associated with the session |
| `execution_policy_digest` | Policy shared by all clients attached to the root |
| `bridge_protocol_version` | Negotiated external bridge revision |
| `prime_version` | Prime runtime build used by the binding |
| `worker_generation` | Current execution-history generation |
| `event_cursor` | Last acknowledged event position within a generation |

Generation and cursor changes cause reconciliation.
They do not change the stable root identity.
An incompatible workspace, resource, policy, or root binding rejects attachment before mutation.
A tested version transition may resume a binding through an explicit compatibility or migration path.
Credentials and mutable session contents are absent from the binding receipt.

## Controller states

| State or action | Required semantics |
| --- | --- |
| Viewer | Reads snapshots, transcripts, kernel output, and runtime trees |
| Controller | Submits input, steers work, changes model settings, and answers interactive requests |
| Detach | Releases this client attachment and its control ownership |
| Cancel admission | Cancels an input that Prime has not accepted for execution |
| Cancel turn | Cancels accepted work with a recorded outcome |
| Stop root | Explicitly stops the root and its owned execution tree |
| Takeover | Transfers control through a recorded, visible operation |

The controller lease covers mutating native-terminal and Omnigent clients.
Prime's scheduled work uses the existing Prime queue.
Retries use stable operation identities.
An uncertain mutation remains uncertain until Prime's journal or snapshot resolves it.

## Acceptance matrix

| ID | Capability | Required observed result |
| --- | --- | --- |
| N01 | Native registration | Omnigent lists `prime-native` and its distinct native UI agent with provider and terminal hooks. |
| N02 | Readiness | Missing or incompatible Prime and bridge versions fail before a turn. No other harness launches as fallback. |
| N03 | Model and effort | Authenticated Prime discovery provides choices. A selected choice round-trips through the worker and both attached interfaces. |
| N04 | Identity | A fresh root reports the Kit release marker. Prime's base instructions remain present. Framework lifecycle text comes from Omnigent's owning module. |
| N05 | Skills | A selected text skill and a Python-backed skill execute their distinctive behavior from the pinned resource artifact. |
| N06 | Native terminal | Omnigent opens and reattaches the native Prime view to the admitted root and execution host. |
| N07 | Same live kernel | A Python value created through one interface remains observable through the other after client detach and reattach to the same living worker. |
| N08 | Root recovery | A killed worker recovers the stable root with a new generation or reports terminal failure. The UI identifies lost kernel state. No accepted prompt runs twice. |
| N09 | Event recovery | Duplicate events do not duplicate transcript entries. Cursor gaps and stale generations trigger snapshots or discard stale records. |
| N10 | Shared control | Two attached clients cannot both mutate the root without the enforced controller contract. The UI shows ownership and takeover. |
| N11 | Cancellation | Admission cancellation, turn cancellation, client detach, and root stop produce distinct observable outcomes. |
| N12 | Rich output | Python cells, text, images, MIME attachments, diffs, errors, and background completion retain correlation and render correctly. |
| N13 | Native commands and extensions | Required Prime commands and extension dialogs have supported control paths. Unsupported operations fail explicitly. |
| N14 | RLM hierarchy | Recursive children retain Prime root and parent lineage. They do not create declared Kit roles or duplicate roots. |
| N15 | Mixed roles | Prime works as a declared root and child in Prime, Codex, and Claude compositions. Each declared role retains its own binding. |
| N16 | Long-running controls | Goals, heartbeat, schedules, gates, compaction, and refinement remain visible and controllable through the Prime owner. |
| N17 | Quiet liveness | A healthy scheduled wait or background child wait passes the generic inactivity threshold without a false timeout. |
| N18 | Settlement | The final completed state agrees with Prime terminal quiescence and the required descendant-settlement policy. |
| N19 | MCP | A harmless real call returns the expected result. A wrong-result control fails. Disconnect and reconnect preserve explicit tool availability. |
| N20 | Enforced permissions | Allow, deny, and approval reach the actual execution gate. A denied marker write produces no marker from a terminal or Omnigent client. |
| K01 | Optional targets | Existing Kit installations migrate with Hermes and Omnigent selected. Prime is managed only when selected. |
| K02 | Deterministic resources | Direct Prime and Omnigent delivery reference the same compiled resource digest for the same source profile. |
| K03 | Lifecycle | Discovery, inspection, conflicts, partial retry, rollback, removal, and recovery work through the Prime native adapter. |
| K04 | Resource retention | A live or resumable root retains its resource revision after upgrade. New dependencies require explicit fresh-session or migration behavior. |
| K05 | Private state | Authentication, transcripts, schedules, learned state, and kernel data survive resource updates and remain absent from releases and public evidence. |
| S01 | Worker confinement | The worker, kernel, shell, MCP process, RLM child, and grandchild cannot read a forbidden host file or access paths outside the admitted filesystem policy. |
| S02 | Network policy | A permitted endpoint works and a forbidden endpoint fails from root and descendant execution. |
| S03 | Endpoint binding | A sandbox session cannot attach to a host daemon or an unrelated root. |
| S04 | Teardown | Explicit session teardown leaves no owned worker, kernel, MCP process, or descendant running in the retired execution boundary. |
| M01 | Upgrade qualification | A new Omnigent, Prime, or bridge version passes the full relevant matrix before its support record advances. |
| M02 | Patch retirement | Compatibility patches verify their exact input and have a tested upstream retirement condition. |

## Evidence records

An acceptance record names the repository revisions, runtime versions, bridge revision, resource digest, platform, scenario, and observed result.
It names the logs, screenshots, videos, test result, and command failure signal used for that conclusion.
Demo screenshots and recordings remain outside the tracked tree unless they are maintained documentation or test assets.
An absent test collection, missing artifact, or unavailable runtime is an unverified result.

## Probe dispositions

| Requirement | Raised issue | Disposition |
| --- | --- | --- |
| Native identity | A `prime-native` name could silently resolve to a generic ACP route or another harness. | Resolved by N01 and N02. |
| Resources | Empty skills, duplicate skill names, and dependency updates could change a live session. | Resolved by deterministic manifests, explicit collision errors, N05, K02, and K04. An empty skill selection is valid. |
| Composition | Declared role IDs could collide with recursive child IDs. | Resolved by separate origin and lineage types, N14, and N15. |
| Recovery | Retry or new generation could duplicate input or falsely imply restored Python memory. | Resolved by N08 and N09. |
| Concurrency | Native terminal and Omnigent could race prompts, settings, or dialog responses. | Resolved by the Prime-side controller contract, N10, and N11. |
| Lifecycle | A third target could become mandatory or permit deletion of referenced resources. | Resolved by K01, K03, and K04. |
| Native controls | The selected bridge may not cover all required command and extension-UI operations. | Unresolved. P1-Prime inventories the operations. P2 supplies N12, N13, and N16 evidence. |
| Execution policy | Approval UI could report denial while kernel or descendant code still executes. | Unresolved. P1-Prime proves the execution hook. P1-Omni proves its cross-client projection. P4 supplies N20 and S01 through S04 evidence on supported platforms. |

Eight applicable probe items have dispositions.
Six have explicit acceptance criteria and two remain unresolved.
No item uses a backstop or judgment-only verdict.
These dispositions describe specification coverage, not runtime completion.
