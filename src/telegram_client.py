"""Raw-HTTP Telegram Bot API client (plan task 5).

Chosen transport: **httpx** (sync) — first-class timeouts, injectable
``MockTransport`` for tests, and room to grow. Raw HTTP only, no bot
frameworks (requirements.md locked decision #1).

Security: the token lives in the base URL, so httpx's own error strings
would leak it. Every failure is converted — with the token redacted — into a
``TelegramAPIError`` *before* it can reach logs, so the token (which is also
scrubbed by the app's log filter) never appears in a log record.
"""
from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from src.logging_utils import log_call, redact
from src.telegram_models import TelegramAPIError, Update, parse_updates

API_BASE_URL = "https://api.telegram.org"


class SendMessageResponse(BaseModel):
    """Minimal validated shape of a sendMessage response (boundary check)."""

    model_config = ConfigDict(extra="allow", strict=True)

    ok: bool
    description: str | None = None


class TelegramClient:
    """Thin, typed wrapper around the Telegram Bot API over raw HTTP."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = API_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 40.0,
    ) -> None:
        if not token.strip():
            raise ValueError("Telegram bot token must be non-empty")
        self._token = token  # kept for redaction only — never logged
        self._http = httpx.Client(
            base_url=f"{base_url}/bot{token}",
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    @log_call()
    def get_updates(
        self,
        offset: int | None = None,
        timeout: int = 30,
        limit: int | None = None,
    ) -> list[Update]:
        """Long-poll ``getUpdates``; returns typed updates (malformed ones skipped)."""
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        if limit is not None:
            params["limit"] = limit
        payload = self._request("GET", "/getUpdates", params=params)
        return parse_updates(payload)

    @log_call()
    def send_message(self, chat_id: int, text: str) -> None:
        """Send a text message; raises ``TelegramAPIError`` on any failure."""
        payload = self._request(
            "POST", "/sendMessage", json={"chat_id": chat_id, "text": text}
        )
        try:
            response = SendMessageResponse.model_validate(payload)
        except ValidationError as exc:
            raise TelegramAPIError(
                f"malformed sendMessage response: {exc.errors()[0]['msg']}"
            ) from None
        if not response.ok:
            detail = f": {response.description}" if response.description else ""
            raise TelegramAPIError(f"sendMessage returned ok=false{detail}")

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Execute one API request; every failure becomes a redacted TelegramAPIError."""
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            # httpx error strings can embed the full bot URL (token included).
            raise TelegramAPIError(
                redact(f"{type(exc).__name__}: {exc}", self._token)
            ) from None
        if response.status_code >= 400:
            detail = self._error_description(response)
            suffix = f": {detail}" if detail else ""
            raise TelegramAPIError(
                redact(f"HTTP {response.status_code} from {path}{suffix}", self._token)
            )
        try:
            return response.json()
        except ValueError:
            raise TelegramAPIError(
                f"non-JSON response body from {path} (HTTP {response.status_code})"
            ) from None

    @staticmethod
    def _error_description(response: httpx.Response) -> str | None:
        """Best-effort extraction of Telegram's human-readable error description."""
        try:
            body = response.json()
        except ValueError:
            return None
        if isinstance(body, dict) and isinstance(body.get("description"), str):
            return body["description"]
        return None