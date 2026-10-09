#!/usr/bin/env python3
"""Entry point — The Telegram Documentaries, Phases 1–4.

Long-polling loop that replies to every text message with the confirmation
text while idle. Photo uploads pass through **The Bouncer** (an ADK LlmAgent on
Gemini 3.1 Flash Lite): approved photos continue into **The Interviewer**, a
stateful seven-question documentary interview whose answers build a behavioural
profile plus a suggested animal; non-human photos get a cheeky rejection and
reset the chat's ephemeral state.

On completion **The Converter** (Phase 4) fires: an ADK ``LlmAgent`` on
Gemini 3.1 Flash Image builds a hybrid animal portrait from the retained
photo + profile and sends it straight to the chat. While the Gemini key is
blocked, a deterministic OpenCV photo-booth composer produces the image
instead; the local composer is wired as the resilience fallback exactly like
the Bouncer's local face detector.

When Gemini cannot be reached (missing/blocked API key, network failure,
timeout) the Bouncer falls back to a key-free OpenCV face detector, so photo
uploads still get a real human/non-human verdict. The Interviewer's questions
and profile builder are deterministic and local, so the interview works offline
regardless of the Gemini key — the ADK agent is wired as the backbone for later
stages.

Usage:
    python main.py
"""

from __future__ import annotations

import logging
import signal
import sys
import threading

from src.bouncer import Bouncer
from src.config import ConfigError, load_settings
from src.converter import Converter
from src.gateway import Gateway
from src.interview_state import InterviewStateStore
from src.interviewer import Interviewer
from src.local_vision import LocalVisionClassifier
from src.logging_utils import configure_logging
from src.portrait_store import PortraitStore
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

    # The Bouncer gates photo uploads. Never hardcoded — keys always come from
    # settings/.env (constitution non-negotiable). The key-free OpenCV face
    # detector is always wired in as the resilience fallback: if Gemini is
    # missing or unreachable (blocked key, network, timeout), photo uploads
    # still get a real human/non-human verdict instead of a dead end.
    local_vision = LocalVisionClassifier()  # logs event=local_vision_ready/unavailable
    bouncer = None
    if settings.gemini_api_key or local_vision.available:
        # Model is resolved at Bouncer construction (honours a runtime
        # BOUNCER_MODEL env override).
        bouncer = Bouncer(
            api_key=settings.gemini_api_key,
            local_classifier=local_vision,
        )
        if not settings.gemini_api_key:
            logger.warning("event=bouncer_local_only reason=missing_gemini_api_key")
    else:
        logger.warning("event=bouncer_unavailable reason=no_gemini_key_and_no_local_vision")

    # The Interviewer owns the per-chat interview state machine. Its questions
    # and profile/animal builder are deterministic and local (Gemini-key-free),
    # so the interview works in Telegram today; the ADK agent is wired as the
    # backbone for later stages. One shared state store is passed in and used
    # by the gateway for all reads/writes (TECH.md: one shared state driver).
    interview_store = InterviewStateStore()
    interviewer = Interviewer(api_key=settings.gemini_api_key, store=interview_store)

    # The Converter (Phase 4) owns the hybrid portrait: an ADK image agent on
    # gemini-3.1-flash-image, plus the key-free local photo-booth fallback while
    # the Gemini key is blocked. The portrait store keeps the approved photo's
    # raw bytes per chat_id until the interview completes (or a reset purges it).
    portraits = PortraitStore()
    local_composer = None
    try:
        # Imported lazily so a missing cv2 degrades to "no local fallback"
        # instead of stopping the whole bot at import time.
        from src.local_composite import LocalHybridComposer

        local_composer = LocalHybridComposer()
    except Exception:
        logger.exception("event=converter_local_composer_unavailable")

    converter = None
    if settings.gemini_api_key or local_composer is not None:
        converter = Converter(
            api_key=settings.gemini_api_key,
            local_composer=local_composer,
        )
        logger.info(
            "event=converter_ready model=%s local_fallback=%s",
            converter.model,
            local_composer is not None,
        )
        if not settings.gemini_api_key:
            logger.warning("event=converter_local_only reason=missing_gemini_api_key")
    else:
        logger.warning(
            "event=converter_unavailable reason=no_gemini_key_and_no_local_composer"
        )

    client = TelegramClient(token=settings.telegram_bot_token)
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=converter,
        portraits=portraits,
    )
    try:
        gateway.run(stop_event)
    finally:
        client.close()

    logger.info("event=shutdown_complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
