from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.errors import OmnigentError
from omnigent.native.admission import native_owner
from omnigent.native.source_owner import NativeAdmission, NativeAdmissionRequest, NativeStop
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


class NativeSourceServer:
    def __init__(self, root: Path) -> None:
        self.store = SqlAlchemyConversationStore(f"sqlite:///{root / 'native-source.db'}")
        self.url = ""

    def source(self, alias: str) -> str:
        source_id = hashlib.sha256(alias.encode()).hexdigest()[:32]
        if self.store.get_conversation(source_id) is None:
            self.store.create_conversation(conversation_id=source_id)
        return source_id

    def respond(
        self, request: httpx.Request, snapshot: dict[str, Any] | None = None
    ) -> httpx.Response:
        parts = request.url.path.split("/")
        if len(parts) < 5 or parts[4] != "native-admission":
            return httpx.Response(200, json=snapshot or {})
        alias = parts[3]
        source_id = self.source(alias)
        body = json.loads(request.content)
        try:
            if len(parts) == 5:
                admission_request = NativeAdmissionRequest.model_validate(body)
                ticket = self.store.admit_native(
                    source_id,
                    admission_request.owner,
                    expected_epoch=admission_request.expected_epoch,
                ).model_copy(update={"source_id": alias})
                return httpx.Response(200, json=ticket.model_dump())
            if "operation_id" in body:
                stop = NativeStop.model_validate(body)
                stop = stop.model_copy(
                    update={
                        "admission": stop.admission.model_copy(update={"source_id": source_id})
                    }
                )
                self.store.validate_native_stop(stop)
            else:
                ticket = NativeAdmission.model_validate(body).model_copy(
                    update={"source_id": source_id}
                )
                self.store.validate_native_admission(ticket)
            return httpx.Response(200, json={"current": True})
        except OmnigentError as exc:
            return httpx.Response(exc.http_status, json={"error": exc.message})

    def write_prime_config(self, runtime: Path) -> None:
        alias = runtime.name
        ticket = self.store.admit_native(self.source(alias), native_owner(alias, "prime-native"))
        ticket = ticket.model_copy(update={"source_id": alias})
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "config.json").write_text(
            json.dumps(
                {
                    "nativeAdmission": ticket.model_dump(),
                    "serverUrl": self.url,
                    "authHeaders": {},
                }
            )
        )


@pytest.fixture
def native_source_server(tmp_path: Path) -> Iterator[NativeSourceServer]:
    source = NativeSourceServer(tmp_path)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            response = source.respond(
                httpx.Request(
                    "POST",
                    f"{source.url}{self.path}",
                    content=self.rfile.read(length),
                )
            )
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(response.content)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    source.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield source
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
