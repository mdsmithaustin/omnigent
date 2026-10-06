# Prime Native

`omnigent prime-native` connects Prime Agent's terminal to an Omnigent
conversation. Messages sent from either interface appear in the Omnigent
transcript. The harness identifier is `prime-native` and the CLI-created agent
is `prime-native-ui`.

## Launch and reattach

Install Prime Agent **0.9.6** and `tmux`. This adapter requires that exact Prime
version on macOS and Linux. Configure Prime's authentication before launching.
For xAI, run `/login` in Prime and select the xAI subscription or API-key entry.
Complete the selected flow as described in the
[pinned Prime 0.9.6 provider guide](https://github.com/PrimeIntellect-ai/prime-agent/blob/e260085dd8f742e0def3d871860c9a888b114851/packages/coding-agent/docs/providers.md#xai-grok).
Saved authentication is separate from the current provider and model labels.
A listed or selected model does not prove that a request will succeed.
Omnigent copies `auth.json`, `settings.json`, and `models.json` into
private conversation state without changing the source files.
Launch is rejected on native Windows because the published default supervisor
named pipe does not identify a private conversation for scoped shutdown.

To use an existing Python environment, set `PRIME_AGENT_KERNEL_PYTHON` to its
absolute `bin/python` entry. That environment must contain Prime's published
`prime-agent-runtime` and required packages. Start the host with this setting
before creating the session. Existing hosts and native workers retain their
launch environment. Without the setting, Prime manages its default kernel
environment.

```sh
omnigent prime-native
```

The command starts or connects to the configured Omnigent server, prints a
conversation URL, and attaches the terminal. To use an existing server, pass
`--server` with its URL. To select a Prime executable outside PATH, set
`OMNIGENT_PRIME_PATH` to its absolute path. `PRIME_AGENT_CODING_AGENT_DIR` selects
the source configuration directory; its default is `~/.prime/agent`.

Arguments after `--` reach Prime. For example, `--offline` disables Prime Cloud
requests while a configured model provider still supplies completions.

```sh
omnigent prime-native -- --offline
```

Close the attached terminal window to leave the attachment. The underlying
Prime terminal and worker keep running. Reattach with the Prime Native
conversation picker.

```sh
omnigent prime-native --resume
```

`--resume` also accepts the conversation ID printed by Omnigent for direct
attachment. If the native terminal exited, Omnigent uses Prime's captured
session ID and `--resume` to reopen saved history. A restarted worker does
not preserve Python memory.

Use the conversation's stop control to end its work. Exiting the required
Prime terminal also stops the private daemon. Omnigent owns Prime's session,
extension, daemon, and interactive mode arguments, so pass-through arguments
cannot override them.

If a terminal launch is still in progress, stop returns 503 and keeps its
ownership record. Retry after launch settles. A successful stop waits for the
private runtime's processes and sockets to disappear.

## Change settings and control a turn

The model picker uses Prime's available registry. Before launch, the host reads
`prime-agent model list` with Prime's credentials. A live session reports its
own registry through the extension. Model identifiers contain the exact
`provider/model` pair. Pi credentials and curated model lists are not a fallback
when Prime discovery fails.

For a live conversation, use its model and reasoning-effort controls. The public
API applies the same controls through `PATCH /v1/sessions/{id}` with
`model_override` and `reasoning_effort`. These controls also apply to a custom
agent whose executor uses `prime-native`, without a wrapper label. An explicit
harness override toward or away from Prime takes precedence over a stale wrapper
label. A combined request changes the model
before the effort. Prime's native observations update the displayed settings,
including changes made in the terminal. A delayed acknowledgment cannot replace
a newer terminal choice. Clearing a live setting or writing it with `silent`
is unsupported.

Omnigent maps `none` to Prime's `off` and `ultra` to `max`. Prime may clamp the
requested effort for the chosen model. The displayed value is the effective
native level.

A settings acknowledgment requires a native callback whose observation reaches
Omnigent with HTTP 2xx. Rejected or missing publication returns 504 after a
possible mutation. Prime 0.9.6 emits no callback when a requested model or effort
is already active, so those requests can also return 504. Omnigent does not
invent an observation or replay the control.

Each callback publication has a one-second deadline within the control's
remaining budget. A slower response can return 504 even if the server later
stores the observation. Inspect the displayed native state before another
control.

Send `{"type":"compact"}` or `{"type":"interrupt"}` to
`POST /v1/sessions/{id}/events` for the corresponding control. Compaction waits
for Prime's completion callback. Interrupt returns admission before the native
aborted result settles. Messages also receive HTTP 202 for admission. Delivery
to the extension inbox does not complete a native turn.

Rejected controls return a non-success response. A missing live binding returns
503. An expired acknowledgment after possible mutation returns 504 and is not
retried. A combined settings failure retains the observed partial result.
Inspect the error's `outcomes` for the controls already attempted. Each runner
serializes its admitted settings in order. Independent terminal
selections remain outside that queue, and interrupt bypasses it.

## Use declared resources

A Prime Native agent receives its custom instructions after Prime's base
instructions. Omnigent appends framework instructions through the shared
runtime composer. The adapter does not copy framework policy into Prime.

An agent's declared MCP tools use Omnigent's existing session proxy. Prime
advertises the discovered tools through the shared extension, and the relay
executes them. Discovery failures appear in the server or runner logs. The
native terminal still starts when an additive MCP server is unavailable.

After successful discovery, the retained MCP connection can recover on a later
call even when an earlier call exhausted reconnect attempts. A failed first
initialization does not enable that recovery. Prime receives a completed native
tool error when runner dispatch fails. A subsequent eligible call can recover
the connection after the server returns. The existing ordinary-call circuit breaker
still applies. See [MCP recovery and retries](AGENT_YAML_SPEC.md#mcp-recovery-and-retries)
for serialized calls, startup cancellation, teardown timing, and MRTR limits.

Bundled skills use `load_skill` and `read_skill_file`. A Python resource delivered
by those tools can execute in Prime's native `ipython` kernel. Configure resources
in an [agent specification](AGENT_YAML_SPEC.md) and start a session with that
agent. The `omnigent prime-native` shortcut creates the `prime-native-ui` agent.
It does not select a custom agent bundle.

Omnigent evaluates relayed root tools against the agent's policy. That gate does
not confine arbitrary Python code or recursive descendants.

## State and compatibility

Conversation state lives under `$OMNIGENT_DATA_DIR/prime-native/`, or
`~/.omnigent/prime-native/` when the variable is unset. Each conversation has
its own Prime configuration, temporary directory, daemon socket, and saved
sessions. When that socket path would exceed the Unix limit, Omnigent uses a
private digest directory under `/tmp/ogp-<uid>/`. Stop retains saved
sessions. Conversation DELETE attempts adapter-state removal after shutdown.
Runner cleanup is best effort. A successful response can leave state when the
runner is unavailable or removal fails; it does not prove observed retirement.

Startup and host maintenance stop orphaned Prime runtimes whose recorded owner
is dead in the same process namespace and boot. A live terminal protects its
runtime even after its runner exits. Recovery retains saved sessions and private
configuration for resume. Unknown or legacy ownership records remain untouched.
Cleanup sends Prime's public shutdown command to the qualified private
supervisor socket. Omnigent clears transient ownership only after that socket
and its owned processes stop. If ownership or shutdown cannot be proved, it
retains the runtime for a later cleanup attempt. The public
`prime-agent shutdown` CLI holds a per-user admission lease. Omnigent uses
the private socket for session cleanup. Native Windows launch is rejected
because Prime's default named pipe is shared.

Prime is a fork of Pi that now develops independently, as described in
[Prime's README](https://github.com/PrimeIntellect-ai/prime-agent/blob/e260085dd8f742e0def3d871860c9a888b114851/packages/coding-agent/README.md).
The adapters share
the extension inbox, message conversion, and tool relay transport. Prime has
its own launch, version check, session resume, and daemon shutdown code.
Pi's `--session` and `--list-models` are not Prime arguments. Use
`prime-agent model list` for Prime's credential-filtered model catalog.

Prime loads external extensions into recursive RLM children. Omnigent binds its
extension effects to the public session header's root depth. Children continue
executing in Prime. Their extension instances do not consume root controls or
publish their transcript as the Omnigent root conversation. Missing or invalid
depth is unknown and does not admit a root binding.

The published daemon failed the replay-cursor and tested worker-recovery gates.
The adapter therefore keeps the extension transport. Durable recovery across
missed events, global control exclusivity, completion-dependent declared children,
and whole-descendant settlement remain unqualified. Agent Profile Kit is outside
this implementation. Root policy hooks do not confine arbitrary Python code or
recursive descendants. [The acceptance matrix](../designs/prime-native/NO_FORK.md)
records each original requirement and its remaining evidence.

## Remaining roadmap

The supported adapter has measured launch, messages, controls, declared resources,
root tool denial, and living-kernel reattachment. Finite native `running` waits
passed on parent revision `afe870ce93c9df8bccfa88be13bb54c39d64e5ae`.
The later MCP working-tree repair passed one declared regular HTTP reconnect
case with configured xAI credentials and Grok 4.7. Finite waits have not been
rerun on those changed MCP and extension bytes. Each receipt identifies its
source, so neither result qualifies an untested final commit. Earlier provider
and MCP source units passed independent cumulative review
at `094a84e198a5ebdbb5e2c6e8934b189b43fce777`. The private composition with
actual merged PR5 requires fresh independent source and documentation review.
The verification workflow records
the current hashes separately from historical PASS+NOTES verdicts.
Fresh live WAIT and MCP receipts on final integrated bytes remain pending.

The next qualification priorities follow the gaps in the
[acceptance matrix](../designs/prime-native/NO_FORK.md#original-acceptance-criteria):

1. Extend N19 beyond regular HTTP. Exercise real MRTR callbacks and opaque
   approval retries across restart, stdio restart, wider error and cancellation
   paths, deadlines, server classes, and providers.
2. Extend N17 beyond finite `running` tools. Prove genuine `waiting`, status-only
   protection during complete output silence, default one-hour thresholds, and
   indefinite waits. Qualify gateway retry behavior separately.
3. Resolve the published daemon replay and worker-recovery failures before
   claiming N08 or N09. Global control exclusivity, completion-dependent declared
   children, and whole-descendant settlement remain gaps under N10, N15, and N18.
4. Qualify the remaining controls, rich output, approvals, descendant visibility,
   and external containment individually. Kit remains external to this adapter.

The original 31-item program remains incomplete. The workflows below repeat the
bounded passing cases and retain failures as separate evidence.

## Verify the adapter

The project skill drives a real Prime binary, Omnigent server, host, runner,
and terminal. A local model endpoint returns a fixed reply to prove message
transport without provider credentials.

```sh
uv sync --group dev
.agents/skills/verify-prime-native/scripts/verify.py --prime-path /tmp/omnigent-prime-runtime/application/prime-agent
```

Supply the actual path to your Prime 0.9.6 binary. Require exit code zero and
`result.json` with `PASS` in the printed evidence directory. Inspect
`terminal-items.json` for both submitted messages and both
`PRIME_NATIVE_PONG` replies. `cleanup.json` must report no owned survivors.
`reattach.json` records the same native identity and live process IDs across
detach and resume. The helper retains its evidence after removing scratch state.

See [the verification skill](../.agents/skills/verify-prime-native/SKILL.md)
and its feature map for qualification gaps and additional scenarios.

The broader adapter driver executes real tools and a living Python kernel. It
uses disposable resources and a local model fixture, so commercial provider
credentials are not required. Supply the path to an interpreter with Prime's
published Python runtime installed.

```sh
PYTHONPATH="$PWD" uv run --no-sync python .agents/skills/verify-prime-native/scripts/adapter_probe.py --prime-path /absolute/path/to/prime-agent --kernel-python /absolute/path/to/kernel-venv/bin/python
```

Require exit code zero, every required scenario marked `VERIFIED` in
`outcomes.json`, an empty `fatal` list, and no owned survivors or forced cleanup
in `cleanup.json`. Inspect the retained tool results and actions. Require the
before and after source inventories to match in `source-integrity.json`, and
the live extension bytes to match in `deployed-extension-identities.json`.
Inspect `kernel-identity.json` for the supplied interpreter, environment prefix,
loaded `rlm` source, and owned kernel PID. The probe checks that identity against
the process records and retains the literal memory results across reattachment.
A readiness receipt is optional orchestration input,
not a prerequisite for public use. This broader probe pins the tested macOS
arm64 artifact. See the [full probe workflow](../.agents/skills/verify-prime-native/SKILL.md#full-adapter-probe)
for the runtime prerequisites, negative controls, and evidence inventory.
The [authenticated finite-wait workflow](../.agents/skills/verify-prime-native/SKILL.md#qualify-authenticated-finite-waits)
uses configured xAI credentials and Grok 4.7. Its historical five-case run on
`c22045ea9` passed its original checks with actual 60-second and 300-second Python
tools, matched idle controls, HTTP steering, and reattachment to the same living
kernel. The driver SHA256 was
`a2584ce9b149568e791f49ace5b7be7c332493770ad771cde18cb0e20d8335bb`.
Those bytes did not require exact public prompt ownership and could report
cleanup success while outer scratch remained. Their result publication could
also precede mandatory finalization. Preserve the historical receipts and their
limits. Earlier journal and pane cleanup failures remain FAILED.

The later parent run
`provider-waits-gkknmv6b` passed five cases on revision `afe870ce93c9df8bccfa88be13bb54c39d64e5ae`,
with actual 60-second and 300-second Python tools, matched runner and pane idle
controls, HTTP steering, and reattachment to the same living kernel. Its receipt distinguishes finite native
`running` from unqualified genuine `waiting` and status-only protection during
silence. The workflow requires copied-auth removal and exact owned-process
absence. It does not inspect unrelated terminal input. This run used the same
historical driver hash and shares the prompt, outer-scratch, and publication
limits above.

Current verification requires the exact native queue receipt before the tool
ends, followed by the tool result, queued user entry, and successful native reply.
HTTP 202 proves admission only. The public conversation must contain exactly one
fresh user message whose concatenated `input_text` equals the prompt, before the
selected final and with no intervening user. Native and public user and final
IDs remain independent. Require the ordered tool evidence and literal reply too.

The provider command now requires `--cooperative-cleanup`. Admit that flag only
for a controlled namespace with settled owned writers and no hostile concurrent
namespace writer. The flag, private permissions, and watches do not exclude
hostile same-UID interference in the final check/syscall window. That gap remains
BLOCKED. Cleanup proves scoped namespace removal, not physical erasure or
snapshot destruction.

Require each child's settlement, removal, and owner-closure receipts, plus
`completion.json` with matching result and manifest hashes. A provisional
`qualified` value is insufficient. The external scratch owner covers fallible
construction and requires explicit fixture settlement before removal. Errors
remain sticky, including failures after removal or during receipt and handle
closure. See the [verification workflow](../.agents/skills/verify-prime-native/SKILL.md#qualify-authenticated-finite-waits)
for the command, required receipt fields, and retained-failure checks.

This private PR6 composition combines accepted PR6
`1f5a8e4d6fb678a5ed65bc1021a3d018695e03a2` with the actual PR5 merge
`63b3d78f14fd23f68b651c989eb0a0e0fd8b2c19`. The accepted PR6 review returned
Source PASS+NOTES and documentation PASS, with 950 source cases and 20 controls.
Those results describe the old PR6 context. Fresh independent source and
documentation review of this composition remains required.

The provider helper SHA256 remains
`c80b409a1511c48bd2d6f7048bc1d02d3136e4c177197a662ad98110c2f99db2`.
At Source `3ac1fd1b2abca91e8973f059fd9d4cd8b6bcbb9c`, the MCP helper SHA256 was
`b382a4c1bff9e313e0ef53a5cb81ce391e1f8f1b5c63d06f92b08366dfd6b466`.
The repaired helper SHA256 is
`043dab4e5c213dbd87ffe6f3ef49a59d3e4fa8d1f30563e7633d71314a50b6a4`.
Its fixture close fallback reaps the exact registered child even when identity
metadata capture fails. The original failure remains sticky.
`OWNER_SHA256` admits that exact provider helper. Every present native-final
error rejects, including empty containers, false, zero, and whitespace-only
strings. Only missing, null, and exact empty-string errors are absent.
A successful stop reason and matching literal reply remain required.

The composition preserves PR6's paired root rejection and task-boundary tests.
Relayed tools and task operations without a live root binding return a paired
error result. They do not consume root controls or mutate the root task list.
PR5's historical standalone 20-case root context does not replace this contract.
Its earlier source review at `e1aa5176782d2cce6d50ad9e2115977339143a5c` passed
854 context cases and 69 controls. The parent failed 21 controls and passed 48.
These historical counts are unchanged and are not fresh composition results.

The actual PR5 merge has the reviewed `fd2b0b4bbaf52cecf7bc34129ef6804c0fdd14c3`
tree. It brings Main's bootstrap, CRDB, CLI registry, and agent-name index
repairs, the OAuth prerequisite correction, and two literal-readiness waits.
The earlier registry failure and Linux empty-readiness failure remain historical.
Linux misc CI passed at fd2. That result does not qualify this new composition
or any native runtime. The imported registry fix requires a fresh scoped check.
No actual MCP call, provider acceptance, or fresh live WAIT qualification is
claimed. Full S01-S04 and the 31-requirement program remain incomplete.

The prior PR6 candidate `cb11a4ed94a15427e7cd0a37e3847de0da0169a1` had the
exact reviewed `98309e7c5136c3801bfd227e79289f2d62ed7915` tree against
base `c0b3173d5a0a874255d9bef35c9e0c4fb895b77d`. Its 925 repository cases
and seven independent checks are historical evidence for that composition.
Its provider SHA256 was
`c4d86b911fd5b793ec24453c666b43c14a6736164478051f3b005085cf2847c3`,
and its MCP SHA256 was
`1a55d312be7c8317c898f89ff9ec37001f04a78d937b0f2e839713c7486fb435`.
The provider-only port at `a5d0703a57882800a3a0b36dc925a090b4546451`
matched `57293032d4fe2f53bd4c688a14cac4e9fd41e930`. These historical
identities do not establish current PR5 and PR6 whole-tree identity or runtime
qualification.

The preceding provider SHA256 was
`45b233b559bb9b5206d8db51b4d638dff15fb32872ac2dc64f3a6814ae57c10c`.
The paired historical MCP SHA256 was
`9f47ad394e923dc6ae13c92907b81d6830a289bd4d36a7a0d2df12637fe966b4`.
Their independent cumulative review at
`094a84e198a5ebdbb5e2c6e8934b189b43fce777` covers that source context only.
Historical MCP review at `ad945096ed6da2c1ffe866a33841188f77770b4f`
returned PASS+NOTES for MCP SHA256
`b03472857f5cf666e026093ee664a6e58cff0858603e0e05a04bb1c24f7b7832`.
Fresh final-byte WAIT and MCP receipts remain required after source or context
changes.

The later audited `provider-waits-d_tb1jmg` run at
`4248f5dafee18884c8d66caa2f1480f1c619f58e` also remains FAILED. All five native
replies failed at kernel seed, before WAIT, with false committed completions.
All five lack external compact-root retirement authority. The precise DELETE
and filesystem branches and native cause remain unknown. The first case also
has a sticky `unrelated_user_process_identity_changed` failure with unknown
cause. Preserve these allocations and historical receipts. Fresh MCP execution
on the repaired source has not run.

The configured private basic-response attempt at
`4248f5dafee18884c8d66caa2f1480f1c619f58e` used the preceding provider hash
`45b233b559bb9b5206d8db51b4d638dff15fb32872ac2dc64f3a6814ae57c10c` and failed.
Its expected `OK` reply was absent, and its native final reported `agent_lifecycle_failure` despite public CLI exit
zero. Settlement failed and the daemon remained retained. Basic Prime response
is still unverified. The durable public Prime 0.9.6 installation has version and
hash checks, but no successful model-response baseline. Neither this result nor
the WAIT failures establish an authentication, provider, kernel, or installation
cause. Preserve the retained allocations and private receipts.

A later sanitized basic-receipt review records the actual basic response as
FAILED with `xai_no_usable_credential` and unknown cause. Finite shutdown and
known PID/socket absence do not prove normal/full settlement, a successful
response, exhaustive descendants, credential-source comparison, removal, or
future custody. A successful model-response baseline remains unverified.

For a failed reply, inspect the current `<operation>-native-failure.json`.
The separate predicates receipt marks stale state as `previous_poll`. Missing,
null, and empty-string errors are absent; non-string errors are malformed and
present. The bounded failure observation contains no native error text, sample,
or hash. Its coarse class does not establish provider authentication or cause.

The failure receipt's nested `diagnostic` uses a closed finite schema. It
projects known structured diagnostic types, kinds, status and code buckets,
and matched same-operation ipython status and `isError`. It exports no raw
payload or unknown-value hash. Startup, protocol, and bootstrap flags remain
unavailable without a typed producer. Diagnostic categories do not attest the
backend cause, and previous-poll predicates cannot populate the current failure.

Qualified cleanup admits root, parent, and vnode witnesses before public DELETE
while the dedicated runner can still perform owned teardown. Public Stop
remains a separate control qualification and a cleanup attempt after DELETE
failure. A successful Stop fallback cannot clear the DELETE failure.
`retirement-attempts.json` records bounded allocation and session
bindings, reached phase, status, held identities, no-follow name state, validated
event flags, and allowlisted branches on every retirement-attempt exit.
Unknown observations remain explicit. A retained external root or DELETE 200
without the combined removal witness cannot qualify retirement. There is no
manual external-root reclamation. Sticky errors, complete settlement, captures,
and owner exit before committed publication remain required. The cooperative
namespace precondition still leaves hostile same-UID final-syscall interference
BLOCKED. See the verification skill for the exact finite observation contract.

The owner captures the expected CLI log alias as metadata only, then removes
it only after writer settlement and matching identity checks. See the
[ownership and failure contract](../designs/prime-native/NO_FORK.md#provider-probe-ownership-and-completion)
for exact receipt requirements and the retained failed WAIT record.

Genuine native
`waiting`, status-only protection during complete output silence, one-hour and
indefinite waits, and the full S01-S04 guarantees remain unqualified.
The separate daemon probe is a qualification gate and currently exits nonzero
for the published runtime's known gaps.

To repeat the declared regular HTTP MCP restart case, run this command from the
checkout to qualify. Supply the admitted Prime 0.9.6 binary, the kernel
environment's `bin/python` entry without resolving its symlink, saved xAI
subscription OAuth credentials, and a private evidence directory.
Admit `--cooperative-cleanup` only under the controlled namespace precondition above.

```sh
PYTHONPATH="$PWD" uv run --no-sync python .agents/skills/verify-prime-native/scripts/mcp_probe.py --prime-path /absolute/path/to/prime-agent --kernel-python /absolute/path/to/kernel-venv/bin/python --auth-source /absolute/path/to/auth.json --evidence-parent /absolute/path/to/private-evidence --cooperative-cleanup
```

Follow the printed receipt path directly into the fresh `provider-mcp-*`
directory. Require exit zero and all eight required claims `VERIFIED` in
`result.json`. Require `completion.json` with `passed: true` and compare its
`result_sha256` and `manifest_sha256` with the retained files. Missing completion,
false completion, or either mismatched hash fails. The result's provisional
`qualified` value and raw `passed` fields are not success authority.
Require verified cleanup with no errors, survivors,
private sockets, credential copies, or forced fallback. A nonzero exit, missing
claim, or non-`VERIFIED` claim fails qualification. Require fixture closure for
both generations, runtime settlement in the same evidence directory, and
allocation-matched removal and closed-owner receipts. A failed fixture receipt retains scratch and blocks success.
Owner exit precedes completion publication. Inspect the completed native
outage error before generation 2 starts, the new server PID and nonce, matching
native, provider, and fixture results, and the same root and kernel object after
recovery. Follow the [MCP verification workflow](../.agents/skills/verify-prime-native/SKILL.md#qualify-regular-http-mcp-reconnect)
for the exact sequence and evidence files. This case does not qualify MRTR,
stdio, all servers, or all providers.
