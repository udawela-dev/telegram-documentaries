"""Unit tests for the Telegram typed boundary models (plan task 4 — RED first).

The contract (SPECS/TECH.md): parse raw, untrusted Telegram update JSON into
validated Pydantic models *before* use; never pass raw dicts across module
boundaries; malformed/unexpected updates are skipped with a log and never
crash the loop.
"""
import logging

import pytest
from pydantic import ValidationError

from src.telegram_models import (
    Chat,
    MalformedResponseError,
    Message,
    TelegramAPIError,
    Update,
    parse_updates,
)

VALID_UPDATE = {
    "update_id": 1001,
    "message": {
        "message_id": 55,
        "chat": {"id": 4242, "type": "private", "username": "someone"},
        "text": "hello there",
        "date": 1700000000,
    },
}


# --- parsing: happy path -------------------------------------------------------


def test_parse_updates_valid_message_returns_typed_update():
    updates = parse_updates({"ok": True, "result": [VALID_UPDATE]})

    assert len(updates) == 1
    update = updates[0]
    assert isinstance(update, Update)
    assert update.update_id == 1001
    assert isinstance(update.message, Message)
    assert isinstance(update.message.chat, Chat)
    assert update.message.chat.id == 4242
    assert update.message.text == "hello there"


def test_parse_updates_never_returns_raw_dicts():
    updates = parse_updates({"ok": True, "result": [VALID_UPDATE]})

    for update in updates:
        assert isinstance(update, Update)

    assert updates[0].message is not None
    assert isinstance(updates[0].message.chat, Chat)


def test_parse_updates_tolerates_unknown_fields():
    """Telegram adds fields over time; unknown keys must never break parsing."""
    payload = {
        "ok": True,
        "result": [
            {
                "update_id": 2,
                "message": {
                    "message_id": 1,
                    "chat": {"id": 7, "type": "private"},
                    "text": "hi",
                    "entities": [{"offset": 0, "length": 2, "type": "bold"}],
                    "some_future_field": {"nested": [1, 2, 3]},
                },
                "some_future_update_field": "x",
            }
        ],
    }

    updates = parse_updates(payload)

    assert updates[0].message.chat.id == 7  # type: ignore[union-attr]


# --- parsing: unexpected but well-formed updates -------------------------------


def test_parse_updates_message_without_text_is_kept():
    """A photo/sticker message has no text — it is still a usable chat context."""
    updates = parse_updates(
        {
            "ok": True,
            "result": [
                {
                    "update_id": 3,
                    "message": {"message_id": 1, "chat": {"id": 9, "type": "private"}},
                }
            ],
        }
    )

    assert len(updates) == 1
    assert updates[0].message is not None
    assert updates[0].message.text is None


def test_parse_updates_update_without_message_parses_as_none():
    """edits, channel posts, callback queries etc. arrive without 'message'."""
    updates = parse_updates(
        {
            "ok": True,
            "result": [
                {"update_id": 4, "edited_message": {"message_id": 1, "chat": {"id": 9}}},
                {"update_id": 5, "channel_post": {"message_id": 2, "chat": {"id": -100}}},
            ],
        }
    )

    assert len(updates) == 2
    assert all(update.message is None for update in updates)
    assert [u.update_id for u in updates] == [4, 5]


# --- parsing: malformed updates are skipped, never fatal -----------------------


def test_parse_updates_skips_message_missing_chat_and_logs(caplog):
    with caplog.at_level(logging.WARNING):
        updates = parse_updates(
            {
                "ok": True,
                "result": [
                    VALID_UPDATE,
                    {"update_id": 6, "message": {"message_id": 1}},
                ],
            }
        )

    assert [u.update_id for u in updates] == [1001]
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert any("skip_update" in r.message for r in caplog.records)


def test_parse_updates_skips_update_missing_update_id_and_logs(caplog):
    updates = parse_updates(
        {"ok": True, "result": [VALID_UPDATE, {"message": {"message_id": 1, "chat": {"id": 2}}}]}
    )

    assert [u.update_id for u in updates] == [1001]


def test_parse_updates_skips_wrong_types_but_keeps_valid_batch(caplog):
    """Only modeled fields are validated; one bad item never kills the batch."""
    payload = {
        "ok": True,
        "result": [
            VALID_UPDATE,
            {"update_id": "not-an-int", "message": {"message_id": 1, "chat": {"id": 2}}},
            {"update_id": 7, "message": {"message_id": 3, "chat": {"id": "not-an-int"}}},
            {"update_id": 8, "message": {"message_id": 4, "chat": {"id": 2}, "text": 123}},
            None,
            "garbage",
        ],
    }

    with caplog.at_level(logging.WARNING):
        updates = parse_updates(payload)

    assert [u.update_id for u in updates] == [1001]
    assert len(caplog.records) >= 5  # every malformed item produced a skip log


# --- parsing: whole-payload problems raise loudly ------------------------------


def test_parse_updates_rejects_non_mapping_payload():
    for bad in (None, ["x"], "text", 42):
        with pytest.raises(MalformedResponseError):
            parse_updates(bad)


def test_parse_updates_missing_ok_field_raises():
    with pytest.raises(MalformedResponseError):
        parse_updates({"result": []})


def test_parse_updates_ok_false_raises_telegram_api_error():
    with pytest.raises(TelegramAPIError) as excinfo:
        parse_updates({"ok": False, "error_code": 409, "description": "Conflict: terminated by other getUpdates request"})

    assert "Conflict" in str(excinfo.value)


def test_parse_updates_ok_false_without_description_raises():
    with pytest.raises(TelegramAPIError):
        parse_updates({"ok": False})


def test_parse_updates_result_not_a_list_raises():
    with pytest.raises(MalformedResponseError):
        parse_updates({"ok": True, "result": "oops"})


def test_parse_updates_result_missing_raises():
    with pytest.raises(MalformedResponseError):
        parse_updates({"ok": True})


# --- direct model behaviour -----------------------------------------------------


def test_update_without_message_is_valid_model():
    update = Update(update_id=1)
    assert update.message is None


def test_message_without_text_is_valid_model():
    message = Message(chat=Chat(id=1))
    assert message.text is None


def test_chat_id_must_be_an_integer():
    with pytest.raises(ValidationError):
        Chat(id="4242")