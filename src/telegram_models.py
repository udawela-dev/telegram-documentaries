"""Typed boundary models for the Telegram Bot API (plan task 4).

Only the fields Phase 1 needs exist: ``update_id``, ``message``, ``chat.id``,
``text``. Everything else is tolerated (``extra="allow"``) so Telegram adding
fields never breaks parsing. External input is untrusted and arbitrarily
shaped: malformed items inside a ``result`` are skipped with a structured log;
whole-payload problems raise loudly.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger(__name__)


class TelegramAPIError(RuntimeError):
    """A Telegram API-level failure (HTTP error, ok=false, network failure)."""


class MalformedResponseError(TelegramAPIError):
    """A response body that is not even shaped like a Telegram API response."""


class Chat(BaseModel):
    # strict=True: untrusted input is never coerced (a string chat_id is malformed)
    model_config = ConfigDict(extra="allow", strict=True)

    id: int


class PhotoSize(BaseModel):
    """One Telegram photo size variant (uploads deliver several resolutions)."""

    model_config = ConfigDict(extra="allow", strict=True)

    file_id: str
    width: int
    height: int
    file_size: int | None = None


class Message(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    chat: Chat
    text: str | None = None
    photo: list[PhotoSize] | None = None


class TelegramFile(BaseModel):
    """Typed shape of a getFile result (the download path lives here)."""

    model_config = ConfigDict(extra="allow", strict=True)

    file_id: str
    file_path: str | None = None
    file_size: int | None = None


class Update(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    update_id: int
    message: Message | None = None


def parse_updates(payload: Any) -> list[Update]:
    """Boundary: untrusted getUpdates JSON → validated ``list[Update]``.

    * Whole-payload shape problems raise (``MalformedResponseError``).
    * ``ok: false`` raises ``TelegramAPIError`` with Telegram's description.
    * One bad update inside ``result`` never fails the batch — it is skipped
      with a WARNING log.
    """
    if not isinstance(payload, Mapping):
        raise MalformedResponseError(
            f"getUpdates payload must be a JSON object, got {type(payload).__name__}"
        )
    if "ok" not in payload:
        raise MalformedResponseError("getUpdates payload has no 'ok' field")
    if payload["ok"] is not True:
        description = payload.get("description")
        detail = str(description) if isinstance(description, str) else "Telegram API error"
        raise TelegramAPIError(f"getUpdates returned ok=false: {detail}")
    result = payload.get("result")
    if not isinstance(result, list):
        raise MalformedResponseError("getUpdates payload 'result' must be a list")

    updates: list[Update] = []
    for index, raw in enumerate(result):
        try:
            updates.append(Update.model_validate(raw))
        except ValidationError as exc:
            logger.warning(
                "event=skip_update index=%d reasons=%s",
                index,
                _describe_validation_error(exc),
            )
    return updates


def _describe_validation_error(exc: ValidationError) -> str:
    """Compact, bounded summary of the first few validation problems."""
    summaries = []
    for error in exc.errors()[:3]:
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        summaries.append(f"{location}: {error['msg']}")
    return "; ".join(summaries)


def parse_file(payload: Any) -> TelegramFile:
    """Boundary: untrusted getFile JSON → validated ``TelegramFile``.

    Whole-payload problems raise (``MalformedResponseError`` / ok=false →
    ``TelegramAPIError``); an invalid ``result`` is malformed input, not a
    silent skip — the caller must not proceed to a download without a valid
    file descriptor.
    """
    if not isinstance(payload, Mapping):
        raise MalformedResponseError(
            f"getFile payload must be a JSON object, got {type(payload).__name__}"
        )
    if "ok" not in payload:
        raise MalformedResponseError("getFile payload has no 'ok' field")
    if payload["ok"] is not True:
        description = payload.get("description")
        detail = str(description) if isinstance(description, str) else "Telegram API error"
        raise TelegramAPIError(f"getFile returned ok=false: {detail}")
    result = payload.get("result")
    try:
        return TelegramFile.model_validate(result)
    except (ValidationError, TypeError) as exc:
        raise MalformedResponseError(
            f"invalid getFile result: {_describe_validation_error(exc) if isinstance(exc, ValidationError) else exc}"
        ) from None