Status: rejected, see DESIGN.md

# Rotate the Omnigent session on Prime `/new` (design only, not implemented)

## The bug, and the finding that changes the plan

After `/new`, or `/resume` to another Prime conversation, the bridge follows the TUI (`session_bridge.py:190-200`)
but keeps posting to the session named in `config.json` (`:85-88`). Its PATCH of `external_session_id` (`:213-218`)
gets a 400 from the write-once rule (`sqlalchemy_store.py:4509`, raised at `routes_core.py:3334`), and the bridge
never checks the status. One web session then holds two Prime conversations, usage keeps summing across both
(`transcript.py:305-310`), and resume reopens the first.

The approved fix copies codex `/new` (`codex_native/forwarder.py:2420-2525`), which moves the terminal with
`POST .../terminals/{id}/transfer`. This fork refuses that move for every native terminal.
`resource_registry.py:1996-2002` raises CONFLICT "Native terminals cannot move between sessions because Stop
requires their original source owner" for `codex`, `claude` and `prime-native`. Commit 787f74bfc added it after
v0.18.0, and `tests/runner/test_session_resources.py:928` pins it for Prime. So rotation cannot live in the bridge
alone. It has to change who owns the runtime. The same guard probably breaks codex `/new` and claude `/clear` in
this fork today, since both call `raise_for_status` on the transfer (`forwarder.py:2496`, `claude_native/hook.py:548`).
That is inferred from code and was not run.

## How the runtime is keyed today

- The runtime root is `sha256(session id)` under the bridge roots (`prime_native/bridge.py:141-158`). Every Prime
  lookup derives from it. That includes the spawn env `HARNESS_PRIME_NATIVE_BRIDGE_DIR` (`bridge.py:161`, hooked at
  `harness_plugins.py:348`) and the provider `bridge_dir` hook (`harness_plugins.py:353`), which `native_owner` uses
  for admission (`native/admission.py:30-43,73`). It also includes the message gate (`runner/app.py:6631`), interrupt
  (`controls.py:211-213`), Stop (`process.py:1140-1141`), delete cleanup (`orchestration.py:9432-9434`) and launch
  (`main.py:252`).
- `write_extension_files` (`pi_native/bridge.py:339`) writes `config.json` once per launch with `sessionId`,
  `serverUrl`, `inboxDir`, `authHeaders` and `nativeAdmission`. The bridge reads `sessionId` and `inboxDir` once
  (`session_bridge.py:81-88`) and re-reads only `authHeaders` (`:483-489`).
- A web message to session X goes through `app.py:6618-6641`, which admits X, ensures X's terminal and waits for a
  live `binding.json`. The per-session executor (`inner/prime_native_harness.py:10`) then writes
  `inbox/msg_<uuid>.json` with `enqueue_user_message` (`pi_native/bridge.py:189`). Model, effort and compact go to
  `controls/requests/` through `PrimeExtensionBinding(admission.owner.runtime)` (`app.py:1362-1373`).
- The owner claim `owner.pid` and the bridge's `binding.json` heartbeat (`session_bridge.py:353-375`) are files in
  the root. The terminal is `terminal_prime-native_main`, registered under X (`main.py:367-373`).
- A message to a new session Y finds no `(Y, prime-native, main)` terminal, so `_ensure_native_terminal_for_turn`
  (`resource_routes.py:646`) launches a second runtime. A launch onto a live root stops it (`process.py:705-712`).
- Codex reaches its app-server from a rotated session through the label `omnigent.codex_native.bridge_id`
  (`codex_native/bridge.py:33`), which every rotation inherits. A bridge state file names the live session, and the
  runner refuses any other (`runner/app.py:3690`). Prime needs the same two facts. Every Prime lookup above is
  synchronous and takes only a session id, and threading labels through `native_owner` and the provider hooks would
  touch every native provider. So Prime keeps both facts in local files.

## Data shape

A runtime root R belongs to its launch session X. Every session rotated out of R shares R.

```
<R>/lineage.json                        written only by the bridge
  {"active": "conv_Y", "bound": {"<Prime sessionId S1>": "conv_X", "<S2>": "conv_Y"}}
<bridge root>/aliases/<sha256(Y)[:32]>  contains "conv_X"; the bridge writes it, deleting Y removes it
```

- `bridge_dir_for_session_id(Y)` reads the alias first. Paths, `native_owner`, spawn env, Stop and interrupt for Y
  then resolve to R with no other change. Y's admission names R as owner, so Stop on Y closes the real process.
- `holder(session)` in `prime_native/bridge.py` returns `lineage.active` while R's owner claim is live, else `None`.
  Runner entry points refuse a non-holder with a 409 that names the holder. They are the message branch before
  `_ensure_native_terminal_for_turn` (`app.py:6618`), a Prime `pre_launch` in `_launch_native_terminal`
  (`orchestration.py:9815`, as codex does at `:10268`), `_execute_prime_control`, `interrupt_session`, and Stop.
  Delete cleanup removes a non-holder's alias and never stops R.
- The transfer guard (`resource_registry.py:1996`) allows a native move when the provider `bridge_dir` hook resolves
  source and target to the same root. Prime meets that only after the alias exists.
- Per Omnigent session, rotation resets `_session_url`, `_transcript` (a fresh `TranscriptState`, same `agent_label`),
  `_posted_settings`, `_lost_status`, `_unposted` with its backoff, and the policy notice. Per runtime, these stay:
  `_incarnation`, `binding.json`, the owner claim, `_delivered`, `_prompt_attempts`, inbox, controls and daemon client.

## When to rotate

On each `_attach`, with S as the root's `sessionId`:

1. If `bound[S]` exists, that session is the target. This is a switch back and creates nothing.
2. Otherwise GET the active session. If its `external_session_id` is unset or equals S, PATCH it to S and bind S to
   it. This covers the first attach after launch and a `--resume` launch.
3. Otherwise create Y and bind S to Y.

If the target is not the active session, the bridge moves to it. `bound` persists in R, so `/resume` back to the
first conversation returns to X with no second session, also after R relaunches. The store has
`find_conversation_by_external_session_id` (`stores/conversation_store/__init__.py:578`), but only import routes use
it and no session API exposes it. The bridge creates every session for its roots, so the local map is enough.

## Rotation steps and failure

The steps run in `_attach` before the daemon `attach`, so nothing is mirrored or delivered until the target is
settled. Each converges on re-run. A failure raises into `_session_loop`, which retries every 0.25 s and logs only a
changed failure (`session_bridge.py:147-157`). Throttle repeated 4xx with the repost backoff, 1 s doubling to 30 s.

1. Create, in case 3 only. GET X for `agent_id`, `runner_id` and `labels`, POST `/v1/sessions` with
   `{agent_id, labels}`, then write `bound[S]=Y` and the alias at once. A crash before that write leaves one empty
   session, as codex does.
2. PATCH the target with `{runner_id}` and `{external_session_id: S}`. Both are no-ops on re-run.
3. Transfer the terminal from the active session to the target. A 404 counts as done when the target's
   `terminal_prime-native_main` returns 200.
4. Best effort, post `external_session_superseded` with `{target_conversation_id}` to the old session so open web
   views redirect (`routes_events.py:1962`, as claude does at `claude_native/forwarder.py:5353`).
5. Set `active`, reset the per-session state, and attach with `post_history=True`. Stable `source_id`s make
   re-posting a switched-back conversation harmless.

Until step 5, X stays holder, so a message accepted for X just before `/new` can reach the new root, as today. Keep
the old session's `runner_id`, unlike codex (`forwarder.py:2517`). The gate refuses it, and a switch back needs it.

## Prime 0.9 extension

It has the same gap. `session_start` (`omnigent_pi_native_extension.js:2320`) calls `patchExternalSessionId`
(`:2388`), which PATCHes `config.sessionId`, read once at load, and drops every outcome (`:903-918`). Leave it. The
Prime 0.9 path is removed in 0.19.0 (`process.py:148`, `main.py:334`). Plain pi-native shares the JS and needs its
own rotation through the `pi_native` runner paths, so track it as a pi-native follow-up.

## Test plan

`tests/_helpers` has no HTTP session fake, and only one file needs one, so it stays local to
`test_session_bridge.py` and replaces the answer-200-to-everything `record` (`:221-232`). It holds sessions with
write-once `external_session_id` (400 on change), a POST `/v1/sessions` that returns `conv_2` then `conv_3`, a
transfer route that moves a terminal owner, and `/events` logs per session. The handler in
`tests/harnesses/codex_native/session/test_session_rotation.py` is the template.

- Extend `test_the_bridge_follows_the_terminal_into_a_new_session` (`:804`). After `/new` there is one create,
  `conv_1` keeps `NATIVE_ID`, `conv_2` has the fresh `sessionId`, and one transfer moves the terminal to `conv_2`.
  The fresh root's history and the web prompt reach `conv_2` only. Switching back creates nothing and moves the
  terminal to `conv_1`.
- With `conv_1` unset, the first attach sends one PATCH. At `NATIVE_ID`, a resume launch creates and moves nothing.
- Convergence. Fail the create with 503, then the transfer, then one PATCH. After recovery there is one `conv_2`,
  one transfer, and nothing from the fresh root in `conv_1`. Restart the bridge mid-rotation and assert no second create.
- Runner. Parametrize `test_session_resources.py:928` so a same-root Prime transfer moves and a different-root move
  still gets 409. A message for a non-holder gets 409 and never calls `launch_prime_terminal`. Test alias resolution.
- `verify.py` on real Prime 0.10 with the fixture model. After the first web turn, send `/new\r` to the tmux TUI and
  read the target from the old session's superseded event. Assert the old `external_session_id` is unchanged, the new
  one equals the daemon's new `sessionId`, a web message to the new session lands once in the new Prime session and
  not in the old, `terminal_prime-native_main` is 200 on the new session and 404 on the old, and a message to the old
  session gets 409. The wrong-result control expects the terminal on the old session and must fail.

## Size, order and the riskiest unknown

About 300 production lines (bridge 150, lineage and alias 60, runner gates 50, guard 15) and 400 test lines. Land
four verifiable units in order. Units 2 and 3 touch disjoint files once unit 2 fixes the lineage format.

1. Pin, about 40 lines. Apply rules 1 to 3, and in case 3 stop mirroring and delivering and post one notice to X.
   This alone ends the mixing and the ignored 400.
2. Lineage, alias, holder gate and transfer guard, with runner tests.
3. Bridge rotation, replacing the pin's case 3.
4. The `verify.py` `/new` check.

The riskiest unknown is whether every runner path that can launch, stop or deliver to a Prime root for a non-holder
passes a gate. One missed path, such as session init on reconnect, page-open auto-create or orphan GC
(`process.py:1025-1045`), stops the live TUI through `reserve_prime_launch`. Settle it before coding with a census
of the callers of `launch_prime_terminal`, `stop_session`, `runtime_paths` and the Prime provider hooks. Then write
one runner test that registers a live fake terminal under Y, with Y as holder, drives a message, a session init and
a delete for X, and asserts R's owner claim survives. Orphan GC also keys on the launch session (`process.py:1040`).
Decide whether it waits until every session in `bound` is deleted.

## If changing ownership is unwelcome

Stop after unit 1. Prime's TUI hosts many conversations in one runtime, and this fork ties Stop to one session per
runtime. Any rotation either shares the runtime, as above, or leaves the terminal on the old session while it shows
the new conversation. Pin keeps every invariant. Its one loss is that conversations started with `/new` stay off the web.
