# Prime-native implementation plan

**Historical proposal.** The active [no-fork design](NO_FORK.md) supersedes this document. The Prime fork, Prime-side bridge, global controller promise, and P1-Prime sequence below are historical proposal context. Do not execute this plan or treat its capabilities as qualified.

Build a first-class `prime-native` route for operators who need Prime roots and declared Omnigent roles to keep their own identities and policy bindings. The program enforces a Prime-owned worker with a versioned bridge, a distinct Omnigent native contribution, and optional Kit delivery. P1-Prime runs in a future Prime repository. P1-Omni and P2 form one Omnigent fork stack. P3 and P4 form one Kit stack. Cross-repository dependencies use exact merged revisions, never a shared GitHub PR stack. The reviewed snapshots are Omnigent `7752eef36d1c2a1148a42dd3afccc16c3b08bcbd`, Prime `2d24ad4e6b2d1ee8e6919af6f108e980a14d550e`, and Kit `c8cdf6c2180acf47a79c64730a7ca3c686b114aa`. These pins are proposal inputs, not qualified releases.

## How to read this

One box is one unit of work. Every box names the evidence that checks it. A nested box is a sub-step of the box above it. Check a box only when its evidence exists, a file, a log line, a screenshot, a test run, or a SHA. The body is a how-to. The appendices explain and record.

The program runs `"$PSTACK_SKILLS_ROOT/poteto-mode/playbooks/autopilot-stack.md"`. The operator reviews the screenshots and video for every PR. The operator alone lands P1-Prime in Prime, P1-Omni then P2 in Omnigent, and P3 then P4 in the Kit. PR owners prepare verified heads and stop at merge-ready.

Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

## Program checklist

### Arm the program

- [ ] State the protocol and selected scope to the operator. A normal implementation session runs its selected unit boxes manually under the existing request. Do not arm a `/goal`, a loop, or an automation from an ordinary implementation go.
- [ ] Only on the operator's explicit user request to run this full program unattended, arm a `/goal` with this exact text. "Run `designs/prime-native/IMPLEMENTATION.md` as P1-Prime, P1-Omni, P2, P3, and P4 in dependency order. A PR is verified only when its unit, live, and perf boxes are all checked. PR owners prepare verified repository-local stacks. The operator alone lands them after review. Done means every accepted gate N01 through N20, K01 through K05, S01 through S04, M01, and M02 has passing evidence at qualified revisions. Any required unverified item leaves the program incomplete."
- [ ] In private, untracked run state, record the absolute `PSTACK_SKILLS_ROOT`, `PSTACK_SOURCE_ROOT`, and `PROJECT_ROOT` bindings from the **pstack-harness** skill. Record whether this program owns `PSTACK_TEMP_ROOT`. Keep concrete host paths out of this plan and other shared files.
- [ ] Read these from trunk at program start. Re-read them at every tick.
  - [ ] `git -C "${PSTACK_SOURCE_ROOT:?}" fetch origin '+refs/heads/main:refs/remotes/origin/main' && git -C "$PSTACK_SOURCE_ROOT" show 'origin/main:skills/poteto-mode/playbooks/autopilot-stack.md'`
  - [ ] `git -C "${PSTACK_SOURCE_ROOT:?}" fetch origin '+refs/heads/main:refs/remotes/origin/main' && git -C "$PSTACK_SOURCE_ROOT" show 'origin/main:skills/swarm/SKILL.md'`
  - [ ] At the coordinator's Omnigent `PROJECT_ROOT`, check `git -C "${PROJECT_ROOT:?}" cat-file -e 'origin/main:.claude/skills/verify-omnigent/SKILL.md'`. If it succeeds, read `git -C "${PROJECT_ROOT:?}" show 'origin/main:.claude/skills/verify-omnigent/SKILL.md'`. Otherwise record that the proposed control skill is absent from trunk and read its future P1-Omni version only at the exact reviewed P1-Omni head.
  - [ ] `git -C "${PSTACK_SOURCE_ROOT:?}" fetch origin '+refs/heads/main:refs/remotes/origin/main' && git -C "$PSTACK_SOURCE_ROOT" show 'origin/main:skills/poteto-mode/playbooks/opening-a-pr.md'`
  - [ ] `git -C "${PSTACK_SOURCE_ROOT:?}" fetch origin '+refs/heads/main:refs/remotes/origin/main' && git -C "$PSTACK_SOURCE_ROOT" show 'origin/main:skills/how/SKILL.md'`
  - [ ] `git -C "${PSTACK_SOURCE_ROOT:?}" fetch origin '+refs/heads/main:refs/remotes/origin/main' && git -C "$PSTACK_SOURCE_ROOT" show 'origin/main:skills/interrogate/SKILL.md'`
- [ ] Only on the operator's explicit user request to run this full program unattended, arm the 30-minute audit tick. In a local session, a real terminal `/loop`. In a cloud root, a cloud-sleeper wake chain. Never leave the cadence to memory.
- [ ] Use this tick prompt, verbatim. "Re-read the execution playbook from trunk and the armed /goal. Audit the operation against both and fix drift in this tick. Probe every active lane and judge progress by side effects only. Stand down a stuck lane and dispatch its replacement now. Then post a short status message to the operator in chat only when the audit found a tracked change that no earlier status message reported, such as a PR opened, a code-ready head, a round launched or closed, a verdict, a merge, a stuck agent and the action taken, a blocker added or cleared, or a decision only the operator can make. Name every such change and nothing else. Do not repeat a table, the merged list, or an unchanged blocker. If the audit found none, end the turn with no reply text. Either way, log this tick's row in your decision trail. The row names the items reported, or none."
- [ ] On the operator's hold or stand-down, send every owner a zero-writes order at once.

### Spawn owners

- [ ] Spawn one owner per PR with the full lifecycle the execution playbook names.
- [ ] Give every owner the recorded `PSTACK_SKILLS_ROOT` and `PSTACK_SOURCE_ROOT` bindings and its repository-local `PROJECT_ROOT`. State whether the program owns a temporary source clone.
- [ ] Follow this dependency graph. Cross-repository dependencies require merged parent revisions. A same-repository child starts from its parent's exact verified head and targets the parent branch until the operator lands that parent. Record each required parent SHA in the child PR.
  - [ ] P1-Prime starts in a future Prime fork from its reviewed revision.
  - [ ] P1-Omni starts in this Omnigent fork from `design/prime-native-integration` after P1-Prime merges. Rebase the design input onto current fork `main` before implementation. It records the exact P1-Prime SHA and the design commit SHA.
  - [ ] P2 is an Omnigent child PR from the exact verified P1-Omni head in this fork and targets `prime-native-vertical-slice` until P1-Omni lands.
  - [ ] P3 starts in a future Kit branch after P1-Omni and P2 merge. It records their exact Omnigent SHAs and the P1-Prime SHA.
  - [ ] P4 is a Kit child PR from the exact verified P3 head and targets `prime-native-resources` until P3 lands. It records the exact qualified Prime and Omnigent revisions.
- [ ] Use these proposed repository-local branches and PR identifiers after the relevant repository exists. P1-Prime uses Prime branch `prime-native-bridge` and PR `P1-Prime`. P1-Omni uses Omnigent branch `prime-native-vertical-slice` and PR `P1-Omni`. P2 uses Omnigent branch `prime-native-controls` and PR `P2`. P3 uses Kit branch `prime-native-resources` and PR `P3`. P4 uses Kit branch `prime-native-qualified-launch` and PR `P4`. Record `PR_BASE_BRANCH`, `PR_HEAD_BRANCH`, `PR_HEAD_SHA`, `PR_ID`, `LANE_NUMBER`, and `LANE_SLUG` in each lane's untracked run state before commands use them.
- [ ] Hold the file boundaries. P1-Prime touches only the future Prime fork. P1-Omni and P2 touch only this Omnigent fork. P3 and P4 touch only the future Kit branch. Do not rebase, retarget, or append a PR across repositories.
- [ ] Hold the review gate. P1-Prime, P1-Omni, P2, P3, and P4 change an interaction. They wait for the operator's review in chat with screenshots and a video before merge.

### PR mechanics, for every PR

- [ ] Resolve the forge once. Default to `gh`; if `command -v origin` succeeds and Origin can resolve the repository, use `origin pr` for every PR operation. Record any fallback to `gh`. Never require `gt`.
- [ ] Open the PR ready, never draft, with `origin pr create --status open --base "$PR_BASE_BRANCH"` or `gh pr create --base "$PR_BASE_BRANCH"` according to the resolved forge. A stack child targets its parent branch only in the same repository. A cross-repository dependency records the exact parent SHA in its body and targets that repository's `main`.
- [ ] Run the repo's lint and typecheck once before the PR-facing push. Push with hooks on.
- [ ] Read `"$PSTACK_SKILLS_ROOT/unslop/SKILL.md"` and apply the bundled **unslop** skill before each commit. Run `/no-comments` before review.
- [ ] Triage every Bugbot and automated security reviewer comment per `"$PSTACK_SKILLS_ROOT/poteto-mode/references/bugbot-triage.md"`.
- [ ] Before the code-ready report, rebase a root PR onto current trunk or restack a child onto its exact verified parent head. Keep the parent branch as the child's base until landing. Babysit only within the PR's repository. After the operator lands the parent, restack the child onto current trunk, retarget it to `main`, and refresh any invalidated checks before landing. Keep the chosen merge base in fix rounds. Rebase again only at merge prep, on a `git merge-tree` conflict with that repository's trunk, or on a CI failure that comes from a change on that trunk.

### Verdict and merge, for every PR

- [ ] At the code-ready head SHA and at each later push that changes the patch, run the swarm per `"$PSTACK_SKILLS_ROOT/swarm/SKILL.md"`. One gates lane. The ten live lanes from the PR's **Verify, live** block. The perf lane from its **Verify, perf** block. Two or more audit lanes, each with its own focus, that read the diff and the receipts and distrust the PR body. The root audits the receipts in the merge-ready report before the verdict.
- [ ] Clean only when every lane is `PASS`. Findings go back to the owner, including a defect that a lane filed as a note. A new head gets a fresh swarm and a fresh verdict, except for results that stay valid under the patch-id rule in `"$PSTACK_SKILLS_ROOT/poteto-mode/playbooks/shipping.md"`.
- [ ] The repository owner appends a clean PR only to its repository-local stack. The operator lands each contiguous clean stack bottom-up after each review gate. A later push requires a fresh verdict unless `"$PSTACK_SKILLS_ROOT/poteto-mode/playbooks/shipping.md"` permits the unchanged patch-id result.

### Boot recipe, for every live lane

Each live lane runs in its own isolated worktree or cloud environment at the PR head. P1-Prime uses the documented Prime native terminal and bridge driver. P1-Omni and P2 use `verify-omnigent` only after P1-Omni creates it and the lane reads that skill at the exact reviewed P1-Omni head. P3 and P4 use `verify-profile-kit` when it can drive the Kit surface. If a required project control skill is absent, its owner creates or qualifies the driver before claiming the relevant live gate and records the risk until then.

- [ ] Restore `PSTACK_SKILLS_ROOT`, `PSTACK_SOURCE_ROOT`, and `PROJECT_ROOT` before reading a pstack or consumer skill.
- [ ] `git fetch origin "$PR_HEAD_BRANCH" && git checkout "$PR_HEAD_SHA"`.
- [ ] Start the repository-local surface. P1-Prime starts the pinned Prime bridge and worker. P1-Omni and P2 start `omnidev` and the pinned Prime bridge. P3 and P4 start the Kit fixture with its documented launch command. Wait for the bridge ready record and any displayed UI URL.
- [ ] Deliver input only through the selected project control skill when it exists. Before that skill exists, P1-Prime uses the admitted Prime terminal, P1-Omni and P2 use `omnidev omnigent` plus the Omnigent UI and admitted terminal, and P3 or P4 use documented Kit commands. Read logs, bridge snapshots, and binding records without mutating them.
- [ ] Save every screenshot to `/tmp/swarm-"$PR_ID"/worker-"$LANE_NUMBER"/"$LANE_SLUG".png` and return the paths with the report. Save review videos under the same untracked `/tmp` directory.

## Establish the Prime-owned bridge (P1-Prime)

**Depends on.** None. Run this proposed PR in a future Prime fork on branch `prime-native-bridge`. Its reviewed base is Prime `2d24ad4e6b2d1ee8e6919af6f108e980a14d550e`.

**Files.**

- [ ] Edit existing `packages/coding-agent/src/main.ts`, `packages/coding-agent/src/modes/acp/acp-mode.ts`, `packages/coding-agent/src/modes/acp/acp-events.ts`, `packages/coding-agent/src/modes/acp/acp-meta.ts`, and `packages/coding-agent/src/modes/daemon/daemon-protocol.ts` after confirming them at the selected Prime base.
- [ ] Create proposed `packages/coding-agent/src/modes/omnigent_bridge/` for version negotiation, stable root attachment, controller ownership, command journaling, snapshot reconciliation, and engine policy interception.
- [ ] Create proposed focused bridge tests beside the selected Prime ACP and daemon test suites. Do not edit Omnigent or Kit files in P1-Prime.

**Build.**

- [ ] Keep `AgentConnection` and daemon protocol types private to Prime. Expose a versioned bridge record with root identity, worker generation, event cursor, models, effort, command state, extension UI, goals, gates, heartbeat, schedule, refinement, rich events, liveness, and MCP availability.
- [ ] Provide root-scoped controller lease enforcement for native terminal and Omnigent mutations. Keep viewers concurrent. Distinguish detach, cancel admission, cancel turn, and stop root. Preserve scheduled work through Prime's queue.
- [ ] Make worker generation and cursor gaps request authoritative snapshots. Discard stale events. Journal accepted and uncertain commands so reconnect never replays an uncertain prompt. Report kernel reset explicitly.
- [ ] Intercept allow, deny, and approval at the execution engine for the root tree. A deny must block Python, shell, MCP, RLM children, and descendants before side effects.

**You see.**

- [ ] A bridge admission reports a stable root, protocol version, and controller owner. A denied marker write returns denial and creates no marker. A disconnect leaves a resident root available for reattachment when policy permits it.

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Add Prime tests for protocol negotiation, root identity, controller lease, detach and cancellation outcomes, event duplicates, cursor gaps, generation recovery, command journaling, unavailable MCP, and execution-gate denial. Run the Prime repository's documented focused test command after reading its contributor guide.
- [ ] Run the Prime repository's documented lint, typecheck, and pre-commit commands. Failure is any non-zero exit, a mutation by a viewer, stale data that replaces a snapshot, a replayed uncertain command, or a denied marker that exists.

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `sonnet` at the PR head, per the boot recipe.

- [ ] Lane 1. Regression lane against Prime trunk. Start ordinary daemon-backed ACP at trunk and bridge admission at head. Trunk lacks the bridge. Record that fact and gate stable identity and detach behavior at head. Save `p1-prime-regression.png`. Pass when head binds one resident root and records a bridge version.
- [ ] Lane 2. Start a root with an authenticated Prime configuration. Save `p1-prime-model-effort.png`. Pass when bridge discovery reports only Prime-advertised models and effort.
- [ ] Lane 3. Attach two viewers and one controller. Save `p1-prime-controller.png`. Pass when only the controller can submit a mutation and takeover is visible.
- [ ] Lane 4. Detach the controller while a root stays resident. Save `p1-prime-detach.png`. Pass when the worker and active root remain observable without a duplicate worker.
- [ ] Lane 5. Write a Python value, detach, and attach a replacement bridge. Save `p1-prime-kernel.png`. Pass when the replacement reads the same value from the same live root.
- [ ] Lane 6. Force a worker replacement. Save `p1-prime-recovery.png`. Pass when the bridge publishes a new generation and an explicit kernel-reset status without replaying the accepted input.
- [ ] Lane 7. Inject duplicate events and a cursor gap. Save `p1-prime-event-recovery.png`. Pass when duplicates do not reappear and the gap produces an authoritative snapshot.
- [ ] Lane 8. Deny a marker write from an admitted terminal. Save `p1-prime-terminal-deny.png`. Pass when the execution gate denies it and the marker does not exist.
- [ ] Lane 9. Deny the same marker write from the Prime-side protocol conformance driver acting as an external bridge client. Save `p1-prime-external-deny.png`. Pass when the execution gate denies it and the marker does not exist. The real Omnigent projection is verified in P1-Omni.
- [ ] Lane 10. Disconnect an approved MCP relay while the root continues. Save `p1-prime-mcp-unavailable.png`. Pass when dependent work receives typed unavailable status instead of a stale usable tool.

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. Measure ordinary Prime daemon-backed ACP startup at trunk and head. Measure head-only bridge admission, reattach, and snapshot delivery separately.
- [ ] Probe. Interleave five cold ordinary ACP starts at trunk and head. At head run five bridge admissions, reattachments, and fixed snapshot deliveries with recorded host load and version data.
- [ ] Baseline. Record trunk ordinary ACP startup samples first. Record bridge work as an additional head-only measure.
- [ ] Rule. Proposed initial policy budgets need calibration before implementation. Head ordinary ACP startup must not exceed trunk median by more than 10 percent. Bridge admission and reattach must each be at most 3 seconds and the fixed snapshot delivery at most 1 second. Any excess fails until recalibrated from recorded samples.

**Review gate.** The operator reviews before merge.

- [ ] Copy lane 9 screenshots into `/tmp/prime-native-review/P1-Prime-review-deny.png`.
- [ ] Record a 30 to 60 second video of controller handoff, detach, reattach, and engine denial on a lane VM. Save it as `/tmp/prime-native-review/P1-Prime-review.mp4`.
- [ ] Post the screenshots and the video in chat. Stop at merge-ready. Wait for the operator's click.

**Merge.**

- [ ] Root's clean verdict at the exact P1-Prime head SHA.
- [ ] Bugbot triage done.
- [ ] Rebased onto the current Prime trunk after the verdict, patch-id unchanged.
- [ ] The operator lands P1-Prime in the Prime repository. The PR owner records its merged SHA for P1-Omni.

## Establish the Omnigent native vertical slice (P1-Omni)

**Depends on.** P1-Prime merged SHA recorded in the P1-Omni PR body. P1-Omni runs only in this fork on branch `prime-native-vertical-slice`.

**Files.**

- [ ] Edit existing `omnigent/harness_plugins.py`, `omnigent/native/native_coding_agents.py`, `omnigent/runner/resource_registry.py`, `omnigent/runner/native/orchestration.py`, and `omnigent/runtime/prompt.py`.
- [ ] Create `omnigent/harnesses/prime_native/` for the native provider, launch, bridge client, event mapping, and lifecycle state.
- [ ] Create proposed `tests/harnesses/test_prime_native_bridge.py` and extend existing `tests/test_harness_plugins.py`, `tests/runner/test_app_sessions_native_events_lifecycle.py`, and `tests/runner/test_native_terminal_lock_coverage.py` when their current seams still match.
- [ ] Create proposed `tests/fixtures/prime_native/resources/` with a distinctive append-system identity, text skill, Python-backed skill, dependency manifest, and literal expected resource digest. This Omnigent-local contract fixture supplies P1-Omni before the Kit compiler exists.
- [ ] Create or qualify proposed `.claude/skills/verify-omnigent/SKILL.md` for the real native UI, terminal, and bridge verification driver.

**Build.**

- [ ] Add the core `prime-native` contribution with `prime-native-ui`, install data, capabilities, and harness-specific model discovery. Wire native provider fields `run_native`, `auto_create_terminal`, `spawn_env_builder`, `materialize_agent_spec`, `interrupt_handler`, `stop_handler`, and `bridge_dir`. The native integration supplies bridge attachment. Do not use the community native loader while its reviewed admission rule rejects native contributions.
- [ ] Consume only the P1-Prime versioned bridge contract. Validate its required capability version and root binding before launch. The Prime-side bridge owns `AgentConnection` adaptation, execution policy, controller lease, and generation recovery.
- [ ] Prove the smallest real path before broad migration. An Omnigent declared role attaches to one P1-Prime resident worker, loads the Omnigent-local resource contract fixture, presents `APPEND_SYSTEM.md` identity and skills, routes an Omnigent turn and native terminal attach to that root, and observes the Prime engine deny result for both clients. Record its expected digest and identity marker. P3 later proves Kit-generated delivery against this accepted schema.
- [ ] Keep Prime RLM descendants in the Prime tree. Keep declared Prime, Codex, and Claude roles as separate Omnigent sessions. Keep framework instructions canonical in their owning modules and compose them in `omnigent/runtime/prompt.py`. Do not add lifecycle fields to `AgentSpec`.

**You see.**

- [ ] The native catalog lists `prime-native` and `prime-native-ui`. A fresh admitted root shows the bridge-provided root identity, resource digest, bridge version, worker generation, controller owner, and current tool availability. A denied marker write leaves no marker on disk.

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Add unit cases for native registry lookup, no generic ACP fallback, incompatible bridge rejection, root binding validation, controller lease transitions, event duplicate and gap reconciliation, explicit unavailable-tool status, and engine-side deny propagation. After creating the proposed bridge suite, run `uv run --no-sync pytest tests/harnesses/test_prime_native_bridge.py tests/test_harness_plugins.py tests/runner/test_app_sessions_native_events_lifecycle.py tests/runner/test_native_terminal_lock_coverage.py`. A missing suite, zero collected tests, or any non-zero exit fails this gate.
- [ ] Run `uv run --no-sync ruff check . && uv run --no-sync ruff format --check . && uv run --no-sync pyrefly check`. Failure is any non-zero exit or a test that accepts a generic route, replays an uncertain prompt, or reports denial after a marker exists.

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `sonnet` at the PR head, per the boot recipe.

- [ ] Lane 1. Regression lane against trunk. Start the load-bearing native catalog and terminal scenario at trunk and head. Trunk lacks Prime. Record that fact and gate N01, N02, N06, and N20 at head. Save `p1-registry-regression.png`. Pass when head lists only the explicit Prime contribution and the denied marker is absent.
- [ ] Lane 2. Start a root with the pinned bridge and authenticated Prime configuration. Save `p1-readiness-model.png`. Pass when missing or incompatible versions fail before a turn and the reported model and effort came from Prime.
- [ ] Lane 3. Start a root with a distinctive Kit identity addition. Save `p1-identity.png`. Pass when Prime base instructions and the Kit release marker both appear in the observed root state.
- [ ] Lane 4. Execute one distinctive text skill and one Python-backed skill from the immutable resource artifact. Save `p1-skills.png`. Pass when both produce their expected literal outputs and the root reports the artifact digest.
- [ ] Lane 5. Attach the Omnigent terminal to the admitted Prime root. Save `p1-terminal-attach.png`. Pass when the native view names the same root and execution host as the binding record.
- [ ] Lane 6. Write a Python value through the Omnigent client, detach, and reattach through the native terminal. Save `p1-kernel-continuity.png`. Pass when the exact value remains readable from the same living worker.
- [ ] Lane 7. Attach a read-only viewer while a controller owns the root. Save `p1-controller-lease.png`. Pass when the viewer reads state and its mutation is rejected with the controller owner shown.
- [ ] Lane 8. Attempt a denied marker write from the native terminal. Save `p1-terminal-deny.png`. Pass when the terminal receives the deny result and the marker does not exist.
- [ ] Lane 9. Attempt the same denied marker write from Omnigent. Save `p1-omnigent-deny.png`. Pass when Omnigent receives the same deny result and the marker does not exist.
- [ ] Lane 10. Disconnect a required MCP relay and request its tool from the continuing root. Save `p1-tool-unavailable.png`. Pass when Prime receives a typed unavailable result and the UI exposes the loss.

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. Measure Omnigent pod readiness at trunk and head. Measure head-only native registration, bridge admission, and initial terminal attach as separate work because trunk has no Prime route.
- [ ] Probe. Interleave five cold runs at trunk and head with the same `omnidev` startup procedure. At head, run five isolated bridge admission and terminal attaches. Record wall-clock samples, host load, Prime version, and bridge version.
- [ ] Baseline. Record the trunk pod-ready values first. Record the head Prime measurements without comparing unlike scenarios.
- [ ] Rule. Proposed initial policy budgets need calibration before implementation. Head pod readiness must not exceed trunk median by more than 10 percent. Head bridge admission must be at most 3 seconds and initial terminal attach at most 5 seconds. Any excess fails until the operator accepts a recalibrated policy with fresh measurements.

**Review gate.** The operator reviews before merge.

- [ ] Copy lane 8 screenshots into `/tmp/prime-native-review/P1-review-terminal-deny.png`.
- [ ] Record a 30 to 60 second video of the attach, controller lease, and denial on a lane VM. Save it as `/tmp/prime-native-review/P1-review.mp4`.
- [ ] Post the screenshots and the video in chat. Stop at merge-ready. Wait for the operator's click.

**Merge.**

- [ ] Root's clean verdict at the exact P1-Omni head SHA.
- [ ] Bugbot triage done.
- [ ] Rebased onto current Omnigent trunk after the verdict, patch-id unchanged.
- [ ] The operator lands P1-Omni in this fork after review. The PR owner records its merged SHA for P2 and P3.

## Render Prime controls and composition (P2)

**Depends on.** Exact verified P1-Omni head SHA recorded in the P2 body. P2 runs only in this fork on branch `prime-native-controls` and targets `prime-native-vertical-slice` until that parent lands. Then record the P1-Omni merged SHA and restack onto `main` before P2 lands.

**Files.**

- [ ] Edit existing P1-Omni Prime native provider modules, `omnigent/runner/app.py`, `omnigent/runner/resource_registry.py`, and `omnigent/runner/native/orchestration.py`. Locate and name the existing web or TUI render files in the P2 PR before editing them.
- [ ] Create proposed bridge schema fixtures and UI tests after P1-Omni accepts the P1-Prime bridge contract.
- [ ] Do not edit Kit or Prime files. A newly discovered bridge gap opens a named repository-local Prime follow-up PR before P2 consumes it.

**Build.**

- [ ] Render authoritative bridge snapshots for lifecycle state, kernel reset, controller ownership, models and effort, commands, extension dialogs, Python cells, images, MIME attachments, diffs, errors, RLM lineage, goals, heartbeat, schedules, gates, compaction, refinement, and terminal quiescence.
- [ ] Reconcile cursor gaps or new generations with an authoritative snapshot. Discard stale events. Do not reject every generation change. Keep accepted and uncertain prompts unreplayed until Prime resolves them.
- [ ] Add declared root and child composition for Prime, Codex, and Claude. Keep Prime recursive descendants out of declared-role configuration. Implement separate detach, admission cancellation, turn cancellation, and root stop outcomes.

**You see.**

- [ ] A Prime root and a declared child show their own bindings. A RLM descendant shows Prime parent lineage. A worker recovery displays the new generation and kernel-reset visibility without a duplicate transcript entry.

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Add cases for schema-version negotiation, rich event correlation, snapshot replacement, duplicate and stale events, generation changes, controller takeover, command rejection, descendant origin, and distinct cancellation operations. Run `uv run --no-sync pytest tests/runner/` after adding the focused cases. Missing cases, zero collected tests, or any non-zero exit fails.
- [ ] Run `uv run --no-sync ruff check . && uv run --no-sync ruff format --check . && uv run --no-sync pyrefly check`. Failure is any non-zero exit, an event rendered twice, a stale event replacing a snapshot, or an RLM child materialized as a declared role.
- [ ] If P2 edits `web/`, add a colocated Vitest test and a covering Playwright test under `tests/e2e_ui/` for the user-facing controls. Run `cd web && pnpm install && pnpm run lint && pnpm run type-check && pnpm run build && pnpm test` in a separate shell rooted at the repository, then run the documented Python Playwright suite from the repository root. A non-zero exit, missing covering test, zero collected tests, or incorrect rendered control state fails. A TUI-only change retains the Python checks and records why the web gate is inapplicable.

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `sonnet` at the PR head, per the boot recipe.

- [ ] Lane 1. Regression lane against the exact verified P1-Omni parent head. Run its root attach and denied-tool scenario at parent and P2 head. The parent lacks P2 controls. Record that fact and gate the new control state plus the user-visible settlement at P2 head. Save `p2-controls-regression.png`. Pass when P1-Omni behavior remains and P2 renders an authoritative root state. Run the common native catalog scenario at trunk separately.
- [ ] Lane 2. Drive Python cell output, text, an image, a MIME attachment, a diff, and an error through one root. Save `p2-rich-output.png`. Pass when each output keeps its event correlation and renders once.
- [ ] Lane 3. Trigger a bridge cursor gap. Save `p2-cursor-gap.png`. Pass when the UI requests and applies a snapshot without duplicate transcript entries.
- [ ] Lane 4. Restart the worker after a Python value exists. Save `p2-generation-recovery.png`. Pass when the UI shows a new generation and possible lost kernel state without replaying the accepted prompt.
- [ ] Lane 5. Run a Prime root with a declared Codex child and a declared Claude child. Save `p2-mixed-roles.png`. Pass when each declared role has its own session and binding.
- [ ] Lane 6. Spawn a Prime RLM descendant from the same root. Save `p2-rlm-lineage.png`. Pass when it has Prime root and parent lineage and no Kit role definition.
- [ ] Lane 7. Start a goal, heartbeat, schedule, gate, compaction, and refinement. Save `p2-long-running-controls.png`. Pass when the controls remain visible and actions use the Prime owner.
- [ ] Lane 8. Leave a root waiting for a scheduled input beyond the generic ACP inactivity threshold. Save `p2-quiet-liveness.png`. Pass when the root stays healthy with its next wake-up source visible.
- [ ] Lane 9. Exercise detach, cancel admission, cancel turn, and stop root in four isolated roots. Save `p2-lifecycle-actions.png`. Pass when each action has its specified distinct result.
- [ ] Lane 10. Complete a turn while a child settles. Save `p2-terminal-quiescence.png`. Pass when Omnigent marks completion only after the Prime terminal-quiescence policy is true.

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. Measure common Omnigent pod readiness at trunk and head. Measure head-only snapshot application latency, rich-event rendering latency, and terminal-quiescence settlement delay.
- [ ] Probe. Interleave five trunk and head pod starts. At head inject recorded bridge events at fixed sizes, then time snapshot application and quiescence after the final descendant settles.
- [ ] Baseline. Record the trunk pod-ready values first. Record head control measurements separately because trunk lacks Prime controls.
- [ ] Rule. Proposed initial policy budgets need calibration before implementation. Head pod readiness must not exceed trunk median by more than 10 percent. Head snapshot application must be at most 1 second for the recorded fixture and settlement display must be at most 2 seconds after the authoritative quiescence event. Any excess fails until recalibrated with recorded samples.

**Review gate.** The operator reviews before merge.

- [ ] Copy lane 7 screenshots into `/tmp/prime-native-review/P2-review-long-running-controls.png`.
- [ ] Record a 30 to 60 second video of mixed roles, a RLM child, and quiescent settlement on a lane VM. Save it as `/tmp/prime-native-review/P2-review.mp4`.
- [ ] Post the screenshots and the video in chat. Stop at merge-ready. Wait for the operator's click.

**Merge.**

- [ ] Root's clean verdict at the exact P2 head SHA.
- [ ] Bugbot triage done.
- [ ] Rebased onto current Omnigent trunk after the verdict, patch-id unchanged.
- [ ] The operator lands P2 above P1-Omni after review. The PR owner records its merged SHA for P3 and P4.

## Add optional Kit resources and routes (P3)

**Depends on.** P1-Prime, P1-Omni, and P2 merged SHAs recorded in the P3 body. P3 runs only in a future Kit repository branch `prime-native-resources`.

**Files.**

- [ ] Edit a future Kit branch only. Start with `profile_kit/release.py`, `profile_kit/native.py`, `profile_kit/lifecycle.py`, `profile_kit/state.py`, `profile_kit/inspection.py`, route validation, and catalog materialization after confirming their current locations at the recorded Kit revision.
- [ ] Create a future Kit Prime adapter, deterministic Prime resource renderer, receipt schema migration, route registry entry, and proposed targeted tests.
- [ ] Do not change Omnigent's default or Hermes defaults. Do not create the Kit branch from this Omnigent fork.

**Build.**

- [ ] Add an optional `prime` target. Migrate existing installations with Hermes and Omnigent still selected. Add Prime only when selected. Make root and child route descriptors accept `prime-native` and preserve declared-role composition.
- [ ] Compile one immutable resource artifact for identity additions, skills, Python dependencies, and MCP declarations. Reproduce the P1-Omni resource fixture's accepted schema and digest semantics at its exact merged revision. Deliver the same digest to direct Prime and Omnigent and repeat N04 and N05 with Kit-generated output. Preserve Prime authentication, session files, kernel state, schedules, and learned state outside releases.
- [ ] Make the lifecycle, receipt, inspection, conflict, retry, rollback, removal, recovery, and resource retention target-aware. Keep Prime resource changes for new roots until an explicit migration or restart rule exists.

**You see.**

- [ ] A selected profile records the optional Prime route and its resource digest. An unselected existing profile has no Prime target. A live binding retains its old resource digest across a later release.

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Add tests for state migration, optional target selection, source collision, deterministic digest equality, root and child route validation, private-state exclusion, conflict preview, partial Prime retry, rollback, removal, recovery, and resource retention. Run the focused test command documented by the Kit `mise.toml` or its current contributor guide.
- [ ] Run the Kit repository's documented lint, typecheck, and `pre-commit run --all-files`. Failure is any non-zero exit, a changed Hermes or Omnigent default, a release that contains private state, or a live binding whose referenced resources can be retired.

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `sonnet` at the PR head, per the boot recipe.

- [ ] Lane 1. Regression lane against trunk. Build and inspect the existing Hermes and Omnigent profile on trunk and head. Trunk lacks Prime. Record that fact and gate optional Prime selection plus unchanged default artifacts at head. Save `p3-defaults-regression.png`. Pass when default outputs and inspection facts stay unchanged and Prime appears only when selected.
- [ ] Lane 2. Migrate an existing Kit installation without selecting Prime. Save `p3-state-migration.png`. Pass when Hermes and Omnigent retain their receipts and Prime is absent.
- [ ] Lane 3. Select Prime and build direct Prime and Omnigent routes from one profile. Save `p3-digest-equality.png`. Pass when both bindings report the same resource digest.
- [ ] Lane 4. Run a distinctive text skill and a Python-backed skill from a selected Prime profile. Save `p3-resource-skills.png`. Pass when both exhibit their literal expected behavior through direct Prime and Omnigent.
- [ ] Lane 5. Start a root, upgrade the profile resources, and inspect the living root. Save `p3-resource-retention.png`. Pass when the root retains its original digest and the new digest applies only to a fresh root.
- [ ] Lane 6. Inspect a release and a demo capture for private state. Save `p3-private-state.png`. Pass when no credential, transcript, schedule, learned state, or kernel content appears.
- [ ] Lane 7. Modify a managed Prime resource locally and request a preview. Save `p3-conflict-preview.png`. Pass when the lifecycle reports the conflict before a write.
- [ ] Lane 8. Force the Prime target to fail after other selected targets succeed, then retry. Save `p3-partial-retry.png`. Pass when retry changes only Prime and `good_release` advances only after the selected targets succeed.
- [ ] Lane 9. Roll back, remove, and recover a selected Prime target. Save `p3-rollback-recovery.png`. Pass when the adapter restores the verified managed resource tree and preserves private Prime state.
- [ ] Lane 10. Start a Prime root with declared Prime, Codex, and Claude children from a selected Kit profile. Save `p3-root-child-routes.png`. Pass when root and children receive their expected independent route bindings.

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. Measure common existing-profile build and inspection time at trunk and head. Measure head-only selected-Prime compilation, installation preview, and route materialization time separately.
- [ ] Probe. Interleave five clean builds and inspections at trunk and head using the same profile with Prime unselected. At head run five selected-Prime builds from the same source profile and record digest computation, preview, and materialization durations.
- [ ] Baseline. Record trunk build and inspection samples first. Record selected-Prime measurements as additional work rather than a ratio to trunk.
- [ ] Rule. Proposed initial policy budgets need calibration before implementation. Head default build and inspection medians must not exceed trunk by more than 10 percent. Selected-Prime compile, preview, and route materialization must each be at most 5 seconds for the recorded fixture. Any excess fails until recalibrated from recorded samples.

**Review gate.** The operator reviews before merge.

- [ ] Copy lane 3 screenshots into `/tmp/prime-native-review/P3-review-digest-equality.png`.
- [ ] Record a 30 to 60 second video of optional selection, artifact inspection, and resource retention on a lane VM. Save it as `/tmp/prime-native-review/P3-review.mp4`.
- [ ] Post the screenshots and the video in chat. Stop at merge-ready. Wait for the operator's click.

**Merge.**

- [ ] Root's clean verdict at the exact P3 head SHA.
- [ ] Bugbot triage done.
- [ ] Rebased onto current Kit trunk after the verdict, patch-id unchanged.
- [ ] The operator lands P3 in the Kit repository after review. The PR owner records its merged SHA for P4.

## Qualify controlled launch and upgrades (P4)

**Depends on.** Exact verified P3 head SHA plus the recorded qualified P1-Prime, P1-Omni, and P2 SHAs. P4 runs only in the Kit repository on branch `prime-native-qualified-launch` and targets `prime-native-resources` until P3 lands. Then record the P3 merged SHA and restack onto `main` before P4 lands.

**Files.**

- [ ] Edit existing `profile_kit/launch.py`, `profile_kit/native_guard.py`, `profile_kit/sbx_runtime/Dockerfile`, `profile_kit/lifecycle.py`, `profile_kit/release.py`, and `profile_kit/inspection.py` after confirming them at the P3 base.
- [ ] Create proposed Kit controlled Prime launcher, guest bootstrap, SBX image entries, engine-side permission probes, teardown probes, compatibility receipt, and upgrade qualification driver.
- [ ] Delete compatibility patches only after their upstream retirement test passes. Do not delete a patch because a newer version exists.

**Build.**

- [ ] Bind a controlled session to a private Prime state root, approved binary, admitted daemon endpoint, canonical workspace, resource digest, and policy digest. Reject unbound arguments, endpoints, roots, and cross-client mutation without a controller lease.
- [ ] Use the P1-Prime engine policy capability and P2 native route. Place the actual Prime worker, Python kernel, shell processes, MCP processes, RLM children, and descendants in the execution boundary. Permit only the admitted workspace and admitted private-state paths. Prove filesystem and network denial at the execution engine rather than from UI records. A new enforcement gap opens a named repository-local Prime or Omnigent follow-up PR.
- [ ] Pin Prime, bridge, Python dependencies, and source patches in Kit configuration. Record source digest, behavioral check, compatible version range, and tested retirement condition. Qualify upgrades through the root, child, mixed-role, recovery, and sandbox matrix before Kit support records advance.

**You see.**

- [ ] The controlled guest identifies its private worker and endpoint. A host daemon cannot attach. A denied filesystem or network action creates no side effect from any root descendant. Teardown leaves no owned process in the boundary.

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Add engine-side test cases for allow, deny, approval, inherited policy, endpoint binding, sandbox worker placement, filesystem deny, network deny, descendant deny, teardown, patch input digest, and upgrade receipt promotion. Run the documented Kit and Omnigent focused test commands after the owner establishes their exact paths.
- [ ] Run the documented lint, typecheck, and pre-commit commands in every changed repository. Failure is any non-zero exit, a policy decision with a side effect, a host attachment from a guest, an orphan process after teardown, or a support record advanced without matrix evidence.

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `sonnet` at the PR head, per the boot recipe.

- [ ] Lane 1. Regression lane against the exact verified P3 parent head. Run P3 resource delivery and the pinned P2 attached-root scenario at parent and P4 head. The parent lacks controlled Prime launch. Record that fact and gate P4 binding, confinement, and teardown behavior at P4 head. Save `p4-controlled-regression.png`. Pass when resource delivery and P2 remain functional and P4 enforces its admitted binding. Run existing controlled Codex or Claude launch at Kit trunk separately.
- [ ] Lane 2. Launch a controlled root in SBX and inspect worker, kernel, shell, MCP, child, and grandchild process locations. Save `p4-worker-confinement.png`. Pass when every owned process runs in the admitted boundary.
- [ ] Lane 3. Attempt a forbidden host-file read from the root Python kernel. Save `p4-root-filesystem-deny.png`. Pass when the read fails and no forbidden contents reach output.
- [ ] Lane 4. Attempt the same forbidden read from a RLM descendant and a shell child. Save `p4-descendant-filesystem-deny.png`. Pass when both fail with no forbidden contents.
- [ ] Lane 5. Call one permitted network endpoint from a root and descendant. Save `p4-network-allow.png`. Pass when both receive the expected harmless response.
- [ ] Lane 6. Call one forbidden network endpoint from a root and descendant. Save `p4-network-deny.png`. Pass when both fail before an outbound connection completes.
- [ ] Lane 7. Attempt an unbound endpoint, root, and extra argument in separate controlled launches. Save `p4-endpoint-binding.png`. Pass when all three are rejected before a worker starts.
- [ ] Lane 8. Exercise allow, deny, and approval from an Omnigent client and native terminal against one root. Save `p4-policy-clients.png`. Pass when both clients share the execution policy and the denied marker does not exist.
- [ ] Lane 9. Stop a root with a running MCP process and RLM descendant, then inspect the guest. Save `p4-teardown.png`. Pass when no owned worker, kernel, MCP process, or descendant remains.
- [ ] Lane 10. Upgrade the pinned Prime, bridge, or Omnigent candidate in a disposable matrix run. Save `p4-upgrade-matrix.png`. Pass when root, child, mixed-role, recovery, and sandbox scenarios pass before the support receipt changes.

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. Measure existing Kit controlled Codex or Claude launch readiness at Kit trunk and P4 head. Measure head-only controlled Prime guest launch, policy-decision round trip, and teardown completion separately.
- [ ] Probe. Interleave five existing controlled Codex or Claude launches at Kit trunk and head with the same fixture. At head run five controlled Prime launches with one allow and one deny, then measure from launch request to ready and from stop request to no-owned-process observation.
- [ ] Baseline. Record Kit trunk controlled Codex or Claude readiness values first. Record the controlled Prime guest measurements as additional work because Kit trunk lacks P4.
- [ ] Rule. Proposed initial policy budgets need calibration before implementation. Head existing controlled Codex or Claude readiness median must not exceed Kit trunk by more than 10 percent. Controlled Prime launch must be at most 30 seconds, policy decision round trip at most 2 seconds, and teardown at most 10 seconds for the recorded guest fixture. Any excess fails until recalibrated from recorded samples.

**Review gate.** The operator reviews before merge.

- [ ] Copy lane 8 screenshots into `/tmp/prime-native-review/P4-review-policy-clients.png`.
- [ ] Record a 30 to 60 second video of controlled launch, a denied marker, and verified teardown on a lane VM. Save it as `/tmp/prime-native-review/P4-review.mp4`.
- [ ] Post the screenshots and the video in chat. Stop at merge-ready. Wait for the operator's click.

**Merge.**

- [ ] Root's clean verdict at the exact P4 head SHA.
- [ ] Bugbot triage done.
- [ ] Rebased onto current Kit trunk after the verdict, patch-id unchanged.
- [ ] The operator lands P4 above P3 after review. The operator lands the clean Kit stack bottom-up.

## Close the program

- [ ] Every box above is checked with its evidence.
- [ ] If the program owns `PSTACK_TEMP_ROOT`, remove it with the cleanup block in the **pstack-harness** skill. Never remove an installed root or an explicit checkout.
- [ ] Reply to the operator with the report the execution playbook names.

## Appendix A. Prototype evidence

No prototype, Prime login, worker launch, live model call, controlled launch, or end-to-end acceptance has run on this design branch. P1-Prime proves the Prime-side contract and engine-policy part of N02, N03, N07 through N11, N19, and N20. P1-Omni proves N01, N04 through N07, and the Omnigent projection of N19 and N20. P2 proves N08 through N18. P3 proves K01 through K05. P4 proves S01 through S04, M01, and M02. Each owner records its future branch, SHA, screenshots, videos, and exact failure signals here before claiming an acceptance ID.

## Appendix B. Alternatives rejected

Generic ACP alone loses the native provider, durable root binding, lifecycle-aware liveness, controller lease, rich Prime metadata, and execution-policy proof required by [CONTRACT.md](CONTRACT.md). An external native contribution is not the initial package because the reviewed Omnigent community loader blocks native agents and harnesses. A new `acp-daemon` mode is not assumed because the reviewed Prime ordinary ACP route is daemon-backed. A Kit-only route cannot create the Prime-side execution gate or the Omnigent native projection.

## Appendix C. Risks

The project has no `verify-omnigent` control skill. P1-Omni creates or qualifies a control skill and records its driver. Until then, each live lane uses the documented `omnidev`, UI, CLI, and admitted terminal procedure. The community native contribution loader blocks the intended contribution shape at the reviewed Omnigent revision. P1-Omni uses a core module in this fork and records the upstream replacement condition. Prime bridge encoding and its command inventory remain unproven. P1-Prime must pin the external protocol and fail on incompatible versions. Full policy and confinement remain unproven until P4 shows engine-side interception for root and descendant execution. The performance budgets are proposed initial policy values and require calibration before implementation.

## Appendix D. Links and reading list

Read [README.md](README.md), [DESIGN.md](DESIGN.md), and [CONTRACT.md](CONTRACT.md) before editing. P1-Prime uses `"$PSTACK_SKILLS_ROOT/how/SKILL.md"` for bridge placement and `"$PSTACK_SKILLS_ROOT/interrogate/SKILL.md"` for the execution-policy boundary. P1-Omni and P2 use them for native provider placement and presentation. P3 uses them for the Kit target registry and resource ownership. P4 uses them for the execution boundary and upgrade receipt. Record the program decision trail through `"$PSTACK_SKILLS_ROOT/show-me-your-work/SKILL.md"`.

At the reviewed Omnigent revision, start with `omnigent/harness_plugins.py`, `omnigent/native/native_coding_agents.py`, `omnigent/native/native_dispatch.py`, `omnigent/runner/app.py`, `omnigent/runner/native/orchestration.py`, `omnigent/runner/resource_registry.py`, and `omnigent/runtime/prompt.py`. Reconfirm the current `AGENTS.md`, `CONTRIBUTING.md`, `justfile`, `pyproject.toml`, and CI before selecting commands. Prime source changes use a future Prime fork or branch. Kit changes use a separate future Kit branch. This Omnigent fork contains no Prime or Kit runtime implementation.
