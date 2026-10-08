"""Component tests: the Gateway's Phase 2 photo gate (The Bouncer).

Transport and the ADK agent are doubled; the routing/resilience logic under
test is real. Mirrors the phase-1 polling-loop tests' style.
"""
import logging

import pytest

from src.bouncer import BouncerDecision, BOUNCER_REJECTION, BOUNCER_UNAVAILABLE_REPLY
from src.gateway import Gateway, REPLY_TEXT
from src.telegram_models import Chat, Message, PhotoSize, TelegramAPIError, TelegramFile, Update


class GateClient:
    """Scripted Telegram client with photo download support."""

    def __init__(self, *, fail_get_file: bool = False, fail_download: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.get_file_calls: list[str] = []
        self._batches: list[list[Update]] = []
        self._fail_get_file = fail_get_file
        self._fail_download = fail_download

    def queue(self, updates: list[Update]) -> None:
        self._batches.append(updates)

    def get_updates(self, offset=None, timeout=30, limit=None):
        return self._batches.pop(0) if self._batches else []

    def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    def get_file(self, file_id: str) -> TelegramFile:
        self.get_file_calls.append(file_id)
        if self._fail_get_file:
            raise TelegramAPIError("Bad Request: wrong file identifier")
        return TelegramFile(file_id=file_id, file_path="photos/sample.jpg")

    def download_file(self, file_path: str) -> bytes:
        if self._fail_download:
            raise TelegramAPIError("ConnectError: connection refused")
        return b"image-bytes"


class FakeBouncer:
    """Scripted bouncer: preset verdicts per call, recorded resets."""

    def __init__(self, verdicts: list[bool], *, classify_raises: Exception | None = None) -> None:
        self._verdicts = list(verdicts)
        self._classify_raises = classify_raises
        self.classify_calls: list[tuple[int, int]] = []  # (bytes_len, chat_id)
        self.resets: list[int] = []

    def classify(self, image_bytes: bytes, chat_id: int) -> BouncerDecision:
        self.classify_calls.append((len(image_bytes), chat_id))
        if self._classify_raises is not None:
            raise self._classify_raises
        human_present = self._verdicts.pop(0)
        return BouncerDecision(
            human_present=human_present,
            reason="clear face" if human_present else "no humans here",
        )

    def reset_chat(self, chat_id: int) -> None:
        self.resets.append(chat_id)


def _text_update(update_id: int, chat_id: int) -> Update:
    return Update(update_id=update_id, message=Message(chat=Chat(id=chat_id), text="hi"))


def _photo_update(update_id: int, chat_id: int, sizes: list[tuple[str, int, int]]) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            chat=Chat(id=chat_id),
            photo=[PhotoSize(file_id=fid, width=w, height=h) for fid, w, h in sizes],
        ),
    )


def _poll(gateway: Gateway) -> int:
    return gateway.poll_once()


# --- happy paths ---------------------------------------------------------------


def test_photo_approved_continues_confirmation_flow():
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    client.queue([_photo_update(1, chat_id=111, sizes=[("small", 90, 90), ("big", 360, 480)])])

    replied = _poll(Gateway(client, bouncer=bouncer))

    assert replied == 1
    assert client.sent == [(111, REPLY_TEXT)]  # unchanged confirmation flow
    assert client.get_file_calls == ["big"]  # largest size chosen
    assert bouncer.classify_calls == [(len(b"image-bytes"), 111)]
    assert bouncer.resets == []


def test_photo_rejected_sends_cheeky_rejection_and_resets_chat():
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[False])
    client.queue([_photo_update(2, chat_id=222, sizes=[("f", 100, 100)])])

    replied = _poll(Gateway(client, bouncer=bouncer))

    assert replied == 1
    assert client.sent == [(222, BOUNCER_REJECTION)]
    assert bouncer.resets == [222]


def test_rejection_resets_only_the_rejected_chat():
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[False, True])
    client.queue(
        [
            _photo_update(3, chat_id=333, sizes=[("f", 100, 100)]),  # rejected
            _photo_update(4, chat_id=444, sizes=[("f", 100, 100)]),  # approved
        ]
    )

    _poll(Gateway(client, bouncer=bouncer))

    assert bouncer.resets == [333]  # chat 444 untouched
    assert client.sent == [(333, BOUNCER_REJECTION), (444, REPLY_TEXT)]


def test_text_messages_keep_phase1_behaviour_with_bouncer_present():
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[])
    client.queue([_text_update(5, chat_id=555)])

    replied = _poll(Gateway(client, bouncer=bouncer))

    assert replied == 1
    assert client.sent == [(555, REPLY_TEXT)]
    assert bouncer.classify_calls == []


def test_photo_with_caption_is_gated_not_replied_to_directly():
    """A photo + text caption: the gate decides, not the text branch."""
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    update = Update(
        update_id=6,
        message=Message(chat=Chat(id=666), text="look at this", photo=[PhotoSize(file_id="f", width=10, height=10)]),
    )
    client.queue([update])

    _poll(Gateway(client, bouncer=bouncer))

    assert bouncer.classify_calls == [(len(b"image-bytes"), 666)]
    assert client.sent == [(666, REPLY_TEXT)]


# --- degradation paths ---------------------------------------------------------


def test_photo_without_bouncer_replies_unavailable_and_logs(caplog):
    client = GateClient()
    client.queue([_photo_update(7, chat_id=777, sizes=[("f", 100, 100)])])

    with caplog.at_level(logging.ERROR):
        replied = _poll(Gateway(client, bouncer=None))

    assert replied == 1
    assert client.sent == [(777, BOUNCER_UNAVAILABLE_REPLY)]
    assert any("event=photo_no_bouncer" in r.message for r in caplog.records)


def test_photo_download_failure_replies_unavailable_and_skips_bouncer(caplog):
    client = GateClient(fail_get_file=True)
    bouncer = FakeBouncer(verdicts=[True])
    client.queue([_photo_update(8, chat_id=888, sizes=[("f", 100, 100)])])

    with caplog.at_level(logging.ERROR):
        replied = _poll(Gateway(client, bouncer=bouncer))

    assert replied == 1
    assert client.sent == [(888, BOUNCER_UNAVAILABLE_REPLY)]
    assert bouncer.classify_calls == []  # gate stopped before the LLM
    assert any("event=photo_download_failed" in r.message for r in caplog.records)


def test_photo_get_file_without_path_treated_as_download_failure():
    class NoPathClient(GateClient):
        def get_file(self, file_id: str) -> TelegramFile:
            return TelegramFile(file_id=file_id, file_path=None)

    client = NoPathClient()
    bouncer = FakeBouncer(verdicts=[True])
    client.queue([_photo_update(9, chat_id=999, sizes=[("f", 100, 100)])])

    replied = _poll(Gateway(client, bouncer=bouncer))

    assert replied == 1
    assert client.sent == [(999, BOUNCER_UNAVAILABLE_REPLY)]
    assert bouncer.classify_calls == []


def test_photo_classify_failure_replies_unavailable_without_reset(caplog):
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[], classify_raises=RuntimeError("genai down"))
    client.queue([_photo_update(10, chat_id=1010, sizes=[("f", 100, 100)])])

    with caplog.at_level(logging.ERROR):
        replied = _poll(Gateway(client, bouncer=bouncer))

    assert replied == 1
    assert client.sent == [(1010, BOUNCER_UNAVAILABLE_REPLY)]
    assert bouncer.resets == []  # unknown verdict → no state reset
    assert any("event=photo_classify_failed" in r.message for r in caplog.records)


def test_reset_failure_never_hides_the_rejection_or_kills_the_loop(caplog):
    class StubbornBouncer(FakeBouncer):
        def reset_chat(self, chat_id: int) -> None:
            raise RuntimeError("session service down")

    client = GateClient()
    bouncer = StubbornBouncer(verdicts=[False])
    client.queue([_photo_update(11, chat_id=1111, sizes=[("f", 100, 100)])])

    with caplog.at_level(logging.ERROR):
        replied = _poll(Gateway(client, bouncer=bouncer))

    assert replied == 1
    assert client.sent == [(1111, BOUNCER_REJECTION)]
    assert any("event=bouncer_session_reset_failed" in r.message for r in caplog.records)


def test_loop_survives_photo_gate_problems_and_keeps_polling():
    """One bad photo batch never kills the loop; the next batch still flows."""
    client = GateClient(fail_download=True)
    bouncer = FakeBouncer(verdicts=[True])
    client.queue([_photo_update(12, chat_id=1212, sizes=[("f", 100, 100)])])
    client.queue([_text_update(13, chat_id=1313)])

    gateway = Gateway(client, bouncer=bouncer, backoff_seconds=0.0)

    assert gateway.poll_once() == 1  # photo: graceful unavailable
    assert gateway.poll_once() == 1  # following text batch: normal reply
    assert client.sent == [(1212, BOUNCER_UNAVAILABLE_REPLY), (1313, REPLY_TEXT)]


def test_offset_advances_even_for_rejected_photo():
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[False])
    client.queue([_photo_update(14, chat_id=1414, sizes=[("f", 100, 100)])])

    gateway = Gateway(client, bouncer=bouncer)
    gateway.poll_once()

    assert gateway.offset == 15  # update 14 consumed, next poll starts at 15