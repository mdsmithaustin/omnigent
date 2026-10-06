# Local fork defaults

Starting with `0.17.0+mdsmithaustin.2`, the fork uses separate defaults from
upstream `0.17.0`. This applies to the certified Python distributions in
separate virtual environments. Distribution names and the `omnigent` and
`omni` commands stay the same.

| Resource | Fork default | Upstream default |
| --- | --- | --- |
| Runtime data and CLI configuration | `~/.omnigent-mdsmithaustin` | `~/.omnigent` |
| Project configuration | `.omnigent-mdsmithaustin/config.yaml` | `.omnigent/config.yaml` |
| Terminal history | `~/.omnigent-mdsmithaustin_history` | `~/.omnigent_history` |
| Preferred local port | `6768` | `6767` |
| Session cookie | `mdsmithaustin_ap_session` | `ap_session` |
| OIDC state cookie | `mdsmithaustin_ap_auth_state` | `ap_auth_state` |
| Keyring service | `mdsmithaustin-omni` | `omnigent` |
| launchd host label | `com.mdsmithaustin.omni.host` | `ai.omnigent.host` |
| systemd host unit | `omni-mdsmithaustin-host.service` | `omnigent-host.service` |

HTTPS cookie names retain the `__Host-` prefix. Browser cookies ignore ports,
so the separate cookie names also isolate two local web UIs on the same host.

Native persistent defaults use the fork user directory. Adapter-owned temporary
directories use the `mdma` prefix and retain their existing trusted parent and
user identity. Prime's compact socket fallback uses `/tmp/mdp-<uid>/`.
External provider login sources, including `~/.codex` and `~/.pi/agent`, keep
their existing selection rules.

## Overrides and discovery

Explicit configuration, data, history, native-state, workspace, model, server
URL, and foreground port selections retain their existing behavior.
`OMNIGENT_DATA_DIR` and `OMNIGENT_CONFIG_HOME` remain separate settings.
Owners that previously ignored `OMNIGENT_DATA_DIR`, such as the stable runner
ID and several native bridge roots, continue to ignore it.

Explicit overrides can select shared resources. Use different values in the two
environments to retain isolation. The fork does not implicitly load the
upstream project configuration.

If the preferred local port is occupied, the background server selects a free
port. Clients discover the actual URL from the fork's own server record. Use
the URL printed by the CLI. The existing background `--port` behavior remains
unchanged; exact port selection applies to foreground server startup.

Both `omnigent server stop` and `omnigent stop` use the selected fork process
records. They do not stop an unrecorded listener merely because its health
endpoint responds successfully. Stop an unrecorded server through its owner.
This does not establish safe ownership for copied, corrupt, or reused PID
records.

## Existing state and maintenance

Revision `.1` uses the upstream defaults. Revision `.2` starts with the new
fork locations and does not copy, move, or implicitly import `.omnigent`,
`.omnigents`, `.omniagents`, or the upstream terminal history. Configure the
new fork environment before use. Existing settings, conversations, application
tokens, and keyring secrets do not automatically appear there.

Install each version in its own virtual environment using the
[fork release guide](FORK_RELEASES.md). Replace fork wheels from verified fork
assets. Global `uv tool` coinstallation, registry or nightly upgrades, and the
bundled global uninstaller are outside this coexistence profile. Desktop,
editor, mobile, dev-pod, and Docker defaults are also outside this profile.
