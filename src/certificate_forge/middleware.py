"""Bounded HTTP request bodies and lightweight request timing without buffering responses."""

import logging
import time
from uuid import uuid4

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)


class RequestMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid4())
        start = time.perf_counter()
        status = 500

        async def measured_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                elapsed = (time.perf_counter() - start) * 1000
                message.setdefault("headers", []).extend(
                    [
                        (b"x-request-id", request_id.encode()),
                        (b"server-timing", f"app;dur={elapsed:.2f}".encode()),
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                    ]
                )
            await send(message)

        try:
            if scope["method"] in {"POST", "PUT", "PATCH"}:
                body = bytearray()
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > self.max_body_bytes:
                        response = JSONResponse(
                            {
                                "detail": {
                                    "code": "BODY_TOO_LARGE",
                                    "message": "Request exceeds 2 MB.",
                                }
                            },
                            status_code=413,
                        )
                        await response(scope, receive, measured_send)
                        return
                    if not message.get("more_body", False):
                        break
                delivered = False

                async def replay_receive() -> Message:
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await receive()

                await self.app(scope, replay_receive, measured_send)
            else:
                await self.app(scope, receive, measured_send)
        finally:
            logger.info(
                "http_request request_id=%s method=%s path=%s status=%s duration_ms=%.2f",
                request_id,
                scope["method"],
                scope["path"],
                status,
                (time.perf_counter() - start) * 1000,
            )
