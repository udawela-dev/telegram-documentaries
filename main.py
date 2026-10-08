#!/usr/bin/env python3
"""Entry point — The Telegram Documentaries, Phase 1 gateway + Phase 2 gate.

Long-polling loop that replies to every text message with the confirmation
text. Photo uploads pass through **The Bouncer** (an ADK LlmAgent on Gemini
3.1 Flash Lite): approved photos continue the confirmation flow; non-human
photos get a cheeky rejection and reset the chat's ephemeral state.

Usage:
    python main.py
"""

from __future__ import annotations

import logging
import signal
import sys
import threading

from src.bouncer import BOUNCER_MODEL, Bouncer
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

    # The Bouncer gates photo uploads. It needs a Gemini key — never hardcoded,
    # always from settings/.env (constitution non-negotiable). Without one the
    # bot still runs its text flow, but photo uploads get a graceful "can't
    # look" reply instead of a verdict.
    bouncer = None
    if settings.gemini_api_key:
        bouncer = Bouncer(api_key=settings.gemini_api_key, model=BOUNCER_MODEL)
    else:
        logger.warning("event=bouncer_unavailable reason=missing_gemini_api_key")

    client = TelegramClient(token=settings.telegram_bot_token)
    gateway = Gateway(client, bouncer=bouncer)
    try:
        gateway.run(stop_event)
    finally:
        client.close()

    logger.info("event=shutdown_complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())