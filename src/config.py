"""Configuration: load and validate secrets from .env.

Typed boundary for configuration (SPECS/TECH.md). The token must never be
printed, logged, or serialized.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Raised when the environment cannot provide a valid configuration."""


class Settings(BaseModel):
    telegram_bot_token: str
    gemini_api_key: str | None = None

    def __str__(self) -> str:
        return self._masked_repr()

    def __repr__(self) -> str:
        return self._masked_repr()

    def _masked_repr(self) -> str:
        key = "***" if self.gemini_api_key else "None"
        return f"Settings(telegram_bot_token='***', gemini_api_key={key})"


def load_settings(env_file: str = ".env") -> Settings:
    """Load .env (if present) and build validated Settings.

    Fails loudly with a clear message when TELEGRAM_BOT_TOKEN is missing —
    config errors should never be silently papered over.
    """
    load_dotenv(env_file)

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN is missing. Copy .env.example to .env and set it."
        )

    gemini_key = (os.getenv("GEMINI_API_KEY") or "").strip() or None

    settings = Settings(telegram_bot_token=token, gemini_api_key=gemini_key)
    logger.debug("settings loaded (token never logged)")
    return settings