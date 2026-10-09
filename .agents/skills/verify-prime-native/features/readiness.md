# Readiness and model choices

Users launch Prime Native and select models that Prime can use with its own credentials.

## Sub-features

- A missing or incompatible Prime executable fails before a turn without another adapter launching.
- Missing or incompatible bridge versions must also fail before a turn. Full N02 bridge agreement remains partial.
- The native command identifies Prime Native.
- An empty authenticated catalog reports no available models.
- One or many available models retain their provider and model IDs.
- A selected model and reasoning effort reach the worker.

## How to get to it (user POV)

Run `omnigent prime-native --help`, launch `omnigent prime-native`, or choose Prime Native when starting an Omnigent session. In Prime, use `/login` and the model picker.

## Driving it with the CLI and HTTP API

Positive launch and catalog checks require the repository environment and a real
Prime 0.9.6 binary. Deliberately missing-executable and controlled unsupported-version
checks use separate isolated configuration, data, workspace, and server allocations.
They do not require a working Prime binary at the selected path.

- For positive prerequisites, require exact `0.9.6` from `prime-agent --version` and successful `omnigent prime-native --help`. A nonzero exit or missing expected output fails. These commands do not prove a turn. The helper's [doctor finalizer](../SKILL.md#doctor) performs global process-environment discovery even when the drive is skipped. Do not invoke the helper when that inspection is prohibited.
- Run `prime-agent model list` with isolated Prime configuration. Require its known table columns when models exist. Distinguish Prime's recognized empty-catalog response from a failed command or malformed output.
- Launch with a fixture provider and model. Read the session's model options and active model after extension readiness. Require the selected provider/model ID.
- For the missing-executable case, set `OMNIGENT_PRIME_PATH` to a deliberately absent absolute path and use the public `omnigent prime-native --server <owned URL>` command. Require exit 1 and the exact missing-binary diagnostic below.
- For the unsupported-version case, select a controlled executable that reports `0.9.5` only for `--version`. Require exit 1 and the exact version diagnostic below. A version-only fixture does not establish native operation of Prime 0.9.5.
- For each refusal, require zero sessions from `omnigent host status --server <owned URL> --sessions --json`, no Prime worker or alternate harness from that attempt, and a version-only trace for the controlled fixture. Any session, worker, fallback, or extra fixture invocation fails the case.

## Create a fresh executable-refusal allocation

This procedure is source-derived and **NOT_RUN**. It adds no runtime qualification.
Use it for either negative case, separately from positive Prime 0.9.6 checks.
Save the Bash block below as `/tmp/prime-refusal-recipe.sh`. From this checkout's
root, run these two commands separately. A nonzero exit fails that run.

```sh
PR_CASE=missing bash /tmp/prime-refusal-recipe.sh
PR_CASE=version bash /tmp/prime-refusal-recipe.sh
```

The block reads the caller's `PR_CASE` and defaults to `missing` only when absent.
Each invocation creates its own configuration, data, workspace, fixture, evidence,
server URL, and server lifetime. The paths and
ports in the recorded receipts below are historical evidence only.

First create this checkout's development environment using the maintained
[development setup](../../../../CONTRIBUTING.md#development-setup).
A nonzero setup exit fails the prerequisite.
Run the block from that checkout's root with Bash, Python, curl, and native macOS
`/usr/sbin/lsof` available. This Mac's installed `lsof` 4.91 help and
`/usr/share/man/man8/lsof.8` document the options below. Require support for
`-s p:s`, as listed by `/usr/sbin/lsof -h`. Missing support or a nonzero help exit
fails the prerequisite. Run as the user that starts the server. Inaccessible
listener metadata fails the ownership check. The recipe inspects only its fixed
child PID and exact TCP endpoint. It reads no process environment.
It selects `.venv/bin/omnigent` from this checkout, never a historical installed
user path. The empty workspace prevents project-local configuration overrides.

```bash
(
set -euo pipefail
PR_CASE="${PR_CASE-missing}"
PR_REPO="$(pwd -P)"
PR_OMNI="$PR_REPO/.venv/bin/omnigent"
PR_PYTHON="$PR_REPO/.venv/bin/python"
test -f "$PR_REPO/pyproject.toml"
test -x "$PR_OMNI"
test -x "$PR_PYTHON"
test -x /usr/sbin/lsof
case "$PR_CASE" in missing|version) ;; *) exit 2 ;; esac
PR_ROOT="$(mktemp -d /tmp/prime-refusal.XXXXXX)"
mkdir -p "$PR_ROOT"/{config,data,workspace,fixture,evidence,artifacts,tmp}
printf 'Allocation: %s\nCase: %s\n' "$PR_ROOT" "$PR_CASE"
PR_MISSING="$PR_ROOT/fixture/missing-prime-agent"
PR_FIXTURE="$PR_ROOT/fixture/version-only"
PR_TRACE="$PR_ROOT/evidence/invocations.txt"
PR_ENV=(env -i "PATH=$PR_REPO/.venv/bin:/usr/bin:/bin:/usr/sbin:/sbin"
	"PYTHONPATH=$PR_REPO" PYTHONDONTWRITEBYTECODE=1
	"OMNIGENT_CONFIG_HOME=$PR_ROOT/config" "OMNIGENT_DATA_DIR=$PR_ROOT/data"
	"TMPDIR=$PR_ROOT/tmp" OMNIGENT_SKIP_WEB_UI=true
	"OMNIGENT_PRIME_PATH=$PR_MISSING")
test ! -e "$PR_MISSING"
cat > "$PR_FIXTURE" <<'FIXTURE'
#!/bin/sh
if [ "$#" -eq 1 ] && [ "$1" = '--version' ]; then
	printf 'version_probe\n' >> "${PRIME_REFUSAL_TRACE_FILE:?}"
	printf '0.9.5\n'
	exit 0
fi
printf 'unexpected_invocation\n' >> "${PRIME_REFUSAL_TRACE_FILE:?}"
exit 64
FIXTURE
chmod 700 "$PR_FIXTURE"
PR_PORT="$("$PR_PYTHON" - <<'PY'
import socket
with socket.socket() as s:
    s.bind(('127.0.0.1', 0))
    print(s.getsockname()[1])
PY
)"
PR_SERVER="http://127.0.0.1:$PR_PORT"
readonly PR_PORT PR_SERVER
cd "$PR_ROOT/workspace"
"${PR_ENV[@]}" "$PR_OMNI" server --host 127.0.0.1 --port "$PR_PORT" \
	--database-uri "sqlite:///$PR_ROOT/data/server.db" \
	--artifact-location "$PR_ROOT/artifacts" --no-open \
	> "$PR_ROOT/evidence/server.log" 2>&1 &
PR_SERVER_PID=$!
readonly PR_SERVER_PID
PR_HOST_ATTEMPTED=0
PR_LISTENER_CHECK=0
owns_server_listener() {
	PR_LISTENER_CHECK=$((PR_LISTENER_CHECK + 1))
	PR_FIELDS="$PR_ROOT/evidence/listener-$PR_LISTENER_CHECK.fields"
	PR_LSOF_EXIT=0
	/usr/sbin/lsof -nP -a -p "$PR_SERVER_PID" "-iTCP@127.0.0.1:$PR_PORT" \
		-sTCP:LISTEN -F pfn > "$PR_FIELDS" 2> "$PR_FIELDS.stderr" || PR_LSOF_EXIT=$?
	printf '%s\n' "$PR_LSOF_EXIT" > "$PR_FIELDS.exit"
	test "$PR_LSOF_EXIT" -eq 0 || return 1
	test ! -s "$PR_FIELDS.stderr" || return 1
	"$PR_PYTHON" - "$PR_SERVER_PID" "$PR_PORT" "$PR_FIELDS" <<'PY'
import pathlib, sys
pid, port, fields = sys.argv[1:]
lines = pathlib.Path(fields).read_text().splitlines()
if (len(lines) != 3 or lines[0] != 'p' + pid
        or not (lines[1].startswith('f') and lines[1][1:].isdigit())
        or lines[2] != 'n127.0.0.1:' + port):
    raise SystemExit('UNPROVED_SERVER_LISTENER ' + repr(lines))
print('OWNED_SERVER_LISTENER', pid, '127.0.0.1:' + port)
PY
}
printf 'Server: %s\nPID: %s\n' "$PR_SERVER" "$PR_SERVER_PID"
settle_refusal() {
	PR_RESULT=$?
	trap - EXIT
	set +e
	PR_TARGET_SETTLEMENT=0
	if [ "$PR_HOST_ATTEMPTED" -eq 1 ] && owns_server_listener; then
		PR_TARGET_SETTLEMENT=1
		"${PR_ENV[@]}" "$PR_OMNI" host stop --server "$PR_SERVER" --daemon-only \
			> "$PR_ROOT/evidence/host-stop.txt" 2>&1
		PR_STOP_EXIT=$?
		PR_STATUS_EXIT=1
		if owns_server_listener; then
			"${PR_ENV[@]}" "$PR_OMNI" host status --server "$PR_SERVER" --json \
				> "$PR_ROOT/evidence/host-after.json"
			PR_STATUS_EXIT=$?
		else
			PR_TARGET_SETTLEMENT=0
		fi
	else
		printf 'Target settlement NOT_RUN; no host action on unproved URL; retain %s\n' "$PR_ROOT"
	fi
	kill -TERM "$PR_SERVER_PID"
	PR_SIGNAL_EXIT=$?
	for PR_SETTLE_TICK in {1..40}; do
		kill -0 "$PR_SERVER_PID" 2>/dev/null || break
		sleep 0.25
	done
	if kill -0 "$PR_SERVER_PID" 2>/dev/null; then
		printf 'UNSETTLED server PID %s; retain allocation %s\n' "$PR_SERVER_PID" "$PR_ROOT"
		exit 1
	fi
	wait "$PR_SERVER_PID"
	PR_SERVER_EXIT=$?
	if [ "$PR_TARGET_SETTLEMENT" -ne 1 ]; then
		printf 'FAILED refusal; child wait exit %s; retain %s\n' "$PR_SERVER_EXIT" "$PR_ROOT"
		exit 1
	fi
	curl --silent --show-error --max-time 2 --output /dev/null \
		--write-out '%{http_code}\n' "$PR_SERVER/health" \
		> "$PR_ROOT/evidence/health-after.txt" 2> "$PR_ROOT/evidence/health-after.stderr"
	PR_CURL_EXIT=$?
	"$PR_PYTHON" - "$PR_ROOT" "$PR_RESULT" "$PR_STOP_EXIT" "$PR_STATUS_EXIT" \
		"$PR_SIGNAL_EXIT" "$PR_SERVER_EXIT" "$PR_CURL_EXIT" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
statuses = [int(value) for value in sys.argv[2:]]
print('Settlement statuses:', statuses)
(root / 'evidence' / 'settlement.json').write_text(json.dumps(statuses) + '\n')
if statuses[:4] != [0, 0, 0, 0]:
    raise SystemExit('FAILED_REFUSAL_OR_TARGET_SETTLEMENT ' + repr(statuses))
if statuses[4] not in (0, 143):
    raise SystemExit('UNSETTLED_SERVER_EXIT ' + repr(statuses))
if statuses[5] != 7:
    raise SystemExit('RETIRED_ENDPOINT_NOT_REFUSED ' + repr(statuses))
if json.loads((root / 'evidence' / 'host-after.json').read_text()) != {'daemons': []}:
    raise SystemExit('TARGET_HOST_REMAINS')
if (root / 'evidence' / 'health-after.txt').read_text() != '000\n':
    raise SystemExit('RETIRED_ENDPOINT_HTTP_STATUS')
if 'Stopped ' not in (root / 'evidence' / 'host-stop.txt').read_text():
    raise SystemExit('TARGET_HOST_STOP_NOT_CONFIRMED')
print('SETTLED_REFUSAL_SERVER_AND_HOST', root)
PY
}
trap settle_refusal EXIT
PR_HEALTH=''
for PR_READY_TICK in {1..60}; do
	kill -0 "$PR_SERVER_PID"
	if owns_server_listener && PR_HEALTH="$(curl --silent --show-error --max-time 2 --output /dev/null \
		--write-out '%{http_code}' "$PR_SERVER/health")" && [ "$PR_HEALTH" = 200 ]; then
		break
	fi
	sleep 0.5
done
test "$PR_HEALTH" = 200
printf 'Health: %s\n' "$PR_HEALTH"
owns_server_listener
PR_HOST_ATTEMPTED=1
"${PR_ENV[@]}" "$PR_OMNI" host --server "$PR_SERVER" --background --no-open --non-interactive \
	> "$PR_ROOT/evidence/host-start.txt"
cat "$PR_ROOT/evidence/host-start.txt"
PR_SELECTED="$PR_MISSING"
PR_DIAGNOSTIC='Prime Native requires prime-agent 0.9.6. Set OMNIGENT_PRIME_PATH or install it on PATH.'
if [ "$PR_CASE" = version ]; then
	PR_SELECTED="$PR_FIXTURE"
	PR_DIAGNOSTIC="Prime Native requires prime-agent 0.9.6; found '0.9.5'."
fi
PR_EXIT=0
owns_server_listener
"${PR_ENV[@]}" "OMNIGENT_PRIME_PATH=$PR_SELECTED" "PRIME_REFUSAL_TRACE_FILE=$PR_TRACE" \
	"$PR_OMNI" prime-native --server "$PR_SERVER" \
	> "$PR_ROOT/evidence/refusal.stdout" 2> "$PR_ROOT/evidence/refusal.stderr" || PR_EXIT=$?
printf 'Refusal exit: %s\n' "$PR_EXIT"
test "$PR_EXIT" -eq 1
grep -Fqx "Error: $PR_DIAGNOSTIC" "$PR_ROOT/evidence/refusal.stderr"
printf '%s\n' "$PR_DIAGNOSTIC"
owns_server_listener
"${PR_ENV[@]}" "$PR_OMNI" host status --server "$PR_SERVER" --sessions --json \
	> "$PR_ROOT/evidence/host-status.json"
"$PR_PYTHON" - "$PR_ROOT" "$PR_CASE" "$PR_SERVER" <<'PY'
import json, pathlib, sys
root, case, server = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
daemons = json.loads((root / 'evidence' / 'host-status.json').read_text())['daemons']
if len(daemons) != 1:
    raise SystemExit('EXPECTED_ONE_HOST ' + repr(daemons))
daemon = daemons[0]
if daemon['server_url'] != server:
    raise SystemExit('WRONG_HOST_SERVER ' + repr(daemon))
if not (daemon['process'] == daemon['host_status'] == 'online'):
    raise SystemExit('HOST_NOT_ONLINE ' + repr(daemon))
if daemon['error'] is not None or daemon['sessions'] != []:
    raise SystemExit('HOST_ERROR_OR_CONNECTED_SESSIONS ' + repr(daemon))
trace = root / 'evidence' / 'invocations.txt'
if not (trace.read_text() == 'version_probe\n' if case == 'version' else not trace.exists()):
    raise SystemExit('UNEXPECTED_FIXTURE_TRACE')
print('REFUSAL_OBSERVED', case, 'online owned host; zero connected sessions; fixture trace exact')
PY
)
```

Any nonzero block exit, absent `REFUSAL_OBSERVED` or
`SETTLED_REFUSAL_SERVER_AND_HOST` line, wrong diagnostic, non-1 refusal exit,
status error, nonempty session list, or extra fixture trace fails the procedure.
The Python evidence checks use explicit nonzero failures. They remain active
when the caller enables Python optimization.
The port probe releases its socket before server startup. It allocates a candidate
port and does not prove ownership. `lsof -a` intersects the fixed child PID,
exact TCP address and port, and `LISTEN` state. `-nP` keeps the address and port
numeric. `-F pfn` emits newline-delimited PID, descriptor, and endpoint fields.
Require lsof exit 0, empty stderr, exactly one `p<owned PID>` record, one numeric
`f<descriptor>` record, and exactly `n127.0.0.1:<allocated port>`. A missing,
extra, or different record fails the ownership check. No URL request occurs
until that check passes. Startup retries failed checks within the readiness loop;
exhausted retries or a dead child fail the run. Later ownership failures fail
immediately. Every lsof output, stderr, and exit is retained separately.

A different listener's HTTP 200 cannot satisfy the PID-filtered ownership check.
The block checks ownership before each live URL operation, including trap host
stop and status. If ownership is unproved, the trap skips host actions and URL
requests, settles only the directly launched child, and exits 1. Retain that
failed allocation and rerun the whole block for a fresh allocation. These are
listener snapshots. They do not establish future race immunity or continuous
ownership between a check and a request. The post-signal curl is a retirement
probe at the previously proved endpoint. A new listener there fails settlement.

The trap attempts settlement on success and failure. An unsettled server remains
an explicit retained allocation with its printed PID. Do not remove that allocation
or claim settlement. The server wait accepts only exit 0 or SIGTERM exit 143,
then requires curl exit 7 and HTTP `000` at its retired endpoint.

Public host status lists connected sessions only. Its nonempty online daemon
record prevents an empty registry from passing as a working host. Zero connected
sessions alone cannot establish total session or process absence. For these
specific exact diagnostics, the [public CLI caller](../../../../omnigent/cli_native.py)
reaches [executable admission](../../../../omnigent/harnesses/prime_native/main.py)
before tmux, session creation, or worker dispatch. No Prime worker or alternate
harness from the refused attempt is a source-derived phase conclusion, not a
process census or packet-capture observation. Require exactly one recorded
`--version` probe from the controlled fixture. The fixture is procedural source for synthetic
version refusal, never actual Prime 0.9.5 native operation.

The [server and host CLI source](../../../../omnigent/cli.py) defines the explicit
server options, dedicated-server behavior, status fields, and URL-qualified stop.
`host --background` waits for online registration before the refusal command.
The recipe stops that host while its server is alive, signals only its directly
owned server child, and waits for that child. It retains all new files and evidence.
This bounded settlement does not qualify whole-descendant cleanup, N02 bridge
agreement, or native terminal and HTTP operation.

## Recorded executable refusals

These terminal observations used Omnigent `0.16.0.dev0`, Python 3.12.14, and
checkout `7e1474a8077e3a1cd0627f830ed94e391c498fc3`. The commands below are
historical receipts, not paths or ports to reuse. Each attempt connected to its
owned server and host before executable admission. Both rejected before the
tmux check, session creation, and Prime worker setup.

### Missing executable

Recorded cwd was `/tmp/prime-cli-version-refusal.oBs1Y7/workspace`.

```sh
env -i PATH='/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin' PYTHONPATH='/var/folders/hy/59t2376d7j59lrfrgb7qy2mw0000gn/T/pstack-prime-cli-version-av360h07/repo' PYTHONDONTWRITEBYTECODE=1 OMNIGENT_CONFIG_HOME='/tmp/prime-cli-version-refusal.oBs1Y7/config' OMNIGENT_DATA_DIR='/tmp/prime-cli-version-refusal.oBs1Y7/data' OMNIGENT_PRIME_PATH='/tmp/prime-cli-version-refusal.oBs1Y7/fixture/missing-prime-agent' /Users/msmith1/.local/bin/omnigent prime-native --server 'http://127.0.0.1:49503'
```

The command exited 1 with this diagnostic.

```text
Prime Native requires prime-agent 0.9.6. Set OMNIGENT_PRIME_PATH or install it on PATH.
```

### Controlled unsupported version

Recorded cwd was `/tmp/prime-cli-version-refusal.uOXJAA/workspace`.

```sh
env -i PATH='/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin' PYTHONPATH='/var/folders/hy/59t2376d7j59lrfrgb7qy2mw0000gn/T/pstack-prime-cli-version-av360h07/repo' PYTHONDONTWRITEBYTECODE=1 OMNIGENT_CONFIG_HOME='/tmp/prime-cli-version-refusal.uOXJAA/config' OMNIGENT_DATA_DIR='/tmp/prime-cli-version-refusal.uOXJAA/data' OMNIGENT_PRIME_PATH='/tmp/prime-cli-version-refusal.uOXJAA/fixture/prime-agent-wrong-version' PRIME_REFUSAL_TRACE_FILE='/tmp/prime-cli-version-refusal.uOXJAA/fixture/invocations.txt' /Users/msmith1/.local/bin/omnigent prime-native --server 'http://127.0.0.1:50395'
```

The command exited 1 with this diagnostic.

```text
Prime Native requires prime-agent 0.9.6; found '0.9.5'.
```

Public host status reported zero sessions in both attempts. The controlled
fixture trace contained only `version_probe`. No provider fixture or model
request ran. This is phase-bound refusal evidence, not packet-level traffic
capture. The historical controlled 0.9.7 case also used synthetic version output
and does not establish native operation of that release.

Executable refusal, installed version output, and help do not complete N02.
Missing or incompatible bridge-version refusal before a turn and no
alternate-harness fallback remain open in the full bridge agreement.

## Gotchas

Prime 0.9.6 removed Pi's `--list-models`. Use `model list`. A catalog without credentials is empty. Doctor success does not prove a turn or a provider login.

The default helper proves version admission, the literal `verify/fixture` model catalog row, and that model completing turns. The adapter probe adds two exact provider/model choices and changes model and effort through the public API and native terminal. Parser tests cover malformed and empty catalogs. Real-provider login and a live empty-catalog session remain separate qualification gaps.
