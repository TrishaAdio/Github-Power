"""Passcode gate: a tiny ASGI middleware that guards the MCP endpoint.

The AI can present the passcode in any of these ways:

1. ``Authorization: Bearer <passcode>``
2. ``X-AI-Passcode: <passcode>``
3. In the URL path:  ``http://host:5000/<passcode>/mcp``
4. As a query param: ``http://host:5000/mcp?code=<passcode>``

Option 3 is the most portable, since some MCP clients only accept a bare URL.
"""

from __future__ import annotations

import hmac
import json
from typing import Any
from urllib.parse import parse_qs


def _unauthorized_payload(reason: str) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32001, "message": f"Unauthorized: {reason}"},
        }
    ).encode()


class PasscodeGate:
    """Rejects any request that does not carry the shared passcode."""

    def __init__(
        self, app: Any, passcode: str, on_reject=None, allow_in_url: bool = True
    ) -> None:
        self.app = app
        self.passcode = passcode
        self.on_reject = on_reject
        # When False, only the headers are accepted: the passcode never appears in
        # a URL, so it cannot leak through proxy access logs, history or screenshots.
        self.allow_in_url = allow_in_url

    def _matches(self, candidate: str | None) -> bool:
        if not candidate:
            return False
        return hmac.compare_digest(candidate.strip(), self.passcode)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path: str = scope.get("path", "/")

        # Open, unauthenticated liveness probe.
        if path in ("/healthz", "/health"):
            body = json.dumps({"status": "ok", "service": "github-mcp"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        authorized = False
        reason = "missing passcode"

        # 3) Passcode baked into the URL path -> strip it before forwarding.
        prefix = "/" + self.passcode
        if self.allow_in_url and (path == prefix or path.startswith(prefix + "/")):
            stripped = path[len(prefix) :] or "/"
            scope = dict(scope)
            scope["path"] = stripped
            scope["raw_path"] = stripped.encode()
            authorized = True
        else:
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}

            # 1) Authorization: Bearer <passcode>
            auth = headers.get("authorization", "")
            if auth[:7].lower() == "bearer ":
                if self._matches(auth[7:]):
                    authorized = True
                else:
                    reason = "bad passcode"

            # 2) X-AI-Passcode header
            if not authorized:
                for name in ("x-ai-passcode", "x-passcode", "x-api-key"):
                    if name in headers:
                        if self._matches(headers[name]):
                            authorized = True
                        else:
                            reason = "bad passcode"
                        break

            # 4) ?code= / ?passcode= query parameter
            if not authorized and self.allow_in_url:
                query = parse_qs(scope.get("query_string", b"").decode())
                for key in ("code", "passcode", "key"):
                    if key in query:
                        if self._matches(query[key][0]):
                            authorized = True
                        else:
                            reason = "bad passcode"
                        break

        if not authorized:
            if self.on_reject:
                client = scope.get("client") or ("?", 0)
                self.on_reject(f"{client[0]} -> {path} ({reason})")
            body = _unauthorized_payload(reason)
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"www-authenticate", b'Bearer realm="github-mcp"'),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)
