"""Unit tests for the raw-HTTP Telegram client (plan task 5 — RED first).

Uses httpx ``MockTransport``: real client code, no network. Covers the
long-polling contract (offset/timeout/limit params), typed output, and the
resilience rules — every failure raises a redacted ``TelegramAPIError`` that
never contains the bot token.
"""
import json
import logging

import httpx
import pytest

from src.telegram_client import TelegramClient
from src.telegram_models import MalformedResponseError, TelegramAPIError, Update

TOKEN = "TEST-TOKEN-123"


def _client(handler: "httpx.Request | httpx.Response") -> TelegramClient:
    return TelegramClient(
        TOKEN,
        transport=httpx.MockTransport(handler),
        timeout=5.0,
    )


# --- getUpdates ----------------------------------------------------------------


def test_get_updates_requests_offset_timeout_and_returns_typed_updates():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {"update_id": 42, "message": {"message_id": 1, "chat": {"id": 7}, "text": "hi"}}
                ],
            },
        )

    updates = _client(handler).get_updates(offset=42, timeout=30)

    assert captured["path"] == f"/bot{TOKEN}/getUpdates"
    assert captured["params"] == {"offset": "42", "timeout": "30"}
    assert isinstance(updates[0], Update)
    assert updates[0].update_id == 42
    assert updates[0].message is not None
    assert updates[0].message.chat.id == 7
    assert updates[0].message.text == "hi"


def test_get_updates_omits_offset_and_limit_when_none():
    captured = {}

    def handler(request):
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True, "result": []})

    updates = _client(handler).get_updates(timeout=10)

    assert "offset" not in captured["params"]
    assert "limit" not in captured["params"]
    assert captured["params"] == {"timeout": "10"}
    assert updates == []


def test_get_updates_includes_limit_when_given():
    captured = {}

    def handler(request):
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True, "result": []})

    _client(handler).get_updates(offset=1, timeout=30, limit=5)

    assert captured["params"] == {"offset": "1", "timeout": "30", "limit": "5"}


def test_get_updates_http_error_raises_redacted_telegram_api_error(caplog):
    def handler(request):
        return httpx.Response(500, text="internal server error")

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(TelegramAPIError) as excinfo:
            _client(handler).get_updates(timeout=10)

    assert TOKEN not in str(excinfo.value)
    assert TOKEN not in caplog.text


def test_get_updates_http_error_includes_telegram_description():
    def handler(request):
        return httpx.Response(
            400,
            json={"ok": False, "error_code": 400, "description": "Bad Request: bad offset"},
        )

    with pytest.raises(TelegramAPIError) as excinfo:
        _client(handler).get_updates(timeout=10)

    assert "bad offset" in str(excinfo.value)
    assert TOKEN not in str(excinfo.value)


def test_get_updates_non_json_body_raises_redacted():
    def handler(request):
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(TelegramAPIError) as excinfo:
        _client(handler).get_updates(timeout=10)

    assert TOKEN not in str(excinfo.value)


def test_get_updates_malformed_shape_raises_malformed_response_error():
    def handler(request):
        return httpx.Response(200, json={"ok": True, "result": "not-a-list"})

    with pytest.raises(MalformedResponseError):
        _client(handler).get_updates(timeout=10)


def test_get_updates_ok_false_raises_telegram_api_error():
    def handler(request):
        return httpx.Response(200, json={"ok": False, "description": "Conflict: terminated"})

    with pytest.raises(TelegramAPIError) as excinfo:
        _client(handler).get_updates(timeout=10)

    assert "Conflict" in str(excinfo.value)


def test_get_updates_network_error_raises_redacted_telegram_api_error(caplog):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(TelegramAPIError) as excinfo:
            _client(handler).get_updates(timeout=10)

    assert "connection refused" in str(excinfo.value).lower()
    assert TOKEN not in str(excinfo.value)
    assert TOKEN not in caplog.text


# --- sendMessage ---------------------------------------------------------------


def test_send_message_posts_chat_id_and_hardcoded_text():
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client = _client(handler)
    client.send_message(4242, "Hi Mate")

    assert captured["method"] == "POST"
    assert captured["path"] == f"/bot{TOKEN}/sendMessage"
    assert captured["body"] == {"chat_id": 4242, "text": "Hi Mate"}


def test_send_message_ok_false_raises_with_description():
    def handler(request):
        return httpx.Response(200, json={"ok": False, "description": "Bad Request: chat not found"})

    with pytest.raises(TelegramAPIError) as excinfo:
        _client(handler).send_message(4242, "Hi Mate")

    assert "chat not found" in str(excinfo.value)
    assert TOKEN not in str(excinfo.value)


def test_send_message_http_error_raises_redacted():
    def handler(request):
        return httpx.Response(429, text="Too Many Requests")

    with pytest.raises(TelegramAPIError) as excinfo:
        _client(handler).send_message(4242, "Hi Mate")

    assert TOKEN not in str(excinfo.value)


def test_send_message_malformed_response_raises_redacted():
    def handler(request):
        return httpx.Response(200, json={"ok": "not-a-bool"})

    with pytest.raises(TelegramAPIError) as excinfo:
        _client(handler).send_message(4242, "Hi Mate")

    assert TOKEN not in str(excinfo.value)


def test_send_message_network_error_raises_redacted(caplog):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(TelegramAPIError) as excinfo:
            _client(handler).send_message(4242, "Hi Mate")

    assert TOKEN not in str(excinfo.value)
    assert TOKEN not in caplog.text


# --- construction --------------------------------------------------------------


def test_client_rejects_blank_token():
    with pytest.raises(ValueError):
        TelegramClient("   ", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))