"""Offline request-boundary tests; no ROS, browser, or external API required."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_access import LocalAccessMiddleware


DEFAULT_HOST = b"127.0.0.1:8765"
DEFAULT_ORIGIN = b"http://127.0.0.1:8765"
LOOPBACK_HOSTS = (DEFAULT_HOST, b"localhost:8765", b"[::1]:8765")


def http_scope(method="POST", headers=None, scheme="http", path="/api/audio"):
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": scheme,
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": (
            [(b"host", DEFAULT_HOST), (b"origin", DEFAULT_ORIGIN)]
            if headers is None else headers
        ),
        "server": ("127.0.0.1", 8765),
        "client": ("127.0.0.1", 52000),
    }


def websocket_scope(headers=None, scheme="ws"):
    scope = http_scope(headers=headers, scheme=scheme,
                       path="/api/programs/transcription")
    scope["type"] = "websocket"
    scope.pop("method")
    scope["subprotocols"] = []
    return scope


class LocalAccessTests(unittest.IsolatedAsyncioTestCase):
    async def run_request(self, scope, *, response_headers=None):
        """Record whether rejected traffic reaches the body or application."""
        calls = {"app": 0, "receive": 0}
        messages = []

        async def receive():
            calls["receive"] += 1
            if scope["type"] == "websocket":
                return {"type": "websocket.connect"}
            return {"type": "http.request", "body": b"audio bytes"}

        async def app(received_scope, received_receive, send):
            calls["app"] += 1
            self.assertIs(received_scope, scope)
            await received_receive()
            if scope["type"] == "websocket":
                await send({"type": "websocket.accept"})
                await send({"type": "websocket.close", "code": 1000})
            else:
                await send({
                    "type": "http.response.start", "status": 200,
                    "headers": response_headers or [],
                })
                await send({"type": "http.response.body", "body": b"ok"})

        async def send(message):
            messages.append(message)

        await LocalAccessMiddleware(app)(scope, receive, send)
        return calls, messages

    async def assert_http_rejected(self, scope):
        calls, messages = await self.run_request(scope)
        self.assertEqual(calls, {"app": 0, "receive": 0})
        self.assertEqual(
            [item["type"] for item in messages],
            ["http.response.start", "http.response.body"],
        )
        self.assertEqual(messages[0]["status"], 403)
        headers = dict(messages[0]["headers"])
        self.assertEqual(headers[b"cache-control"], b"no-store")
        self.assertEqual(headers[b"content-type"], b"application/json")
        self.assertEqual(int(headers[b"content-length"]), len(messages[1]["body"]))
        self.assert_protected_response(messages[0])

    def assert_protected_response(self, start):
        headers = start["headers"]
        for name, expected in (
            (b"x-frame-options", b"DENY"),
            (b"content-security-policy", b"frame-ancestors 'none'"),
            (b"x-content-type-options", b"nosniff"),
        ):
            self.assertEqual(
                [value for key, value in headers if key.lower() == name],
                [expected],
            )

    async def test_same_origin_posts_allow_all_loopback_names_and_body_formats(self):
        for host in LOOPBACK_HOSTS:
            for scheme in ("http", "https"):
                for content_type in (
                    b"application/json",
                    b"multipart/form-data; boundary=sample",
                    b"application/x-www-form-urlencoded",
                    b"text/plain",
                ):
                    with self.subTest(host=host, scheme=scheme,
                                      content_type=content_type):
                        calls, messages = await self.run_request(http_scope(
                            scheme=scheme,
                            headers=[
                                (b"host", host),
                                (b"origin", scheme.encode() + b"://" + host),
                                (b"content-type", content_type),
                                (b"sec-fetch-site", b"same-origin"),
                            ],
                        ))
                        self.assertEqual(calls, {"app": 1, "receive": 1})
                        self.assertEqual(messages[0]["status"], 200)
                        self.assert_protected_response(messages[0])

    async def test_health_checks_and_page_loads_need_no_origin(self):
        for host in LOOPBACK_HOSTS:
            for method in ("GET", "HEAD", "OPTIONS"):
                for path in ("/", "/api/state", "/static/chat_app.js"):
                    with self.subTest(host=host, method=method, path=path):
                        calls, messages = await self.run_request(http_scope(
                            method=method, path=path,
                            headers=[(b"host", host)],
                        ))
                        self.assertEqual(calls, {"app": 1, "receive": 1})
                        self.assertEqual(messages[0]["status"], 200)

    async def test_unsafe_methods_require_origin_before_reading_body(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "TRACE", "CONNECT"):
            with self.subTest(method=method):
                await self.assert_http_rejected(http_scope(
                    method=method,
                    headers=[
                        (b"host", DEFAULT_HOST),
                        (b"content-type", b"multipart/form-data; boundary=sample"),
                        (b"content-length", b"999999999"),
                        (b"sec-fetch-site", b"same-origin"),
                    ],
                ))

    async def test_host_validation_blocks_rebinding_and_malformed_authorities(self):
        for host in (
            b"evil.example:8765",
            b"127.0.0.1.evil.example:8765",
            b"localhost.evil.example:8765",
            b"localhost.:8765",
            b"0.0.0.0:8765",
            b"192.168.1.1:8765",
            b"127.1:8765",
            b"2130706433:8765",
            b"127.0.0.1",
            b"127.0.0.1:80",
            b"127.0.0.1:08765",
            b"::1:8765",
            b"user@127.0.0.1:8765",
            b"http://127.0.0.1:8765",
            b"127.0.0.1:8765/",
            b" 127.0.0.1:8765",
            b"127.0.0.1:8765 ",
            b"127.0.0.1:\t8765",
            b"127.0.0.1:8765\r\n",
            b"127.0.0.1:8765\x00",
            b"127.0.0.1:8765,evil.example:8765",
            b"",
        ):
            with self.subTest(host=host):
                # Even an Origin that matches an attacker Host must fail.
                await self.assert_http_rejected(http_scope(headers=[
                    (b"host", host), (b"origin", b"http://" + host),
                ]))
        with self.subTest(host="missing"):
            await self.assert_http_rejected(http_scope(
                headers=[(b"origin", DEFAULT_ORIGIN)],
            ))

    async def test_origin_validation_blocks_foreign_opaque_and_malformed_values(self):
        for origin in (
            b"https://evil.example",
            b"http://localhost:8765",
            b"http://127.0.0.1:9999",
            b"https://127.0.0.1:8765",
            b"http://127.0.0.1",
            b"http://127.0.0.1:08765",
            b"http://127.0.0.1.evil.example:8765",
            b"http://127.0.0.1:8765@evil.example",
            b"http://user:password@127.0.0.1:8765",
            b"http://127.0.0.1:8765/",
            b"http://127.0.0.1:8765/path",
            b"http://127.0.0.1:8765?query",
            b"http://127.0.0.1:8765#fragment",
            b" http://127.0.0.1:8765",
            b"http://127.0.0.1:8765 ",
            b"http://127.0.0.1:8765\r\n",
            b"http://127.0.0.1:8765\x00",
            b"http://127.0.0.1:8765 https://evil.example",
            b"http://127.0.0.1:8765,https://evil.example",
            b"null",
            b"",
        ):
            for method in ("POST", "GET"):
                with self.subTest(origin=origin, method=method):
                    await self.assert_http_rejected(http_scope(
                        method=method,
                        headers=[(b"host", DEFAULT_HOST), (b"origin", origin)],
                    ))

    async def test_duplicate_protected_headers_fail_even_if_both_values_are_valid(self):
        for name, value in (
            (b"host", DEFAULT_HOST),
            (b"origin", DEFAULT_ORIGIN),
            (b"sec-fetch-site", b"same-origin"),
        ):
            with self.subTest(name=name):
                headers = [
                    (b"host", DEFAULT_HOST),
                    (b"origin", DEFAULT_ORIGIN),
                    (b"sec-fetch-site", b"same-origin"),
                    (name.upper(), value),
                ]
                await self.assert_http_rejected(http_scope(headers=headers))
                calls, messages = await self.run_request(websocket_scope(headers))
                self.assertEqual(calls, {"app": 0, "receive": 0})
                self.assertEqual(messages, [
                    {"type": "websocket.close", "code": 1008},
                ])

    async def test_fetch_metadata_rejects_cross_site_same_site_and_invalid_values(self):
        for fetch_site in (
            b"cross-site", b"same-site", b"", b"invalid",
            b"same-origin,cross-site", b"same-origin ", b"same-origin\r\n",
        ):
            for method in ("GET", "POST"):
                with self.subTest(fetch_site=fetch_site, method=method):
                    await self.assert_http_rejected(http_scope(
                        method=method,
                        headers=[
                            (b"host", DEFAULT_HOST),
                            (b"origin", DEFAULT_ORIGIN),
                            (b"sec-fetch-site", fetch_site),
                        ],
                    ))

    async def test_valid_navigation_metadata_and_case_insensitive_header_names(self):
        for fetch_site in (b"same-origin", b"none"):
            with self.subTest(fetch_site=fetch_site):
                calls, messages = await self.run_request(http_scope(headers=[
                    (b"Host", b"LOCALHOST:8765"),
                    (b"Origin", b"http://localhost:8765"),
                    (b"Sec-Fetch-Site", fetch_site),
                    (b"x-unrelated", b"one"),
                    (b"x-unrelated", b"two"),
                ]))
                self.assertEqual(calls, {"app": 1, "receive": 1})
                self.assertEqual(messages[0]["status"], 200)

    async def test_invalid_or_missing_transport_scheme_is_rejected(self):
        for scheme in ("ftp", "", None):
            with self.subTest(scheme=scheme):
                await self.assert_http_rejected(http_scope(scheme=scheme))

    async def test_websocket_accepts_matching_http_and_https_browser_origins(self):
        for host in LOOPBACK_HOSTS:
            for scheme, origin_scheme in (("ws", b"http"), ("wss", b"https")):
                with self.subTest(host=host, scheme=scheme):
                    calls, messages = await self.run_request(websocket_scope(
                        scheme=scheme, headers=[
                            (b"host", host),
                            (b"origin", origin_scheme + b"://" + host),
                        ],
                    ))
                    self.assertEqual(calls, {"app": 1, "receive": 1})
                    self.assertEqual(messages, [
                        {"type": "websocket.accept"},
                        {"type": "websocket.close", "code": 1000},
                    ])

    async def test_websocket_rejects_before_accepting_or_receiving(self):
        rejected_headers = (
            [(b"host", DEFAULT_HOST)],
            [(b"host", DEFAULT_HOST), (b"origin", b"null")],
            [(b"host", DEFAULT_HOST), (b"origin", b"https://evil.example")],
            [(b"host", DEFAULT_HOST), (b"origin", b"https://127.0.0.1:8765")],
            [(b"host", DEFAULT_HOST), (b"origin", b"ws://127.0.0.1:8765")],
            [(b"host", DEFAULT_HOST), (b"origin", b"http://127.0.0.1:8765/")],
            [(b"host", b"evil.example:8765"), (b"origin", b"http://evil.example:8765")],
            [
                (b"host", DEFAULT_HOST), (b"origin", DEFAULT_ORIGIN),
                (b"sec-fetch-site", b"cross-site"),
            ],
        )
        for headers in rejected_headers:
            with self.subTest(headers=headers):
                calls, messages = await self.run_request(websocket_scope(headers))
                self.assertEqual(calls, {"app": 0, "receive": 0})
                self.assertEqual(messages, [
                    {"type": "websocket.close", "code": 1008},
                ])

    async def test_response_protection_replaces_conflicts_and_preserves_other_headers(self):
        original = [
            (b"X-Frame-Options", b"ALLOWALL"),
            (b"x-frame-options", b"SAMEORIGIN"),
            (b"Content-Security-Policy", b"frame-ancestors *"),
            (b"X-Content-Type-Options", b"invalid"),
            (b"content-type", b"text/html"),
            (b"cache-control", b"no-cache"),
            (b"set-cookie", b"one=1"),
            (b"set-cookie", b"two=2"),
        ]
        before = list(original)
        _, messages = await self.run_request(
            http_scope(method="GET"), response_headers=original,
        )
        self.assert_protected_response(messages[0])
        self.assertEqual(original, before)
        for header in original[4:]:
            self.assertIn(header, messages[0]["headers"])
        self.assertEqual(messages[1], {"type": "http.response.body", "body": b"ok"})

    async def test_lifespan_is_passed_through_without_http_checks(self):
        scope = {"type": "lifespan", "asgi": {"version": "3.0"}}
        startup = {"type": "lifespan.startup"}
        complete = {"type": "lifespan.startup.complete"}
        received = []
        sent = []

        async def receive():
            return startup

        async def send(message):
            sent.append(message)

        async def app(actual_scope, actual_receive, actual_send):
            self.assertIs(actual_scope, scope)
            self.assertIs(actual_receive, receive)
            self.assertIs(actual_send, send)
            received.append(await actual_receive())
            await actual_send(complete)

        await LocalAccessMiddleware(app)(scope, receive, send)
        self.assertEqual(received, [startup])
        self.assertEqual(sent, [complete])


if __name__ == "__main__":
    unittest.main()
