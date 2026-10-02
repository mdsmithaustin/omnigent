# Skill commands

API clients and bridges invoke a session skill with a structured event:

```json
{
  "type": "slash_command",
  "data": {
    "kind": "skill",
    "name": "poteto-mode",
    "arguments": "Review this change"
  }
}
```

Send this to `POST /v1/sessions/{id}/events`. The visible conversation item
remains a slash command. Client routing and the event format do not change.

## Native loading

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
A personal or project skill disabled by `skillOverrides` keeps the paste path.
A plain Claude skill whose frontmatter name differs from its directory stays on
the paste path because Claude resolves the directory command. Launching Claude
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

The behavior was checked against Claude Code 2.1.288 and Codex 0.160.0.
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

If a process stops before the CLI expansion is persisted, the command alone
cannot recover the historical instructions. History reconstruction reports a
completed command with missing expansion and does not read today's file as a
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

Native fallback sessions continue pasting the full instructions on each
invocation. Their live context belongs to the CLI, so the runner's managed
history cache cannot prove that an earlier copy remains in that context.

## Verification

Run the regression checks from the source checkout:

```sh
uv sync --extra all --group dev
uv run --no-sync pytest tests/runner/test_skills.py tests/runner/test_native_skill_invocation.py tests/tools/builtins/test_load_skill.py -q
uv run --no-sync pytest tests/server/integration/test_sessions_endpoints.py -k native_skill_dispatch -q
```

The opt-in native test requires authenticated Claude and Codex CLIs, `tmux`,
and an installed `poteto-mode` skill. It creates throwaway sessions and records
three invocations, cold resume, and a truncated fork. Resume and fork must recall
a marker found only in the skill instructions without using a tool to read it.

```sh
OMNIGENT_E2E_NATIVE_SKILLS=1 TMPDIR=/tmp NATIVE_SKILL_OUTPUT=/tmp/native-skill-evidence uv run --no-sync pytest tests/e2e/test_native_skill_invocation_e2e.py --basetemp=/tmp/native-skill-check -q
```

Inspect the saved CLI transcript and Omnigent items. Each request should have
one visible slash item with `native_invocation`, followed by the CLI's own
expansion. The native turn must contain no Omnigent `<user_request>` wrapper.
The evidence files also retain each CLI's input and cache token counters.
