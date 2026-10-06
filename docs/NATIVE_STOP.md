# Stop before forking with a different agent

On a native session, choose **Stop session** from the sidebar menu before
forking with a different agent. The source owner must perform Stop. A reader
can fork a source after its owner has verified shutdown, but cannot Stop it.
The conversation and saved native history remain available.

Stop reports `verified`, `unknown`, or `failed`. Only `verified` permits a
different-agent fork. Idle status, a missing terminal, an offline runner, or a
successful HTTP acknowledgement does not prove shutdown. The web client shows
an unresolved result as an error; CLI shutdown also reports it as an error.
`GET /v1/sessions/{id}` includes the latest `native_stop` result when one exists.

Prime Native can verify closure when its current private owner, retained
terminal handles, owned processes, and required socket all confirm shutdown.
This proof covers the recorded local runner environment and private runtime.
Other providers and unsupported modes return an unqualified result. A source
without recorded owner admission also remains unqualified. This does not
establish the absence of arbitrary escaped child processes.
For a source without recorded admission, Stop still attempts the existing
runner shutdown and dedicated host-runner shutdown. It returns `unknown` even
when those shutdown requests succeed, and different-agent fork stays blocked.

A pending close continues to exclude new native input and startup. It does not
expire. An unqualified close whose actor has finished permits ordinary resume,
while different-agent fork remains blocked. Sending new input, reconnecting, or
creating a native terminal deliberately resumes the source and clears its
verified Stop. Use Stop again before choosing a different agent. A runner or
host replacement retains unresolved predecessor ownership; it cannot prove
that predecessor absent.

Same-agent history forks and ordinary resume keep their existing behavior.
`switch-agent` remains unavailable (HTTP 410). Native terminals cannot transfer
to another session because closure depends on their original source owner.
Ordinary shell transfer remains available.

## HTTP and SDK result

Post the owner event through the existing endpoint:

```python
result = await client.sessions.post_event(
    source_id, {"type": "stop_session", "data": {}}
)
assert result["native_stop"]["outcome"] == "verified", result
```

HTTP 202 means the request was handled; inspect `native_stop.outcome` before
forking. A different-agent fork returns HTTP 409 until current native closure
is verified. Selecting the same original agent does not require this proof.

## Admission and insertion boundary

The server persists a private source epoch, exact current owner, and closure
state in the conversation database. Concurrent ordinary inputs share an epoch.
Stop revokes it before attempting physical closure. Initialization, provider
startup, terminal creation, and deferred input carry their original admission;
the provider validates it again under its cross-process creation lock. Prime's
terminal wrapper consumes its existing launch reservation under that lock.

On permission-enabled servers, native owner admission and validation require
the current runner's tunnel binding token or a configured runner-token allowlist.
Session edit or owner permission alone cannot record native ownership. Local
servers without a permission store retain the existing single-user trust
boundary, including direct CLI Codex startup before runner binding.

For sources with recorded native ownership, skill commands capture admission at
accepted event ingress, before skill resolution. Stop revokes a deferred
command's original ticket. A command revoked during resolution returns a
rejected delivery and cannot reopen the source.
Retrying its stable invocation ID returns the recorded delivery without
resending it. A new invocation can deliberately resume the source after
completed closure.

A native session without recorded owner admission rejects skill dispatch before
delivery. Start or resume its native terminal first. After the owner is ready,
send a new skill invocation. The original invocation retains its rejected
delivery and cannot execute on retry.

The existing fork insertion transaction locks the source, compares the selected
agent before creating its clone, and checks current native closure before
inserting the destination. Metadata and conversation transactions stay
separate. A later metadata, clone, or launch error is still an error, even if a
partial destination already exists. Retrying fork is not an idempotency contract.

## Verify the behavior

With the installed fork packages and a running Prime Native source:

1. Leave the source idle, then try a fork with a different agent. Expect the
   explicit Stop error and no successful destination.
2. As a reader, try Stop. Expect permission denied. A same-agent history fork
   remains available.
3. As the source owner, choose Stop. Check that the result is `verified` and
   that the retained native terminal, owned processes, and private socket have
   closed. A reader can now fork with a different agent.
4. Resume the original source with a new message. A different-agent fork is
   blocked again until a new verified Stop.
5. Repeat with an unsupported provider or an unresolved close. Expect an
   unqualified result and a blocked different-agent fork. Ordinary resume is
   available only after the close actor has finished.

Controlled regressions, without a native model call:

```bash
uv run --no-sync pytest tests/stores/test_native_source_stop.py tests/server/integration/test_native_source_stop.py tests/runner/test_native_source_admission.py tests/cli/test_native_source_stop.py -q
```

To check installed packages against an existing owned Prime source, run
`verify_stop_fork.py` with the Python entry from the wheel environment:

```bash
/absolute/wheel-venv/bin/python .agents/skills/verify-prime-native/scripts/verify_stop_fork.py --server http://127.0.0.1:8123 --source SOURCE_ID --target-agent TARGET_AGENT_ID --owner-headers /tmp/owner.json --reader-headers /tmp/reader.json --evidence /tmp/stop-fork-evidence
```

The header files contain JSON request headers for each identity. Grant the reader
access to the source and target agent first. The driver requires installed module
origins, checks the owner and reader HTTP results, and records the destination.
The caller owns source setup and cleanup of both sessions. Use a fresh evidence
directory. Adding `--expected-stop WRONG` must fail at the literal Stop result.

Run the separately maintained [Prime adapter verification](PRIME_NATIVE.md#verify-the-adapter)
for real runtime evidence. Controlled fixtures and this guide do not certify a
published package or qualify a live native runtime.
