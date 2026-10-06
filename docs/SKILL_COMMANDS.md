# Skill commands

API clients and bridges invoke a session skill with a structured event:

```json
{
  "type": "slash_command",
  "data": {
    "kind": "skill",
    "name": "poteto-mode",
    "arguments": "Review this change",
    "stable_id": "7b2cf620cb18435a9ed9e06591016a80"
  }
}
```

Send this to `POST /v1/sessions/{id}/events`. The visible conversation item
remains a slash command. Allocate `stable_id` as 32 lowercase hexadecimal
characters before the first POST. Keep it with the original name, arguments,
and explicit model override if the response is lost. A deliberate second
invocation uses a new identity.

## Admission and recovery

The response and saved command contain the same `delivery` record. Its `status`
is `accepted`, `rejected`, or `unknown`. Only accepted admission sets
`queued=true`. Admission describes the runner's responsibility for the command.
It does not establish native completion or a successful side effect.

The durable claim binds the workspace, conversation, client identity, original
creator, and submitted fingerprint. When a saved claim exists, reposting the
same identity reads that claim without forwarding again. An absent claim does
not prove that an earlier request cannot still arrive. Changing the submitted fields or creator
returns HTTP 409. Conversation access is checked before lookup. A retry reads
the original claim before resolving the current skill, model selection, or
routing flag, including after skill deletion, compaction, and server or runner
restart. Protection lasts until the conversation is deleted.

A matching runner reply proves acceptance only after a buffer or background
task takes responsibility. An attributed pre-admission refusal proves
rejection. Generic HTTP errors, missing or mismatched reply identity, lost
responses, and cancellation leave admission unknown. An unknown claim is not
authority to resend. This version retains that unknown result; it has no runner
receipt cache that can resolve a lost runner acknowledgment automatically.
Inspect the saved conversation and native result before deciding to start a
new invocation. The web and REPL clients preserve the submitted identity on
uncertainty and do not retry the command automatically.

## Send a skill in the web app

In a native session's chat view, type a complete skill name from the session's
catalog and click **Send** once. Claude uses `/name arguments`; Codex uses
`$name arguments`. For example, `/plugin-name:skill-name Review this change`
selects that qualified Claude skill. Names match the catalog exactly, including
case and namespace. The browser preserves whitespace inside the arguments.
The suggestions menu inserts a name into the draft; selecting a suggestion is
optional and does not invoke the skill.

A complete catalog skill sent without quotes, uploaded files, or path mentions
uses the structured event above. Unknown or partial names, paths, inline tokens,
and a prefix for another CLI remain ordinary messages. Quotes, uploads, and path
mentions keep the ordinary reply path. Local built-ins keep their existing
precedence. Claude's `/btw` and Codex's `/side` remain native CLI commands even
when the catalog contains the same name. Codex catalog skills named `help`,
`btw`, or `side` still use `$help`, `$btw`, or `$side`.

The browser does not choose native loading or paste fallback. The server's
`native_skill_routing` flag controls that choice for a new structured invocation.
Both paths use the same admission and recovery records. The native permission
hook still evaluates the actual REQUEST; browser admission does not replace it.

## Check admission in the web app

Open the conversation and find the skill card. The card shows one of these labels:

- **Admitted; completion not confirmed** means the runner accepted responsibility.
- **Rejected before admission** means an attributed refusal established rejection.
- **Admission unknown** means the saved evidence does not establish either outcome.
- **Historical copy** means the command belongs to a forked transcript and is display-only.

Cards with a saved admission remain visible outside a collapsed **Worked**
section. This applies to loaded transcript items; older commands without a
delivery record keep the usual process folding.

Click **Check admission** on the card to read the saved outcome. Expand
**Invocation details** to see the original identity. The check reads conversation
history in pages and matches the original identity in the returned items. It
uses GET requests and never sends a command. A historical copy has no
recovery action.

If the response was lost, the **Skill recovery** panel above the composer also
retains the original command and its **Check admission** button. This panel
survives navigation and browser reload. Once a check saves accepted or rejected
admission, that invocation leaves the recovery panel. Its card remains in the
loaded transcript, with its admission label, original identity, and
**Check admission** action. If no saved claim is found, the panel keeps the
outcome unknown. Check the native result before deliberately starting
a new invocation. Typing and sending the skill again allocates a new identity.
An admission check does not prove a native side effect or that **Stop** has
halted all native work.

The browser writes the original request to local storage before sending it.
Records are scoped to the server, signed-in user, and conversation. If that
initial write fails, the browser does not send the command. If a later admission
update fails, the recovery panel reports the storage error while the skill card
and **Check admission** use the server outcome. Keep the original browser record
if you need to recover a request that has no server claim. Saved server claims
remain visible to authorized clients even when browser storage is unavailable
or cleared.

## Check admission in the REPL

Resume the original conversation, then run `/recover-skill` to list its saved
commands and locally retained submissions. To check one invocation, copy its ID
from the transcript and run:

```text
/recover-skill 7b2cf620cb18435a9ed9e06591016a80
```

The command reads history and prints the admission label and identity. It also
reports whether no saved claim was found. It never sends a command or declares
native completion. `/history` shows the same admission labels. Forked historical
commands remain display-only.

The REPL saves the original request before sending it under
`$OMNIGENT_DATA_DIR/skill-submissions`, or `~/.omnigent/skill-submissions` when the
environment variable is unset. Each server and conversation has its own hashed
directory in the local user's state. Resume with the same server URL and state
directory after restarting the REPL. The journal retains the original explicit
model override as well as the command and identity. A new skill invocation gets
a new identity, even when its text matches an earlier submission.

## Native loading

Native routing is off by default. Add `native_skill_routing` to the server's
comma-separated `OMNIGENT_FEATURES` value and restart the server to enable it.
For example, `OMNIGENT_FEATURES=usage_page,native_skill_routing` retains the
Usage page feature and enables native routing. Configure the server rather
than the runner; remote runners receive the decision in the resolve request.
The deployment snapshot controls routing. Client event data cannot enable it.

This temporary flag has owner `runtime` and a review target of `0.17.0`.
Native Claude loading performs argument substitution and may execute embedded
shell commands before sending skill content to the model. See the
[Claude Code skill documentation](https://code.claude.com/docs/en/skills#inject-dynamic-context).
The paste path
inserts that content literally. The opt-in permits testing those semantics
before enabling native routing for every deployment.

Removing the entry and restarting the server restores paste routing for new
structured invocations. Expansion capture, echo receipts, resume, and fork
remain enabled for existing native history. The flag does not disable commands
typed directly in native terminals. Listing removal and paste deduplication
remain enabled regardless of the flag.

An older server omits native permission, so a new runner pastes. A new server
accepts legacy pasted responses from the pre-change runner. If a runner ignores
an OFF permission and returns only a native command, the server rejects the
response before saving or delivering the command.

For `claude-native` and `codex-native`, the runner checks whether the CLI can
invoke the selected skill file. A verified selection sends `/name arguments`
to Claude Code or `$name arguments` to Codex. Claude plugin skills use their
qualified command, such as `/plugin-name:skill-name arguments`.

The native provider's optional `native_skill_invocation` hook defaults to
`None`. Providers without this hook keep the paste path. A native provider
also returns `None` when it cannot prove that the command selects the intended
file. Missing files, conflicting names, unsupported launch configuration,
and an unverified Codex launch environment keep the paste path.

Claude checks its user and project skill directories and enabled plugins.
Project discovery stops at the closest repository or worktree root. Skills
above that boundary, reserved local `synced` and `anthropic-skills` names,
and nested plain skill folders keep the paste path when native selection
cannot be proved.
A personal or project skill disabled by `skillOverrides` keeps the paste path.
A plain Claude skill whose frontmatter name differs from its directory stays on
the paste path because Omnigent cannot prove that both names select the same file. Launching Claude
with `--bare` also keeps the paste path because that flag disables skills.
A plugin name that already includes its own namespace keeps that prefix once.
Codex checks its actual launch `CODEX_HOME`, `.agents/skills` directories from
the working directory through the repository root, `~/.agents/skills`, and
`/etc/codex/skills`. Codex plugin selections and files beneath a plugin manifest
currently use the paste path because the CLI qualifies their names. An
installed plugin cache alone does not prevent native loading of a plain skill.

Explicit invocation remains available for a Claude skill with
`disable-model-invocation: true` and a Codex skill with
`policy.allow_implicit_invocation: false`. These settings restrict automatic
invocation. They do not grant manual access to a skill the CLI has disabled. Claude skills
with `user-invocable: false` also keep the paste path.

The native selection rules target Claude Code 2.1.289 and Codex 0.160.0.
The v0.17 port has source-level regression coverage; it has not qualified
installed native runtimes against those versions.
See the [Claude skills reference](https://code.claude.com/docs/en/skills) and
[Codex skills reference](https://learn.chatgpt.com/docs/build-skills). The
[0.160.0 selection code](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/ext/skills/src/selection.rs)
checks implicit invocation policy separately from explicit skill mentions.

## Resume and fork

The visible slash item stores the exact native command in `native_invocation`.
Omnigent does not create a full skill wrapper for this path. The CLI's transcript
forwarder preserves the expansion the CLI actually loaded as hidden context.
Codex itself uses a `<skill>` wrapper, so native Codex transcripts still contain
CLI-generated skill blocks.

Cold resume and history-based forks restore the historical command and its
saved expansion. Reading that command as history does not run it again. This
retains the instructions used for the original turn even if the skill changes
later, and avoids repeating dynamic substitutions or side effects. Claude
reconstruction retains `isMeta`; Codex reconstruction retains hidden context
without creating a visible user-event mirror.

Native command echoes receive an empty hidden receipt under the CLI event's
source identity. The receipt stores no skill content, creates no second visible
command, and contributes no prompt input. Its durable identity makes a repeated
echo idempotent after the first receipt commits. Matching an initial echo to its
pending invocation still depends on the existing process-local pending queue.
The pending queue provides history correlation, not permission. Without proved
request identity, the native hook evaluates the actual REQUEST again. This can
ask for approval a second time after the public POST. An unrelated terminal
prompt cannot inherit approval from a pending command.

If a process stops before the CLI expansion is persisted, the command alone
cannot recover the historical instructions. History reconstruction reports a
historical command with missing expansion and does not read today's file as a
replacement or re-execute the command.

## Paste fallback

The paste wrapper contains the skill name, runner-local path, instructions, and
request arguments. It no longer lists every file in the skill directory.
`load_skill` and `read_skill_file` still provide access to auxiliary files.

For runner-managed history, an identical instruction block already in active
user context permits a short invocation reference instead of another full
copy. Every request keeps its arguments. Changed content or paths require a
new full block. History after the latest compaction is authoritative, including
replacement messages retained by that compaction. An assistant quotation or a
discarded pre-compaction block does not prove that instructions remain active.

Each durable paste message is bound to its command claim. Unknown and rejected
context is excluded from reconstructed input. A fork remaps the historical
references and marks copied command claims and bound context as display-only.
Unbound meta messages keep their existing history behavior.

Native fallback sessions continue pasting the full instructions on each
invocation. Their live context belongs to the CLI, so the runner's managed
history cache cannot prove that an earlier copy remains in that context.

## Verification

The v0.17 port uses controlled Python and web regressions. Native provider
result correlation and installed server and runner restart behavior still
require the live checks below. An accepted nonnative paste context remains available to later deliberate
prompts. When that context is the trailing user item, runner initialization
does not start a new `history_resume` turn for it. Ordinary trailing user
messages retain their existing recovery behavior.

Run the regression checks from the source checkout:

```sh
uv sync --locked --extra all --group dev
uv run --no-sync pytest tests/runner/test_skills.py tests/runner/test_native_skill_invocation.py tests/tools/builtins/test_load_skill.py -q
uv run --no-sync pytest tests/server/integration/test_sessions_endpoints.py -k native_skill_dispatch -q
```

The opt-in native test requires authenticated Claude and Codex CLIs, `tmux`,
and an installed `poteto-mode` skill. It creates throwaway sessions and records
three invocations, cold resume, and a truncated fork. Resume and fork must recall
a marker found only in the skill instructions without using a tool to read it.

```sh
OMNIGENT_FEATURES=native_skill_routing OMNIGENT_E2E_NATIVE_SKILLS=1 TMPDIR=/tmp NATIVE_SKILL_OUTPUT=/tmp/native-skill-evidence uv run --no-sync pytest tests/e2e/test_native_skill_invocation_e2e.py --basetemp=/tmp/native-skill-check -q
```

Inspect the saved CLI transcript and Omnigent items. Each request should have
one visible slash item with `native_invocation`, followed by the CLI's own
expansion. The native turn must contain no Omnigent `<user_request>` wrapper.
The evidence files also retain each CLI's input and cache token counters.

### Command fault recovery

Run these checks from the repository root after the locked test dependencies
are installed. A nonzero exit or a test summary with no passing tests fails the
controlled regression check.

```sh
uv run --no-sync pytest tests/server/integration/test_skill_command_admission.py tests/stores/test_skill_command_claims.py tests/runner/test_skill_context_history_resume.py -q
```

The maintained driver uses the real Claude CLI, native hooks, runner tunnel,
and Bash tool. Only remote inference uses a local fixture. The external relay
records refused delivery, hides a real runner reply behind an ambiguous 502,
and withholds a real public response. It records attributable native assistant
completion, counts forwards independently of native effects, and restarts the
actual server and runner processes with the same conversation and identities.
It then sends a fresh command to check recovery. This does not qualify remote
provider authentication or Codex native behavior.

Preflight starts no runtime. A nonzero exit or `live_started` other than false
fails preflight.

```sh
uv run --no-sync python scripts/verify_skill_command_recovery.py
```

Use a new output directory for each live run. Exit 1 is a failed assertion.
Exit 2 is a runtime or environment gap. Success requires exit 0,
`qualified=true`, `baseline_verified=true`, `cleanup_qualified=true`, an empty
`cleanup_gaps` list, and an empty `owned_survivors` list in `summary.json`. Inspect `fault-observed.jsonl`, `forwards.jsonl`,
`native-effects.txt`, `items.json`, and the recorded process identities.
A budgeted recovery investigation must run this command through its budget
wrapper.

```sh
uv run --no-sync python scripts/verify_skill_command_recovery.py --run --output /tmp/skill-command-recovery-fresh
```

The driver's `native-owners.json` records the private tmux server, pane, and
native process identities while they are alive. Each identity includes its PID,
creation time, private socket, and observed run-specific environment. The final
summary includes detached processes attributed by the unique config directory or
run marker. A nonzero tmux close, an observation gap, or any owned survivor makes
cleanup unqualified. An older run's empty survivor list does not qualify this
stronger census.
