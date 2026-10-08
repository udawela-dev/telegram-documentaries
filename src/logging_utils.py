"""Decorator-based logging helpers — SPECS/TECH.md (logging & error policy).

The policy: comprehensive structured logging via decorators where sensible;
no bare ``except: pass``; no swallowed exceptions; no un-logged fallbacks;
never log the bot token.

This module is intentionally generic — it has no knowledge of secrets or
business objects. ``log_call`` emits structured start/ok/failed records and
*re-raises* so callers decide how far to degrade. ``SecretRedactionFilter``
is a belt-and-braces safety net on the app's output handler; callers must
still never format a secret into a log message in the first place.
"""
from __future__ import annotations

import functools
import logging
import sys
import time
from collections.abc import Callable, Sequence
from typing import ParamSpec, TextIO, TypeVar

P = ParamSpec("P")
R = TypeVar("R")

_REDACTED = "[REDACTED]"


def redact(text: str, *secrets: str) -> str:
    """Replace every known *secret* occurrence in *text* with ``[REDACTED]``."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, _REDACTED)
    return text


class SecretRedactionFilter(logging.Filter):
    """Scrub configured secrets from every record a handler emits.

    A safety net only — it cannot undo a secret that was formatted into a
    message, so log-message construction must never include secrets either.
    """

    def __init__(self, secrets: Sequence[str]) -> None:
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), *self._secrets)
        record.args = ()
        return True


def log_call(
    logger: "logging.Logger | str | None" = None,
    *,
    event: str | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Log structured start/ok/failed records around a callable.

    ``logger`` may be a ``Logger`` instance, a logger name, or None (defaults
    to ``func.__module__``). Failures are logged (never swallowed) and
    re-raised; the failure line carries the exception type and message.
    Exception messages travelling through our code are sanitized of secrets
    before they are raised.
    """

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        log = logger if isinstance(logger, logging.Logger) else logging.getLogger(logger or func.__module__)
        name = event or func.__qualname__

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            started = time.perf_counter()
            log.debug("event=%s stage=start", name)
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                log.error(
                    "event=%s stage=failed duration_ms=%d error_type=%s error=%s",
                    name,
                    _elapsed_ms(started),
                    type(exc).__name__,
                    exc,
                )
                raise
            log.debug("event=%s stage=ok duration_ms=%d", name, _elapsed_ms(started))
            return result

        return wrapper

    return decorate


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def configure_logging(
    *,
    secrets: Sequence[str] = (),
    level: int = logging.INFO,
    stream: TextIO | None = None,
) -> None:
    """One-call app logging setup: structured stderr handler + redaction.

    Idempotent: repeated calls never stack handlers. httpx/httpcore DEBUG
    records render request URLs, which embed the bot token — their threshold
    is raised to WARNING so a DEBUG switch can never leak the token.
    """
    root = logging.getLogger()
    if not any(getattr(handler, "_app_handler", False) for handler in root.handlers):
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        handler.addFilter(SecretRedactionFilter(secrets))
        setattr(handler, "_app_handler", True)
        root.addHandler(handler)
    root.setLevel(level)
    for noisier in ("httpx", "httpcore"):
        logging.getLogger(noisier).setLevel(logging.WARNING)