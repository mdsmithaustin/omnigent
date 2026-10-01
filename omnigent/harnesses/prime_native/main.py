from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import click
import httpx
import yaml

from omnigent.conversation_browser import conversation_url, open_conversation_link_if_enabled
from omnigent.entities.session_resources import (
    SessionResourceView,
    session_resource_view_to_dict,
    terminal_resource_id,
)
from omnigent.harnesses.pi_native.bridge import (
    clear_inbox,
    inject_relay_into_config,
    write_extension_files,
)
from omnigent.harnesses.prime_native.bridge import runtime_paths
from omnigent.harnesses.prime_native.process import (
    abandon_prime_launch,
    build_prime_launch,
    complete_prime_launch,
    reserve_prime_launch,
    resolve_prime_executable,
    stop_prime_runtime,
)
from omnigent.host.daemon_launch import (
    launch_or_reuse_daemon_runner,
    open_daemon_client,
    wait_for_host_online,
    wait_for_runner_online,
)
from omnigent.native.native_coding_agents import native_shell_terminal_spec
from omnigent.native.native_terminal import bind_session_runner, url_component
from omnigent.util.json_types import JsonObject

_logger = logging.getLogger(__name__)


def _materialize_prime_agent_spec(tmpdir: Path) -> Path:
    path = tmpdir / "prime-native-ui.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "prime-native-ui",
                "prompt": (
                    "Prime Native runs in the session terminal. "
                    "Web messages use its extension inbox."
                ),
                "executor": {"harness": "prime-native"},
                "spawn": True,
                "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
                "terminals": native_shell_terminal_spec(),
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def run_prime_native(
    *,
    server: str | None = None,
    session_id: str | None = None,
    extra_args: tuple[str, ...] = (),
    resume_picker: bool = False,
    auto_open_conversation: bool = False,
) -> None:
    from omnigent.chat import _bundle_agent, _remote_headers
    from omnigent.cli import _ensure_host_daemon
    from omnigent.host.identity import load_or_create_host_identity

    resolve_prime_executable()
    if shutil.which("tmux") is None:
        raise click.ClickException("Prime Native requires tmux on PATH.")
    if server is None:
        raise click.ClickException("Prime Native requires a resolved Omnigent server URL.")
    server = server.rstrip("/")
    headers = _remote_headers(server_url=server, host_id=None)
    if resume_picker:
        from omnigent_client import OmnigentClient

        from omnigent.repl._resume_picker import pick_conversation_by_wrapper_label_from_sdk

        async def pick() -> str | None:
            async with OmnigentClient(base_url=server, headers=headers or None) as client:
                return await pick_conversation_by_wrapper_label_from_sdk(
                    client,
                    wrapper_value="prime-native-ui",
                    agent_name="prime-native-ui",
                    host_id=load_or_create_host_identity().host_id,
                )

        session_id = asyncio.run(pick())
        if session_id is None:
            return
    _ensure_host_daemon(server)
    host_id = load_or_create_host_identity().host_id
    with TemporaryDirectory(prefix="omnigent-prime-native-") as directory:
        bundle = (
            None if session_id else _bundle_agent(_materialize_prime_agent_spec(Path(directory)))
        )
        asyncio.run(
            _attach_session(
                server, headers, host_id, session_id, bundle, extra_args, auto_open_conversation
            )
        )


async def _attach_session(
    server: str,
    headers: dict[str, str],
    host_id: str,
    session_id: str | None,
    bundle: bytes | None,
    extra_args: tuple[str, ...],
    auto_open: bool,
) -> None:
    fresh = session_id is None
    terminal_id = terminal_resource_id("prime-native", "main")
    async with open_daemon_client(
        server, headers, host_id, timeout=httpx.Timeout(30, read=120)
    ) as client:
        if session_id is None:
            if bundle is None:
                raise click.ClickException("Prime Native session bundle is missing.")
            metadata: JsonObject = {
                "labels": {"omnigent.ui": "terminal", "omnigent.wrapper": "prime-native-ui"}
            }
            if extra_args:
                metadata["terminal_launch_args"] = list(extra_args)
            response = await client.post(
                "/v1/sessions",
                data={"metadata": json.dumps(metadata)},
                files={"bundle": ("prime-native-ui.tar.gz", bundle, "application/gzip")},
            )
            response.raise_for_status()
            session_id = response.json()["session_id"]
        else:
            response = await client.get(f"/v1/sessions/{url_component(session_id)}")
            response.raise_for_status()
            if response.json().get("labels", {}).get("omnigent.wrapper") != "prime-native-ui":
                raise click.ClickException(
                    f"Conversation {session_id!r} is not a Prime Native session."
                )
            if extra_args:
                response = await client.patch(
                    f"/v1/sessions/{url_component(session_id)}",
                    json={"terminal_launch_args": list(extra_args)},
                )
                response.raise_for_status()
        assert session_id is not None
        await wait_for_host_online(client, host_id, timeout_s=30)
        runner_id = await launch_or_reuse_daemon_runner(
            client,
            host_id=host_id,
            session_id=session_id,
            workspace=str(Path.cwd().resolve()),
            fresh=fresh,
        )
        await wait_for_runner_online(client, runner_id, timeout_s=60)
        await bind_session_runner(client, session_id, runner_id)
        response = await client.post(
            f"/v1/sessions/{url_component(session_id)}/resources/terminals",
            json={
                "terminal": "prime-native",
                "session_key": "main",
                "ensure_native_terminal": True,
            },
            timeout=90,
        )
        response.raise_for_status()
        response = await client.get(
            f"/v1/sessions/{url_component(session_id)}/resources/terminals/{url_component(terminal_id)}"
        )
        response.raise_for_status()
        metadata = response.json().get("metadata", {})
    socket_path, target = metadata.get("tmux_socket"), metadata.get("tmux_target")
    if (
        not isinstance(socket_path, str)
        or not isinstance(target, str)
        or not Path(socket_path).exists()
    ):
        raise click.ClickException(
            "Prime Native terminal did not return reachable tmux attachment metadata."
        )
    click.echo(f"Web UI: {conversation_url(server, session_id)}", err=True)
    open_conversation_link_if_enabled(
        base_url=server,
        conversation_id=session_id,
        enabled=auto_open,
        warn=lambda message: click.echo(message, err=True),
    )
    env = dict(os.environ)
    env.pop("TMUX", None)
    process = await asyncio.create_subprocess_exec(
        "tmux", "-S", socket_path, "-f", os.devnull, "attach", "-t", target, env=env
    )
    await process.wait()
    click.echo(f"Resume with omnigent prime-native --resume {session_id}", err=True)


async def launch_prime_terminal(ctx: NativeLaunchContext) -> SessionResourceView:
    paths = runtime_paths(ctx.session_id)
    reservation = reserve_prime_launch(paths)
    dispatched = False
    reservation_active = True
    try:
        from omnigent.cli_auth import databricks_request_headers
        from omnigent.harnesses.claude_native.bridge import _TOOL_RELAY_FILE, _read_json_file
        from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
        from omnigent.runner._entry import (
            _make_auth_token_factory,
            _runner_tunnel_binding_token_from_env,
        )
        from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER
        from omnigent.runner.native.orchestration import (
            _agent_os_env_from_spec,
            _pi_native_launch_config,
            _unwrap_resolved_spec,
        )
        from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
        from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas

        config = await _pi_native_launch_config(
            session_id=ctx.session_id, server_client=ctx.server_client
        )
        executable = await asyncio.to_thread(resolve_prime_executable)
        await asyncio.to_thread(stop_prime_runtime, paths, launch_reservation=reservation)
        paths.prepare()
        clear_inbox(paths.root)
        factory = _make_auth_token_factory()
        token = await asyncio.to_thread(factory) if factory else None
        headers = databricks_request_headers(config.server_url, bearer_token=token)
        binding_token = _runner_tunnel_binding_token_from_env()
        if binding_token:
            headers[RUNNER_TUNNEL_TOKEN_HEADER] = binding_token
        spec = _unwrap_resolved_spec(ctx.agent_spec)
        tools = build_native_relay_tool_schemas(spec)
        if spec is not None and ctx.server_client is not None:
            try:
                mcp_schemas = await ProxyMcpManager(ctx.session_id, ctx.server_client).schemas_for(
                    spec
                )
                tools.extend(mcp_schemas.schemas)
                for server_name, error in mcp_schemas.failures.items():
                    _logger.warning(
                        "Prime Native MCP %r unavailable at launch: %s",
                        server_name,
                        error,
                        extra={"session_id": ctx.session_id},
                    )
            except Exception:
                _logger.exception(
                    "Failed to discover Prime Native MCP tools for session %s; "
                    "Prime will start without those MCP tools",
                    ctx.session_id,
                    extra={"session_id": ctx.session_id},
                )
        extension, extension_config = write_extension_files(
            paths.root,
            session_id=ctx.session_id,
            server_url=config.server_url,
            conversation_url=conversation_url(config.server_url, ctx.session_id),
            auth_headers=headers,
            tools=tools,
            agent_label="Prime Native",
        )
        from omnigent.harnesses.pi_native.bridge import _atomic_text

        extension_settings = json.loads(extension_config.read_text())
        extension_settings["primeControlsDir"] = str(paths.root / "controls")
        _atomic_text(extension_config, json.dumps(extension_settings))
        from omnigent.runtime.prompt import build_instructions_nullable

        instructions = build_instructions_nullable(spec, None, tools) if spec else None
        model = config.model_override or (spec.executor.model if spec else None)
        launch = build_prime_launch(
            paths,
            executable=executable,
            extension=extension,
            config=extension_config,
            extra_args=config.terminal_launch_args or (),
            external_session_id=config.external_session_id,
            model=model,
            reasoning_effort=config.reasoning_effort,
            instructions=instructions,
        )
        agent_os_env = _agent_os_env_from_spec(ctx.agent_spec)
        dispatched = True
        view = await ctx.resource_registry.launch_required_terminal(
            session_id=ctx.session_id,
            terminal_name="prime-native",
            session_key="main",
            resource_role="prime-native",
            parent_os_env=agent_os_env,
            spec=TerminalEnvSpec(
                os_env=OSEnvSpec(
                    type="caller_process",
                    cwd=str(config.workspace),
                    sandbox=agent_os_env.sandbox if agent_os_env else None,
                ),
                command=sys.executable,
                args=[
                    "-m",
                    "omnigent.harnesses.prime_native.process",
                    str(paths.root),
                    launch.executable,
                    *launch.argv,
                ],
                env=dict(launch.env),
                scrollback=100_000,
                tmux_allow_passthrough=True,
                tmux_start_on_attach=False,
                keep_alive_after_exit=True,
            ),
        )
        await asyncio.to_thread(complete_prime_launch, paths, reservation)
        reservation_active = False
        ctx.publish_event(
            ctx.session_id,
            {"type": "session.resource.created", "resource": session_resource_view_to_dict(view)},
        )
        if ctx.server_client is not None and ctx.ensure_comment_relay is not None:
            await ctx.ensure_comment_relay(
                ctx.session_id, explicit_bridge_dir=paths.root, await_notify=False
            )
            relay = _read_json_file(paths.root / _TOOL_RELAY_FILE)
            if relay and isinstance(relay.get("url"), str) and isinstance(relay.get("token"), str):
                inject_relay_into_config(paths.root, relay["url"], relay["token"])
        return view
    except BaseException:
        if reservation_active:
            await asyncio.to_thread(
                abandon_prime_launch, paths, reservation, dispatched=dispatched
            )
        raise


if TYPE_CHECKING:
    from omnigent.runner.native.orchestration import NativeLaunchContext
