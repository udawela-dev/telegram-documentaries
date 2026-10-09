"""Unit tests for ``main.py`` startup wiring (Phase 6 — NB1).

The Narrator has **no** local/key-free TTS fallback, so a keyless deployment can
never narrate. Announcing ``event=narrator_ready`` there is misleading; readiness
must only be logged when a Gemini key is actually present. These tests run the
real ``main()`` with only the network edges (Telegram client + gateway loop)
doubled, and assert on the structured log records emitted.
"""
import logging

import main as main_module
from src.config import Settings


class _StubClient:
    """Stands in for the real ``TelegramClient`` — no network."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def close(self) -> None:
        pass


class _StubGateway:
    """Stands in for ``Gateway`` so ``main()`` returns instead of long-polling."""

    def __init__(self, *args, **kwargs) -> None:
        self.kwargs = kwargs

    def run(self, stop_event) -> None:
        return None


def _run_main(monkeypatch, settings: Settings) -> int:
    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    monkeypatch.setattr(main_module, "TelegramClient", _StubClient)
    monkeypatch.setattr(main_module, "Gateway", _StubGateway)
    # Never clobber pytest's SIGINT handler.
    monkeypatch.setattr(main_module.signal, "signal", lambda *args, **kwargs: None)
    return main_module.main()


def test_keyless_main_does_not_announce_narrator_ready(monkeypatch, caplog):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    settings = Settings(telegram_bot_token="TEST-TOKEN", gemini_api_key=None)

    with caplog.at_level(logging.INFO):
        rc = _run_main(monkeypatch, settings)

    assert rc == 0
    messages = [record.message for record in caplog.records]
    # "ready" would be a lie with no key: there is no local TTS fallback.
    assert not any("event=narrator_ready" in message for message in messages)
    assert any(
        "event=narrator_unavailable" in message
        and "missing_gemini_api_key" in message
        for message in messages
    )


def test_keyed_main_announces_narrator_ready(monkeypatch, caplog):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    settings = Settings(telegram_bot_token="TEST-TOKEN", gemini_api_key="test-key")

    with caplog.at_level(logging.INFO):
        rc = _run_main(monkeypatch, settings)

    assert rc == 0
    messages = [record.message for record in caplog.records]
    assert any(
        "event=narrator_ready model=" in message
        and "voice=" in message
        for message in messages
    )
    assert not any("event=narrator_unavailable" in message for message in messages)
