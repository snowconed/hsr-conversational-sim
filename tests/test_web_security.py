"""FastAPI security integration tests without ROS, a simulator, or API calls.

Run from the project directory:
    python -m unittest discover -s tests -p "test_web_security.py" -v
"""

import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from starlette.datastructures import UploadFile
from starlette.requests import Request

import realtime_transcription
import web_server


LOCAL_URL = "http://127.0.0.1:8765"
FOREIGN_URL = "http://example.invalid:8765"


class FakeController:
    def __init__(self):
        self.actions = []
        self.stops = 0
        self.closed = False

    def get_state(self):
        return {
            "ready": True, "busy": False, "current_action": None,
            "message": "待機中", "error": None, "holding": None,
            "object_positions": {}, "joints": {}, "history": [],
        }

    def submit(self, action):
        self.actions.append(action)
        return {"accepted": True, "action": action, "message": "受け付けました"}

    def stop(self):
        self.stops += 1
        return {"accepted": True, "action": "stop", "message": "停止しました"}

    def close(self):
        self.closed = True


class MemoryUsage:
    def __init__(self):
        self.records = []

    def record(self, endpoint, requested_model, response=None, **metadata):
        self.records.append({
            "endpoint": endpoint, "requested_model": requested_model,
            "usage": getattr(response, "usage", None), **metadata,
        })

    def record_realtime(self, model, event, elapsed_ms=None):
        self.records.append({
            "endpoint": "realtime/transcription", "requested_model": model,
            "usage": event.get("usage"), "elapsed_ms": elapsed_ms,
        })

    def snapshot(self):
        return list(self.records)


class FakeTranscriptionApi:
    def __init__(self):
        self.transcribe = AsyncMock(
            return_value=SimpleNamespace(text="箱を持ち上げて", usage=None)
        )
        self.audio = SimpleNamespace(
            transcriptions=SimpleNamespace(create=self.transcribe)
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class WebSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        fake_module = ModuleType("household_controller")
        fake_module.HouseholdController = FakeController
        patches = [
            patch.dict(sys.modules, {"household_controller": fake_module}),
            patch.object(web_server, "ApiUsage", MemoryUsage),
            patch.object(web_server, "PROGRAM_ROOT", Path(self.temporary.name)),
            patch.dict("os.environ", {"OPENAI_API_KEY": ""}),
        ]
        for current in patches:
            current.start()
            self.addCleanup(current.stop)
        self.no_api = self._forbid(web_server, "client")
        self.no_sdk = self._forbid(web_server, "warm_api_sdk")
        self.no_upstream = self._forbid(realtime_transcription, "connect_upstream")
        self.lifespan = web_server.app.router.lifespan_context(web_server.app)
        await self.lifespan.__aenter__()
        self.addAsyncCleanup(self.lifespan.__aexit__, None, None, None)
        self.controller = web_server.app.state.controller
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_server.app),
            base_url=LOCAL_URL,
        )
        self.addAsyncCleanup(self.http.aclose)

    def _forbid(self, module, name):
        current = patch.object(
            module, name,
            side_effect=AssertionError("External API calls are forbidden in tests"),
        )
        result = current.start()
        self.addCleanup(current.stop)
        return result

    def assert_security_headers(self, response):
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(
            response.headers["content-security-policy"], "frame-ancestors 'none'"
        )
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def assert_no_side_effects(self):
        self.no_api.assert_not_called()
        self.no_sdk.assert_not_called()
        self.no_upstream.assert_not_called()
        self.assertEqual(self.controller.actions, [])
        self.assertEqual(self.controller.stops, 0)
        self.assertEqual(web_server.app.state.command_epoch, 0)
        self.assertFalse(web_server.app.state.voice_active)
        self.assertEqual(web_server.app.state.usage.records, [])
        self.assertEqual(list(Path(self.temporary.name).rglob("*")), [])

    async def test_every_write_route_checks_origin_before_validation(self):
        writes = [
            (method, route.path.replace("{identity}", "0" * 32))
            for route in web_server.app.routes
            for method in getattr(route, "methods", set())
            if method not in {"GET", "HEAD", "OPTIONS"}
        ]
        self.assertGreaterEqual(len(writes), 9)
        for method, path in writes:
            for origin in (None, FOREIGN_URL):
                with self.subTest(method=method, path=path, origin=origin):
                    headers = {"content-type": "application/json"}
                    if origin is not None:
                        headers["origin"] = origin
                    response = await self.http.request(
                        method, path, headers=headers, content=b"{invalid-json"
                    )
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assert_security_headers(response)
            # The same invalid input reaches normal validation for the UI.
            with self.subTest(method=method, path=path, origin=LOCAL_URL):
                response = await self.http.request(
                    method, path,
                    headers={
                        "origin": LOCAL_URL,
                        "content-type": "application/json",
                    },
                    content=b"{invalid-json",
                )
                self.assertEqual(response.status_code, 422, response.text)
        self.assert_no_side_effects()

    async def test_untrusted_multipart_is_denied_before_receiving_body(self):
        for origin in (None, FOREIGN_URL):
            with self.subTest(origin=origin):
                headers = [
                    (b"host", b"127.0.0.1:8765"),
                    (b"content-type", b"multipart/form-data; boundary=test"),
                    (b"content-length", b"999999999"),
                ]
                if origin is not None:
                    headers.append((b"origin", origin.encode("ascii")))
                scope = {
                    "type": "http", "asgi": {"version": "3.0"},
                    "http_version": "1.1", "method": "POST", "scheme": "http",
                    "path": "/api/audio", "raw_path": b"/api/audio",
                    "root_path": "", "query_string": b"", "headers": headers,
                    "client": ("127.0.0.1", 12345),
                    "server": ("127.0.0.1", 8765),
                }
                receive = AsyncMock(
                    side_effect=AssertionError("Untrusted body must not be read")
                )
                send = AsyncMock()
                with patch.object(Request, "body", new_callable=AsyncMock) as body, \
                        patch.object(UploadFile, "read", new_callable=AsyncMock) as upload:
                    await web_server.app(scope, receive, send)
                    body.assert_not_awaited()
                    upload.assert_not_awaited()
                receive.assert_not_awaited()
                messages = [call.args[0] for call in send.await_args_list]
                self.assertEqual(messages[0]["type"], "http.response.start")
                self.assertEqual(messages[0]["status"], 403)
                self.assertEqual(messages[-1]["type"], "http.response.body")
        self.assert_no_side_effects()

    async def test_page_static_and_state_need_no_origin(self):
        for path in ("/", "/static/base.css", "/static/app.css", "/api/state"):
            with self.subTest(path=path):
                response = await self.http.get(path)
                self.assertEqual(response.status_code, 200, response.text)
                self.assert_security_headers(response)
        self.assertTrue(response.json()["ready"])
        self.assertFalse(response.json()["api"]["configured"])
        self.assert_no_side_effects()

    async def test_same_origin_json_and_manual_commands_work(self):
        headers = {"origin": LOCAL_URL, "sec-fetch-site": "same-origin"}
        action = await self.http.post(
            "/api/action", headers=headers, json={"action": "nod"}
        )
        text = await self.http.post(
            "/api/text", headers=headers, json={"text": "青いボールを持ち上げて"}
        )
        for response in (action, text):
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["accepted"])
            self.assert_security_headers(response)
        self.assertEqual(self.controller.actions, ["nod", "pick_ball"])
        self.no_api.assert_not_called()
        self.no_sdk.assert_not_called()
        self.no_upstream.assert_not_called()

    async def test_same_origin_multipart_transcribes_using_fake_api(self):
        fake = FakeTranscriptionApi()
        with patch.object(web_server, "client", return_value=fake) as factory:
            response = await self.http.post(
                "/api/audio",
                headers={"origin": LOCAL_URL, "sec-fetch-site": "same-origin"},
                files={"file": ("recording.webm", b"fake audio", "audio/webm")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["accepted"])
        self.assertEqual(response.json()["text"], "箱を持ち上げて")
        self.assertEqual(self.controller.actions, ["pick_cube"])
        factory.assert_called_once_with()
        fake.transcribe.assert_awaited_once()
        self.assertEqual(
            fake.transcribe.await_args.kwargs["file"],
            ("command.webm", b"fake audio", "audio/webm"),
        )
        self.assertEqual(
            web_server.app.state.usage.records[0]["endpoint"],
            "audio/transcriptions",
        )
        self.assert_security_headers(response)
        self.no_sdk.assert_not_called()
        self.no_upstream.assert_not_called()

    async def test_foreign_host_is_denied_even_with_matching_origin(self):
        for path in ("/", "/static/app.css", "/api/state"):
            for origin in (None, FOREIGN_URL):
                with self.subTest(path=path, origin=origin):
                    headers = {"host": "example.invalid:8765"}
                    if origin is not None:
                        headers["origin"] = origin
                    response = await self.http.get(path, headers=headers)
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assert_security_headers(response)
        response = await self.http.post(
            "/api/action",
            headers={"host": "example.invalid:8765", "origin": FOREIGN_URL},
            json={"action": "nod"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assert_no_side_effects()

    async def _websocket(self, host, origin):
        headers = [(b"host", host.encode("ascii"))]
        if origin is not None:
            headers.append((b"origin", origin.encode("ascii")))
        scope = {
            "type": "websocket", "asgi": {"version": "3.0"},
            "http_version": "1.1", "scheme": "ws",
            "path": "/api/programs/transcription",
            "raw_path": b"/api/programs/transcription",
            "root_path": "", "query_string": b"", "headers": headers,
            "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8765),
            "subprotocols": [],
        }
        receive = AsyncMock(side_effect=[{"type": "websocket.connect"}])
        send = AsyncMock()
        await web_server.app(scope, receive, send)
        return receive, [call.args[0] for call in send.await_args_list]

    async def test_websocket_is_denied_before_accept_or_upstream(self):
        for host, origin in (
            ("127.0.0.1:8765", None),
            ("127.0.0.1:8765", FOREIGN_URL),
            ("example.invalid:8765", FOREIGN_URL),
        ):
            with self.subTest(host=host, origin=origin):
                receive, messages = await self._websocket(host, origin)
                receive.assert_not_awaited()
                self.assertEqual(
                    messages, [{"type": "websocket.close", "code": 1008}]
                )
        self.assert_no_side_effects()

    async def test_valid_websocket_without_key_returns_public_error(self):
        receive, messages = await self._websocket("127.0.0.1:8765", LOCAL_URL)
        receive.assert_awaited_once()
        self.assertEqual(messages[0]["type"], "websocket.accept")
        sent = [
            json.loads(message["text"])
            for message in messages if message["type"] == "websocket.send"
        ]
        self.assertEqual(sent, [{
            "type": "error",
            "message": "OPENAI_API_KEY を設定してアプリを起動し直してください。",
        }])
        self.assertEqual(messages[-1]["type"], "websocket.close")
        self.assert_no_side_effects()


if __name__ == "__main__":
    unittest.main()
