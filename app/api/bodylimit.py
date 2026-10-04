"""Pure-ASGI request-body cap. Counts bytes actually received, so it also stops chunked uploads
that send no Content-Length (which the Content-Length pre-check cannot)."""
from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _TooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        seen = 0
        started = False
        exceeded = False

        async def limited_receive() -> Message:
            nonlocal seen, exceeded
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.max_bytes:
                    exceeded = True
                    raise _TooLarge
            return msg

        async def send_413() -> None:
            nonlocal started
            body = json.dumps({"error": {"code": "payload_too_large", "message": "Request body too large",
                                         "request_id": None}}).encode()
            started = True
            await send({"type": "http.response.start", "status": 413, "headers": [
                (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                (b"connection", b"close")]})
            await send({"type": "http.response.body", "body": body})

        async def tracking_send(msg: Message) -> None:
            nonlocal started
            if exceeded and not started:
                # The parser downstream may have converted our exception into its own 4xx/5xx;
                # the real cause is the size cap, so report 413 instead.
                await send_413()
                return
            if exceeded and started:
                return  # swallow the rest of the downstream response
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _TooLarge:
            if started:  # response already underway: cannot change status, drop the connection
                raise
            await send_413()
        else:
            if exceeded and not started:
                await send_413()
