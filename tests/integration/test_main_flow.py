"""Integration test: the Phase 1 main flow over real HTTP (plan tasks 5-8).

A local HTTP stub stands in for ``api.telegram.org`` on loopback. Everything
else is real: httpx transport + sockets, URL building, boundary parsing,
dispatch, and offset accumulation. (The guarded end-to-end check against the
live Telegram API is the manual smoke test in README/validation.md.)
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from src.gateway import Gateway
from src.telegram_client import TelegramClient

IT_TOKEN = "IT-TOKEN"


class StubState:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.batches: list[list[dict]] = []


def _make_handler(state: StubState):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            state.requests.append(
                {"method": "GET", "path": parsed.path, "params": parse_qs(parsed.query)}
            )
            batch = state.batches.pop(0) if state.batches else []
            self._send_json({"ok": True, "result": batch})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            state.requests.append({"method": "POST", "path": urlparse(self.path).path, "body": body})
            self._send_json({"ok": True, "result": {"message_id": 1}})

        def _send_json(self, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:  # keep test output quiet
            pass

    return Handler


@pytest.fixture
def stub_server():
    state = StubState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, state
    finally:
        server.shutdown()
        server.server_close()


def test_main_flow_poll_parse_dispatch_reply_and_offset_over_real_http(stub_server):
    server, state = stub_server
    state.batches = [
        [{"update_id": 9001, "message": {"message_id": 1, "chat": {"id": 4242}, "text": "hello"}}]
    ]

    client = TelegramClient(IT_TOKEN, base_url=f"http://127.0.0.1:{server.server_port}", timeout=5.0)
    gateway = Gateway(client, poll_timeout=1)
    try:
        assert gateway.poll_once() == 1

        gets = [r for r in state.requests if r["method"] == "GET"]
        posts = [r for r in state.requests if r["method"] == "POST"]
        assert len(gets) == 1
        assert gets[0]["path"] == f"/bot{IT_TOKEN}/getUpdates"
        assert "offset" not in gets[0]["params"], "first poll must not pass an offset"
        assert posts[0]["body"] == {"chat_id": 4242, "text": "Hi Mate"}

        # Second poll carries offset=last+1 so the update is never reprocessed.
        assert gateway.poll_once() == 0
        assert state.requests[-1]["params"].get("offset") == ["9002"]
    finally:
        client.close()


def test_main_flow_send_failure_does_not_break_next_poll_over_real_http(stub_server):
    server, state = stub_server
    state.batches = [
        [{"update_id": 1, "message": {"message_id": 1, "chat": {"id": 4242}, "text": "x"}}],
        [{"update_id": 2, "message": {"message_id": 2, "chat": {"id": 4242}, "text": "y"}}],
    ]

    class FlakyHandler:
        def __init__(self, inner):
            self._inner = inner

        def send_message(self, chat_id: int, text: str) -> None:
            raise RuntimeError("injected send failure")

        def get_updates(self, offset=None, timeout=30, limit=None):
            return self._inner.get_updates(offset=offset, timeout=timeout, limit=limit)

    client = TelegramClient(IT_TOKEN, base_url=f"http://127.0.0.1:{server.server_port}", timeout=5.0)
    gateway = Gateway(FlakyHandler(client), poll_timeout=1)
    try:
        assert gateway.poll_once() == 0  # send failed, but the poll cycle completes
        assert gateway.offset == 2
        assert gateway.poll_once() == 0  # second send also fails, loop still alive
        assert gateway.offset == 3
    finally:
        client.close()