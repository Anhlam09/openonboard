"""Runs the OpenOnboard API inside the visitor's browser (Pyodide), for the static GitHub Pages demo.

The page's /api/... requests are handed to `handle()` by static-shim.js instead of going over the network.
The browser has no threads, so the few places FastAPI/Starlette would use a worker thread run inline instead.
"""

from __future__ import annotations

import asyncio
import os

os.environ.update(
    MIDDESK_API_KEY="",  # always mock mode: no keys, made-up results
    MIDDESK_WEBHOOK_SECRET="",
    REVIEWER_PASSWORD="",
    IN_BROWSER="true",
    DATABASE_PATH="/data/openonboard.db",
    DOCUMENTS_DIR="/data/uploads",
)

import fastapi.dependencies.utils  # noqa: E402
import fastapi.routing  # noqa: E402
import starlette.concurrency  # noqa: E402
import starlette.datastructures  # noqa: E402
from starlette.responses import FileResponse  # noqa: E402


async def _run_inline(func, *args, **kwargs):
    return func(*args, **kwargs)


for _module in (starlette.concurrency, starlette.datastructures, fastapi.routing, fastapi.dependencies.utils):
    if hasattr(_module, "run_in_threadpool"):
        _module.run_in_threadpool = _run_inline


async def _send_file(self, scope, receive, send):  # FileResponse normally reads the file on a worker thread
    with open(self.path, "rb") as f:
        data = f.read()
    await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
    await send({"type": "http.response.body", "body": data})


FileResponse.__call__ = _send_file

from app.main import app  # noqa: E402


async def handle(method: str, url: str, headers, body) -> dict:
    """Run one request through the FastAPI app and return {status, headers, body} for the browser."""
    path, _, query = url.partition("?")
    raw_body = body.to_bytes() if hasattr(body, "to_bytes") else b""  # JS null/undefined = no body
    header_list = [(str(k).lower(), str(v)) for k, v in (headers.to_py() if hasattr(headers, "to_py") else headers)]
    if raw_body and not any(k == "content-length" for k, _ in header_list):
        header_list.append(("content-length", str(len(raw_body))))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in header_list],
        "client": ("127.0.0.1", 0),
        "server": ("localhost", 443),
    }
    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": raw_body, "more_body": False}
        await asyncio.Event().wait()  # the visitor never "disconnects" mid-request

    result = {"status": 500, "headers": [], "body": b""}
    chunks: list[bytes] = []

    async def send(message):
        if message["type"] == "http.response.start":
            result["status"] = message["status"]
            result["headers"] = [[k.decode("latin-1"), v.decode("latin-1")] for k, v in message.get("headers", [])]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    result["body"] = b"".join(chunks)
    return result
