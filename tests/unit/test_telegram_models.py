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
    TelegramFile,
    Update,
    parse_file,
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


# --- photo messages (Phase 2 — The Bouncer) ------------------------------------


def test_photo_message_parses_typed_photo_sizes():
    updates = parse_updates(
        {
            "ok": True,
            "result": [
                {
                    "update_id": 101,
                    "message": {
                        "message_id": 9,
                        "chat": {"id": 4242, "type": "private"},
                        "date": 1700000000,
                        "photo": [
                            {
                                "file_id": "small-file-id",
                                "file_unique_id": "u-small",
                                "width": 90,
                                "height": 120,
                                "file_size": 4000,
                            },
                            {
                                "file_id": "big-file-id",
                                "file_unique_id": "u-big",
                                "width": 360,
                                "height": 480,
                                "file_size": 64000,
                            },
                        ],
                    },
                }
            ],
        }
    )

    message = updates[0].message
    assert message is not None
    assert isinstance(message.photo, list)
    assert [(p.file_id, p.width, p.height, p.file_size) for p in message.photo] == [
        ("small-file-id", 90, 120, 4000),
        ("big-file-id", 360, 480, 64000),
    ]
    assert message.text is None


def test_photo_message_keeps_any_text_alongside_the_photo():
    updates = parse_updates(
        {
            "ok": True,
            "result": [
                {
                    "update_id": 102,
                    "message": {
                        "message_id": 10,
                        "chat": {"id": 5, "type": "private"},
                        "text": "capture me",
                        "photo": [
                            {"file_id": "f1", "file_unique_id": "u1", "width": 10, "height": 10}
                        ],
                    },
                }
            ],
        }
    )

    message = updates[0].message
    assert message is not None
    assert message.text == "capture me"
    assert message.photo is not None and len(message.photo) == 1


def test_text_message_has_no_photo():
    updates = parse_updates({"ok": True, "result": [VALID_UPDATE]})

    assert updates[0].message is not None
    assert updates[0].message.photo is None


def test_photo_sizes_missing_file_id_are_skipped_with_log(caplog):
    with caplog.at_level(logging.WARNING):
        updates = parse_updates(
            {
                "ok": True,
                "result": [
                    {
                        "update_id": 103,
                        "message": {
                            "message_id": 11,
                            "chat": {"id": 3, "type": "private"},
                            "photo": [{"file_unique_id": "u", "width": 10, "height": 10}],
                        },
                    }
                ],
            }
        )

    assert updates == []  # the update as a whole was malformed → skipped
    assert any("skip_update" in r.message for r in caplog.records)


# --- getFile boundary (Phase 2) -------------------------------------------------


def test_parse_file_valid_result_returns_typed_file():
    parsed = parse_file(
        {"ok": True, "result": {"file_id": "f1", "file_unique_id": "u1", "file_size": 1234, "file_path": "photos/x.jpg"}}
    )

    assert isinstance(parsed, TelegramFile)
    assert parsed.file_id == "f1"
    assert parsed.file_path == "photos/x.jpg"
    assert parsed.file_size == 1234


def test_parse_file_tolerates_missing_file_path():
    parsed = parse_file({"ok": True, "result": {"file_id": "f1"}})

    assert parsed.file_path is None


def test_parse_file_ok_false_raises_telegram_api_error():
    with pytest.raises(TelegramAPIError) as excinfo:
        parse_file({"ok": False, "error_code": 400, "description": "Bad Request: file not found"})

    assert "file not found" in str(excinfo.value)


def test_parse_file_invalid_result_raises_malformed():
    with pytest.raises(MalformedResponseError):
        parse_file({"ok": True, "result": {"file_path": "no-file-id"}})

    for bad in (None, "x", 42, [], {}):
        with pytest.raises(MalformedResponseError):
            parse_file({"ok": True, "result": bad})


def test_parse_file_non_mapping_payload_raises():
    for bad in (None, ["x"], "text", 42):
        with pytest.raises(MalformedResponseError):
            parse_file(bad)