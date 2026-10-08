#!/usr/bin/env python3
"""Entry point — The Telegram Documentaries, Phase 1 gateway.

Long-polling loop that replies to every message with a hardcoded "Hi Mate".
No LLM / Gemini calls yet (no API calls just yet).

Usage:
    python main.py
"""

from __future__ import annotations

import logging
import signal
import sys
import threading

from src.config import ConfigError, load_settings
from src.gateway import Gateway
from src.logging_utils import configure_logging
from src.telegram_client import TelegramClient


def main() -> int:
    # Configure logging BEFORE anything that could fail, so the first error is
    # already structured. Secrets are registered for redaction immediately too.
    configure_logging()
    logger = logging.getLogger("main")

    try:
        settings = load_settings()
    except ConfigError as exc:
        logger.critical("configuration error: %s", exc)
        return 1

    # Belt-and-braces: scrub known secrets from every log record.
    configure_logging(secrets=[settings.telegram_bot_token, settings.gemini_api_key or ""])

    stop_event = threading.Event()

    def _request_stop(signum, frame):  # noqa: ANN001, ANN002
        logger.info("event=signal_received signal=%s shutting_down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    client = TelegramClient(token=settings.telegram_bot_token)
    gateway = Gateway(client)
    try:
        gateway.run(stop_event)
    finally:
        client.close()

    logger.info("event=shutdown_complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())