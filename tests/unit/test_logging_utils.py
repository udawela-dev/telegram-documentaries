"""Unit tests for decorator-based logging helpers (SPECS/TECH.md — logging policy).

Covers the two halves of the policy:
  * ``log_call`` decorator: structured start/ok/failed records, failures
    re-raised (never swallowed, never a bare ``except: pass``).
  * ``SecretRedactionFilter``: the "never log the token" safety net.
"""
import io
import logging

import pytest

from src.logging_utils import SecretRedactionFilter, configure_logging, log_call


@log_call(logger="test.log_call.ok")
def _double(value: int) -> int:
    return value * 2


@log_call(logger="test.log_call.fail")
def _boom() -> None:
    raise ValueError("kaboom")


def test_log_call_logs_start_and_ok(caplog):
    with caplog.at_level(logging.DEBUG):
        result = _double(21)

    assert result == 42
    messages = [r.message for r in caplog.records]
    assert any("event=_double" in m and "stage=start" in m for m in messages)
    assert any("event=_double" in m and "stage=ok" in m for m in messages)


def test_log_call_logs_failure_and_reraises(caplog):
    with caplog.at_level(logging.ERROR):
        # The exception must propagate — the decorator logs, it does not swallow.
        with pytest.raises(ValueError, match="kaboom"):
            _boom()

    failed = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert failed
    assert "stage=failed" in failed[0].message
    assert "error_type=ValueError" in failed[0].message
    assert "kaboom" in failed[0].message


def test_redact_replaces_every_known_secret():
    from src.logging_utils import redact

    assert redact("token=abc-123 rest", "abc-123") == "token=[REDACTED] rest"
    assert redact("no secrets here", "abc-123") == "no secrets here"
    assert redact("a b", "a", "b") == "[REDACTED] [REDACTED]"


def test_secret_redaction_filter_scrubs_secret_from_output():
    logger = logging.getLogger("test.redaction.handler")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SecretRedactionFilter(["super-secret-token"]))
    logger.addHandler(handler)
    try:
        logger.info("request url contains super-secret-token inside")
    finally:
        logger.removeHandler(handler)

    output = stream.getvalue()
    assert "super-secret-token" not in output
    assert "[REDACTED]" in output


def test_secret_redaction_filter_keeps_unrelated_records_intact():
    logger = logging.getLogger("test.redaction.keep")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SecretRedactionFilter(["sekrit"]))
    logger.addHandler(handler)
    try:
        logger.info("event=polling_started chat=%d", 7)
    finally:
        logger.removeHandler(handler)

    output = stream.getvalue()
    assert "event=polling_started chat=7" in output
    assert "ekrit" not in output  # the secret fragment must not appear at all


def test_configure_logging_installs_redacting_handler_and_quiets_http_loggers():
    root = logging.getLogger()
    existing = set(root.handlers)
    try:
        configure_logging(secrets=["tok-123"])

        app_handlers = [h for h in root.handlers if getattr(h, "_app_handler", False)]
        assert app_handlers, "configure_logging must attach its stderr handler to root"
        assert any(
            isinstance(f, SecretRedactionFilter) for f in app_handlers[-1].filters
        ), "the app handler must carry the secret redaction filter"

        # httpx/httpcore DEBUG logs render URLs that embed the bot token.
        assert logging.getLogger("httpx").level >= logging.WARNING
        assert logging.getLogger("httpcore").level >= logging.WARNING
    finally:
        for handler in set(root.handlers) - existing:
            root.removeHandler(handler)
        logging.getLogger("httpx").setLevel(logging.NOTSET)
        logging.getLogger("httpcore").setLevel(logging.NOTSET)


def test_configure_logging_is_idempotent():
    root = logging.getLogger()
    existing = set(root.handlers)
    try:
        configure_logging()
        configure_logging()
        app_handlers = [h for h in root.handlers if getattr(h, "_app_handler", False)]
        assert len(app_handlers) == 1, "repeated configure_logging must not stack handlers"
    finally:
        for handler in set(root.handlers) - existing:
            root.removeHandler(handler)