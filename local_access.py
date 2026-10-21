"""Browser request protection for the loopback-only UI on port 8765.

These checks prevent cross-site requests and DNS rebinding; they are not user
authentication. Keep the server bound to loopback and do not expose it through
a proxy or tunnel. Local processes can supply their own HTTP headers.
"""

ALLOWED_AUTHORITIES = frozenset({
    b"127.0.0.1:8765", b"localhost:8765", b"[::1]:8765",
})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
PROTECTED_HEADERS = frozenset({b"host", b"origin", b"sec-fetch-site"})
REJECTION_BODY = (
    b'{"detail":"Use the local application at http://127.0.0.1:8765 '
    b'with a matching Origin header."}'
)
SECURITY_HEADERS = (
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"frame-ancestors 'none'"),
    (b"x-content-type-options", b"nosniff"),
)


def local_request_allowed(scope):
    """Check raw headers without reading the body or accepting a WebSocket."""
    headers = {}
    for name, value in scope.get("headers", []):
        name = name.lower()
        if name not in PROTECTED_HEADERS:
            continue
        # Never let a proxy/framework's choice of first/last value decide trust.
        if name in headers:
            return False
        headers[name] = value

    authority = headers.get(b"host", b"").lower()
    if authority not in ALLOWED_AUTHORITIES:
        return False
    scheme = {"http": b"http", "https": b"https",
              "ws": b"http", "wss": b"https"}.get(scope.get("scheme"))
    if scheme is None:
        return False
    fetch_site = headers.get(b"sec-fetch-site")
    if fetch_site is not None and fetch_site not in {b"same-origin", b"none"}:
        return False
    origin = headers.get(b"origin")
    if origin is not None:
        # Browser Origin serialization has no path, trailing slash, or userinfo.
        return origin == scheme + b"://" + authority
    # Startup probes and ordinary page loads need no Origin. All writes and
    # WebSocket connections do: JSON and multipart/form posts get equal checks.
    return scope["type"] == "http" and scope.get("method") in SAFE_METHODS


class LocalAccessMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        if not local_request_allowed(scope):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await send({
                    "type": "http.response.start", "status": 403,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(REJECTION_BODY)).encode("ascii")),
                        (b"cache-control", b"no-store"),
                        *SECURITY_HEADERS,
                    ],
                })
                await send({"type": "http.response.body", "body": REJECTION_BODY})
            return

        async def protected_send(message):
            if message["type"] == "http.response.start":
                names = {name for name, _ in SECURITY_HEADERS}
                headers = [
                    (name, value) for name, value in message.get("headers", [])
                    if name.lower() not in names
                ]
                message = {**message, "headers": headers + list(SECURITY_HEADERS)}
            await send(message)

        await self.app(scope, receive, protected_send)
