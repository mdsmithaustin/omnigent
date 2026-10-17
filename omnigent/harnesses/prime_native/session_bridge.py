"""
Mirror a Prime 0.10 TUI session into its Omnigent session over the daemon socket.

Prime 0.10 cannot load the shared Pi extension, so this bridge does the
extension's Prime-mode jobs with the same files and routes: session events go
to ``POST /events``, ``inbox/`` messages become ``prompt`` commands, and
``controls/requests`` become daemon commands answered in ``controls/results``.
It runs on its own event loop inside the terminal wrapper, next to Prime.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import re
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from omnigent.harnesses.pi_native.bridge import _atomic_text, config_path
from omnigent.harnesses.prime_native.bridge import PrimeRuntimePaths
from omnigent.harnesses.prime_native.daemon import (
    DaemonClient,
    DaemonClosedError,
    DaemonCommandError,
    DaemonError,
)
from omnigent.harnesses.prime_native.transcript import TranscriptState, reconcile, reduce
from omnigent.native import owner_claim

_logger = logging.getLogger(__name__)

_TICK_S = 0.25
_STOP_TIMEOUT_S = 2.0
_RESULT_TTL_S = 60.0
_MAX_PROMPT_ATTEMPTS = 5
_DELIVERED_CAP = 4096
_LOG_BYTES = 1_000_000
_POST_ATTEMPTS = 3
_REPOST_FIRST_S = 1.0
_REPOST_MAX_S = 30.0
_REQUEST_NAME = re.compile(r"[a-f0-9]{32}\.json")
_SUSPENDED = "queued session input is suspended"
_STATUS = "external_session_status"

Event = dict[str, Any]
Outcome = dict[str, object]


def _now_ms() -> float:
    return time.time() * 1000


def _model_selector(model: object) -> str | None:
    if not isinstance(model, dict):
        return None
    provider, model_id = model.get("provider"), model.get("id")
    if not isinstance(provider, str) or not isinstance(model_id, str) or not model_id:
        return None
    return f"{provider}/{model_id}" if provider else model_id


def _effort(level: object) -> str | None:
    """Prime's thinking level in Omnigent's spelling (``off`` is ``none``)."""
    return ("none" if level == "off" else level) if isinstance(level, str) and level else None


class SessionBridge:
    def __init__(self, paths: PrimeRuntimePaths, socket_path: Path, http: httpx.AsyncClient):
        config = json.loads(config_path(paths.root).read_text(encoding="utf-8"))
        self._paths = paths
        self._socket_path = socket_path
        self._http = http
        self._session_url = (
            f"{config['serverUrl']}/v1/sessions/{quote(config['sessionId'], safe='')}"
        )
        self._inbox = Path(config["inboxDir"])
        self._policy_notice: str | None = config.get("policyNotice")
        self._incarnation = str(uuid.uuid4())
        self._transcript = TranscriptState(agent_label=config.get("agentLabel") or "Prime Native")
        # An item failed to post; the next attach snapshot re-posts it.
        self._unposted = False
        # The newest status, when its post failed; a re-attach re-posts it.
        self._lost_status: Event | None = None
        self._repost_at = 0.0
        self._repost_delay = _REPOST_FIRST_S
        self._unreachable = False
        self._claimed = False
        self._client: DaemonClient | None = None
        self._session = ""
        self._posted_settings: dict[str, str] = {}
        self._prompt_attempts: dict[str, int] = {}
        # Ids of consumed inbox messages, oldest first, so a re-read file is not prompted twice.
        self._delivered: dict[str, None] = {}
        self._settings_lock = asyncio.Lock()
        self._attached = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()

    async def run(self, stop: asyncio.Event) -> None:
        # Separate tasks, so a slow daemon reply cannot stale the binding or hold up a control.
        loops = [
            asyncio.create_task(self._session_loop()),
            *(
                asyncio.create_task(self._every_tick(step))
                for step in (
                    self._heartbeat,
                    self._dispatch_controls,
                    self._deliver_inbox,
                    self._reattach_if_needed,
                )
            ),
        ]
        try:
            await stop.wait()
        finally:
            for task in (*loops, *self._tasks):
                task.cancel()
            await asyncio.gather(*loops, *self._tasks, return_exceptions=True)
            self._release_binding()

    async def _session_loop(self) -> None:
        first_attach = True
        last_failure = ""
        while True:
            try:
                async with await DaemonClient.connect(self._socket_path) as client:
                    root = await self._root_session(client)
                    await self._attach(client, root, post_history=not first_attach)
                    first_attach, last_failure = False, ""
                    async for envelope in client.events():
                        if envelope.get("activeSessionId") != self._session:
                            continue
                        await self._mirror(client, envelope)
                        if self._unposted and envelope.get("event", {}).get("type") == "agent_end":
                            break
            except Exception as exc:
                # Retried every tick, so only a changed failure is logged.
                if repr(exc) != last_failure:
                    last_failure = repr(exc)
                    if isinstance(exc, (DaemonError, OSError)):
                        _logger.warning("Prime daemon unavailable: %r", exc)
                    else:
                        _logger.exception("Prime session mirror failed")
            finally:
                self._client = None
            await asyncio.sleep(_TICK_S)

    async def _root_session(self, client: DaemonClient) -> dict[str, Any]:
        waiting = False
        while (root := self._tui_root(await client.command("list"))) is None:
            if not waiting:
                waiting = True
                _logger.info("Waiting for a Prime root session under %s", self._paths.session_dir)
            await asyncio.sleep(_TICK_S)
        return root

    def _tui_root(self, listing: dict[str, Any]) -> dict[str, Any] | None:
        """
        The root session the TUI shows, since ``/new`` and ``/resume`` switch it.

        That is the root with a client besides this bridge, else the root the bridge
        last attached, else the first root under this runtime's session dir.
        """
        session_dir = self._paths.session_dir.resolve()
        roots = [
            session
            for session in listing.get("sessions", [])
            if session.get("rlmDepth") == 0
            and isinstance(session.get("sessionFile"), str)
            and Path(session["sessionFile"]).resolve().is_relative_to(session_dir)
        ]
        for root in roots:
            # The count includes this bridge on its root, briefly also after it detached.
            own = 1 if root.get("id") == self._session else 0
            clients = root.get("attachedClients")
            if isinstance(clients, int) and clients > own:
                return root
        current = next((root for root in roots if root.get("id") == self._session), None)
        return current or (roots[0] if roots else None)

    async def _reattach_if_needed(self) -> None:
        """Re-attach when the TUI moved to another root, or to retry a failed re-post once idle."""
        client = self._client
        if client is None:
            return
        root = self._tui_root(await client.command("list"))
        if root is not None and root.get("id") != self._session:
            _logger.info("Prime TUI moved to session %s; re-attaching", root.get("id"))
            await client.close()
        elif (
            self._unposted
            and self._transcript.loop_response_id is None
            and time.monotonic() >= self._repost_at
        ):
            await client.close()

    async def _attach(self, client: DaemonClient, root: dict[str, Any], *, post_history: bool):
        session = str(root["id"])
        native_id = root.get("sessionId")
        if isinstance(native_id, str) and native_id:
            # Prime resumes by this stable UUID; the runtime id changes on every resume.
            with contextlib.suppress(httpx.HTTPError):
                await self._http.patch(
                    self._session_url,
                    json={"external_session_id": native_id},
                    headers=self._headers(),
                )
        snapshot = (await client.command("attach", activeSessionId=session)).get("snapshot", {})
        # The first attach after launch replays history Omnigent already holds.
        self._transcript, history = reconcile(
            self._transcript,
            snapshot.get("messages", []),
            streaming=bool(snapshot.get("state", {}).get("isStreaming")),
        )
        self._unposted = False
        if post_history:
            if self._lost_status and not any(event["type"] == _STATUS for event in history):
                history.append(self._lost_status)
            await self._post_all(history)
        # Omnigent is still unreachable, so retry on a backoff while Prime stays idle.
        if self._unposted:
            self._repost_at = time.monotonic() + self._repost_delay
            self._repost_delay = min(2 * self._repost_delay, _REPOST_MAX_S)
        else:
            self._repost_delay = _REPOST_FIRST_S
        self._client, self._session = client, session
        self._attached.set()
        _logger.info("Attached to Prime session %s (%s)", session, native_id)
        await self._publish_settings(snapshot.get("state", {}))
        with contextlib.suppress(DaemonCommandError):
            catalog = await client.command("get_available_models", activeSessionId=session)
            options = [
                {"id": selector, "model": selector, "displayName": model.get("name") or selector}
                for model in catalog.get("models", [])
                if (selector := _model_selector(model))
            ]
            await self._post({"type": "external_model_options", "data": {"models": options}})
        if self._policy_notice and await self._post(_policy_notice(self._policy_notice)):
            self._policy_notice = None

    async def _mirror(self, client: DaemonClient, envelope: dict[str, Any]) -> None:
        self._transcript, events = reduce(self._transcript, envelope)
        await self._post_all(events)
        if envelope.get("event", {}).get("type") == "agent_end":
            # The daemon emits no event for a model or effort picked in the TUI. A re-attach
            # can close the client first; its snapshot then carries the settings.
            async with self._settings_lock:
                with contextlib.suppress(DaemonCommandError, DaemonClosedError):
                    state = await client.command("get_state", activeSessionId=self._session)
                    await self._publish_settings(state)

    async def _publish_settings(self, state: dict[str, Any], *, force: str | None = None) -> bool:
        """Post each setting that changed since the last post, plus *force* regardless."""
        observed = {
            "model": _model_selector(state.get("model")),
            "effort": _effort(state.get("thinkingLevel")),
        }
        published = True
        for kind, value in observed.items():
            if value is None or (value == self._posted_settings.get(kind) and kind != force):
                continue
            event_type, key = _SETTING_EVENTS[kind]
            if await self._post({"type": event_type, "data": {key: value}}):
                self._posted_settings[kind] = value
            else:
                published = False
        return published

    async def _every_tick(self, step: Callable[[], Awaitable[None]]) -> None:
        await self._attached.wait()
        last_failure = ""
        while True:
            try:
                await step()
                last_failure = ""
            except Exception as exc:
                # Retried every tick, so only a changed failure is logged.
                if repr(exc) != last_failure:
                    last_failure = repr(exc)
                    if isinstance(exc, (DaemonError, OSError)):
                        _logger.warning("Prime bridge %s failed: %r", step.__name__, exc)
                    else:
                        _logger.exception("Prime bridge %s failed", step.__name__)
            await asyncio.sleep(_TICK_S)

    async def _deliver_inbox(self) -> None:
        client = self._client
        if client is None or not self._owns_binding():
            return
        ready = self._paths.root / "input_ready"
        if not ready.exists():
            ready.write_text("")
        for path in sorted(self._inbox.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            content = payload.get("content") if isinstance(payload, dict) else None
            if (
                not isinstance(content, str)
                or payload.get("type") != "user_message"
                or payload.get("id") in self._delivered
            ):
                path.unlink(missing_ok=True)
                continue
            if client.closed:
                return
            try:
                # Steer joins a running turn and starts one when idle.
                await client.command(
                    "prompt",
                    activeSessionId=self._session,
                    message=content,
                    streamingBehavior="steer",
                )
            except DaemonCommandError as exc:
                if _SUSPENDED in exc.error:
                    # An abort or a TUI Esc pauses input until resumed; 0.10.0 still admits steer.
                    with contextlib.suppress(DaemonCommandError):
                        await client.command("resume_queue", activeSessionId=self._session)
                attempts = self._prompt_attempts.get(path.name, 0) + 1
                self._prompt_attempts[path.name] = attempts
                if attempts < _MAX_PROMPT_ATTEMPTS:
                    return
                outcome = (
                    f"could not be delivered to Prime after {attempts} attempts and was dropped"
                )
                await self._post(_lost_message(payload, f"{outcome}: {exc.error}"))
            except DaemonError as exc:
                # Prime may have run it before the connection failed, so a resend could repeat it.
                outcome = "may not have reached Prime and was not resent"
                await self._post(_lost_message(payload, f"{outcome}: {exc}"))
            self._remember(payload.get("id"))
            self._prompt_attempts.pop(path.name, None)
            path.unlink(missing_ok=True)

    def _remember(self, message_id: object) -> None:
        if isinstance(message_id, str):
            self._delivered[message_id] = None
            if len(self._delivered) > _DELIVERED_CAP:
                del self._delivered[next(iter(self._delivered))]

    async def _heartbeat(self) -> None:
        # The first beat claims the binding, as the extension does on load; later beats only
        # renew it, so a superseded bridge goes quiet instead of taking it back.
        eligible = self._owns_binding() if self._claimed else self._owns_runtime()
        if not eligible:
            return
        binding = {"incarnation": self._incarnation, "pid": os.getpid(), "heartbeat": _now_ms()}
        _atomic_text(self._paths.controls_dir / "binding.json", json.dumps(binding))
        self._claimed = True

    def _owns_runtime(self) -> bool:
        return owner_claim.held_by_current_process(self._paths.root)

    def _owns_binding(self) -> bool:
        try:
            record = json.loads((self._paths.controls_dir / "binding.json").read_text())
        except (OSError, ValueError):
            return False
        return (
            isinstance(record, dict)
            and record.get("incarnation") == self._incarnation
            and self._owns_runtime()
        )

    def _release_binding(self) -> None:
        binding = self._paths.controls_dir / "binding.json"
        with contextlib.suppress(OSError, ValueError):
            if json.loads(binding.read_text()).get("incarnation") == self._incarnation:
                binding.unlink()

    async def _dispatch_controls(self) -> None:
        if not self._owns_binding():
            return
        results = self._paths.controls_dir / "results"
        for path in results.iterdir():
            with contextlib.suppress(FileNotFoundError):
                if time.time() - path.stat().st_mtime > _RESULT_TTL_S:
                    path.unlink()
        for path in sorted((self._paths.controls_dir / "requests").iterdir()):
            if not _REQUEST_NAME.fullmatch(path.name):
                continue
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
                path.unlink()
            except (OSError, ValueError):
                continue
            if isinstance(request, dict) and request.get("id") == path.stem:
                task = asyncio.create_task(self._answer(request))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _answer(self, request: dict[str, Any]) -> None:
        if request.get("type") in ("model", "effort"):
            async with self._settings_lock:
                outcome = await self._apply(request)
        else:
            outcome = await self._apply(request)
        if not self._owns_binding():
            return
        result = {"id": request["id"], "incarnation": request.get("incarnation"), **outcome}
        results = self._paths.controls_dir / "results"
        _atomic_text(results / f"{request['id']}.json", json.dumps(result))

    async def _apply(self, request: dict[str, Any]) -> Outcome:
        expires_at = request.get("expiresAt")
        if (
            request.get("incarnation") != self._incarnation
            or not isinstance(expires_at, (int, float))
            or expires_at <= _now_ms()
        ):
            return {"status": "rejected", "detail": "Expired or stale Prime control"}
        handler = _CONTROLS.get(str(request.get("type")))
        client = self._client
        if handler is None:
            return {"status": "rejected", "detail": "Unsupported Prime control"}
        if client is None:
            return {"status": "unavailable", "detail": "Prime daemon is not attached"}
        try:
            async with asyncio.timeout((expires_at - _now_ms()) / 1000):
                return await handler(self, client, request)
        except DaemonCommandError as exc:
            return {"status": "rejected", "detail": exc.error}
        except (TimeoutError, DaemonError) as exc:
            _logger.warning("Prime control %s got no answer: %r", request.get("type"), exc)
            return {"status": "unknown", "detail": "Prime control outcome unknown; not retried"}
        except Exception:
            _logger.exception("Prime control %s failed", request.get("type"))
            return {"status": "unknown", "detail": "Prime control outcome unknown; not retried"}

    async def _set_model(self, client: DaemonClient, request: dict[str, Any]) -> Outcome:
        selector = f"{request.get('provider')}/{request.get('modelId')}"
        await client.command(
            "set_model",
            activeSessionId=self._session,
            provider=request.get("provider"),
            modelId=request.get("modelId"),
        )
        state = await client.command("get_state", activeSessionId=self._session)
        if _model_selector(state.get("model")) != selector or not await self._publish_settings(
            state, force="model"
        ):
            return {"status": "unknown", "detail": "Prime model observation was not confirmed"}
        return {"status": "applied", "model": selector}

    async def _set_effort(self, client: DaemonClient, request: dict[str, Any]) -> Outcome:
        level = request.get("level")
        await client.command("set_thinking_level", activeSessionId=self._session, level=level)
        state = await client.command("get_state", activeSessionId=self._session)
        effort = _effort(state.get("thinkingLevel"))
        if effort is None or not await self._publish_settings(state, force="effort"):
            return {"status": "unknown", "detail": "Prime effort observation was not confirmed"}
        return {"status": "applied", "effort": effort}

    async def _compact(self, client: DaemonClient, request: dict[str, Any]) -> Outcome:
        # compaction_start/compaction_end events carry the progress to the web UI.
        instructions = request.get("customInstructions")
        fields = {"customInstructions": instructions} if instructions else {}
        await client.command("compact", activeSessionId=self._session, **fields)
        return {"status": "applied"}

    async def _interrupt(self, client: DaemonClient, _request: dict[str, Any]) -> Outcome:
        state = await client.command("get_state", activeSessionId=self._session)
        if not state.get("isStreaming"):
            return {"status": "unavailable", "detail": "Prime loop is not abortable"}
        await client.command("abort", activeSessionId=self._session)
        # abort suspends queued input; resume_queue lifts that even when nothing is queued.
        with contextlib.suppress(DaemonCommandError):
            await client.command("resume_queue", activeSessionId=self._session)
        return {"status": "interrupt_accepted"}

    def _headers(self) -> dict[str, str]:
        # The executor refreshes authHeaders in config.json as its bearer rotates.
        try:
            auth = json.loads(config_path(self._paths.root).read_text()).get("authHeaders")
        except (OSError, ValueError):
            auth = None
        return {"content-type": "application/json", **(auth if isinstance(auth, dict) else {})}

    async def _post_all(self, events: list[Event]) -> None:
        for event in events:
            lost = await self._post(event) is None
            if event["type"] == _STATUS:
                self._lost_status = event if lost else None
                self._unposted |= lost
            elif lost and (source_id := event["data"].get("source_id")):
                posted = self._transcript.posted - {source_id}
                self._transcript = replace(self._transcript, posted=posted)
                self._unposted = True

    async def _post(self, event: Event) -> bool | None:
        """
        POST one event, retrying while the server is unreachable.

        :returns: ``True`` once accepted, ``False`` when the server rejects it,
            ``None`` when it stayed unreachable.
        """
        for attempt in range(_POST_ATTEMPTS):
            if attempt:
                await asyncio.sleep(0.05 * 2 ** (attempt - 1))
            try:
                response = await self._http.post(
                    f"{self._session_url}/events", json=event, headers=self._headers()
                )
            except httpx.HTTPError:
                response = None
            if response is not None and not _retryable(response.status_code):
                if self._unreachable:
                    self._unreachable = False
                    _logger.info("Prime bridge reaches Omnigent again")
                if not response.is_success:
                    _logger.warning(
                        "Omnigent rejected %s: %s", event["type"], response.status_code
                    )
                return response.is_success
        if not self._unreachable:
            self._unreachable = True
            _logger.warning(
                "Prime bridge cannot post to Omnigent; failed items wait for a re-attach"
            )
        return None


_SETTING_EVENTS = {
    "model": ("external_model_change", "model"),
    "effort": ("external_reasoning_effort_change", "reasoning_effort"),
}

_CONTROLS: dict[
    str, Callable[[SessionBridge, DaemonClient, dict[str, Any]], Awaitable[Outcome]]
] = {
    "model": SessionBridge._set_model,
    "effort": SessionBridge._set_effort,
    "compact": SessionBridge._compact,
    "interrupt": SessionBridge._interrupt,
}


def _retryable(status: int) -> bool:
    return status in (408, 429) or status >= 500


def _policy_notice(message: str) -> Event:
    # The web UI shows this code as a standing banner, as for codex-native's degraded hook.
    return {
        "type": "external_conversation_item",
        "data": {
            "response_id": "prime-policy-notice",
            "item_type": "error",
            "item_data": {
                "source": "execution",
                "code": "native_policy_not_enforced",
                "message": message,
            },
            "source_id": "prime:policy-notice",
        },
    }


def _lost_message(payload: dict[str, Any], outcome: str) -> Event:
    content = str(payload.get("content"))
    preview = content if len(content) <= 80 else f"{content[:80]}…"
    message = (
        f"Omnigent: a queued message (id {payload.get('id') or '(no id)'}) "
        f"{outcome.rstrip('.')}. Content preview: {json.dumps(preview, ensure_ascii=False)}"
    )
    return {
        "type": "external_conversation_item",
        "data": {
            "response_id": f"pi-deliver-dropped-{int(_now_ms())}",
            "item_type": "error",
            "item_data": {
                "source": "execution",
                "code": "pi_followup_delivery_dropped",
                "message": message,
            },
        },
    }


async def run_bridge(
    paths: PrimeRuntimePaths,
    socket_path: Path,
    stop: asyncio.Event,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Mirror the Prime session behind *socket_path* until *stop* is set."""
    async with httpx.AsyncClient(timeout=5.0, transport=transport) as http:
        await SessionBridge(paths, socket_path, http).run(stop)


def start_in_thread(paths: PrimeRuntimePaths, socket_path: Path) -> Callable[[], None]:
    """Run the bridge on a daemon thread; the returned callable stops it."""
    # The wrapper's stderr is Prime's terminal, so this process logs to the runtime root.
    handler = logging.handlers.RotatingFileHandler(
        paths.root / "bridge.log", maxBytes=_LOG_BYTES, backupCount=1
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    _logger.setLevel(logging.INFO)
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()

    def serve() -> None:
        try:
            loop.run_until_complete(run_bridge(paths, socket_path, stop))
        except Exception:
            _logger.exception("Prime session bridge stopped")
        finally:
            loop.close()

    thread = threading.Thread(target=serve, name="prime-session-bridge", daemon=True)
    thread.start()

    def shutdown() -> None:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(stop.set)
        thread.join(_STOP_TIMEOUT_S)
        logging.getLogger().removeHandler(handler)
        handler.close()

    return shutdown
