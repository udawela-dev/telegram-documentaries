"""Unit tests: Phase 7 gateway hardening (resets, wrong payloads).

The doubles here are tiny scripted stages; the REAL Interviewer + shared
``InterviewStateStore`` drive the phase-dependent routing so the wrong-payload
guards are tested against the true state machine. Honest per repo philosophy:
transport and heavy stages are doubled, the routing logic under test is real.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.bouncer import BOUNCER_UNAVAILABLE_REPLY, BouncerDecision
from src.gateway import (
    GATEWAY_REPLY_NEED_PHOTO,
    GATEWAY_REPLY_PHOTO_DURING_INTERVIEW,
    GATEWAY_REPLY_RETRY_HINT,
    Gateway,
)
from src.interview_state import (
    InterviewPhase,
    InterviewState,
    InterviewStateStore,
    UserProfile,
    utc_now_iso,
)
from src.interviewer import (
    INTERVIEW_QUESTIONS,
    INTERVIEWER_REPLY_RESET,
    Interviewer,
)
from src.portrait_store import PortraitStore
from src.telegram_models import Chat, Message, PhotoSize, Update
from src.temp_assets import TempAssets


class UClient:
    """Scripted transport: records everything, never touches the network."""

    def __init__(self) -> None:
        self.sent_text: list[tuple[int, str]] = []
        self.sent_photos: list[tuple[int, int]] = []
        self.sent_voices: list[tuple[int, int]] = []
        self.batches: list[list[Update]] = []

    def queue(self, updates: list[Update]) -> None:
        self.batches.append(updates)

    def get_updates(self, offset=None, timeout=30, limit=None):
        return self.batches.pop(0) if self.batches else []

    def send_message(self, chat_id: int, text: str) -> None:
        self.sent_text.append((chat_id, text))

    def send_photo(self, chat_id: int, data: bytes, filename: str | None = None) -> None:
        self.sent_photos.append((chat_id, len(data)))

    def send_voice(self, chat_id: int, data: bytes) -> None:
        self.sent_voices.append((chat_id, len(data)))

    def get_file(self, file_id: str):
        from src.telegram_models import TelegramFile

        return TelegramFile(file_id=file_id, file_path="photos/sample.jpg")

    def download_file(self, file_path: str) -> bytes:
        return b"image-bytes"


class RecordBouncer:
    """Scripted approving bouncer with an optional mid-call side effect."""

    def __init__(self, on_classify=None) -> None:
        self.classify_calls: list[int] = []
        self.resets: list[int] = []
        self._on_classify = on_classify

    def classify(self, image_bytes: bytes, chat_id: int) -> BouncerDecision:
        self.classify_calls.append(chat_id)
        if self._on_classify is not None:
            self._on_classify()
        return BouncerDecision(human_present=True, reason="clear face", source="gemini")

    def reset_chat(self, chat_id: int) -> None:
        self.resets.append(chat_id)


class FailBouncer:
    """Bouncer whose classification always fails loudly (retry-hint test)."""

    def classify(self, image_bytes: bytes, chat_id: int) -> BouncerDecision:
        raise RuntimeError("gemini exploded")

    def reset_chat(self, chat_id: int) -> None:
        pass


class RecordConverter:
    def __init__(self, on_hybridize=None) -> None:
        self.hybridize_calls: list[int] = []
        self.resets: list[int] = []
        self._on_hybridize = on_hybridize

    def hybridize(self, chat_id: int, portrait: bytes, profile) -> bytes:
        self.hybridize_calls.append(chat_id)
        if self._on_hybridize is not None:
            self._on_hybridize()
        return b"hybrid-bytes"

    def reset_chat(self, chat_id: int) -> None:
        self.resets.append(chat_id)


class RecordScripter:
    def __init__(self, on_write=None) -> None:
        self.write_calls: list[int] = []
        self.resets: list[int] = []
        self._on_write = on_write

    def write_script(self, chat_id: int, profile) -> str:
        self.write_calls.append(chat_id)
        if self._on_write is not None:
            self._on_write()
        return "a perfectly formed screenplay paragraph"

    def reset_chat(self, chat_id: int) -> None:
        self.resets.append(chat_id)


class RecordNarrator:
    def __init__(self, on_synthesize=None) -> None:
        self.synthesize_calls: list[int] = []
        self._on_synthesize = on_synthesize

    def synthesize(self, chat_id: int, script: str) -> bytes:
        self.synthesize_calls.append(chat_id)
        if self._on_synthesize is not None:
            self._on_synthesize()
        return b"voice-bytes"


def _text_update(update_id: int, chat_id: int, text: str = "hi") -> Update:
    return Update(update_id=update_id, message=Message(chat=Chat(id=chat_id), text=text))


def _photo_update(update_id: int, chat_id: int) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            chat=Chat(id=chat_id),
            photo=[PhotoSize(file_id="f1", width=640, height=480)],
        ),
    )


def _media_update(update_id: int, chat_id: int) -> Update:
    # Neither photo nor text: a video/audio/document/sticker arrives as a
    # Message whose photo and text fields are both absent.
    return Update(update_id=update_id, message=Message(chat=Chat(id=chat_id)))


def _profile(chat_id: int) -> UserProfile:
    return UserProfile(
        chat_id=chat_id, summary="a summary", suggested_animal="wolf"
    )


def _interviewer_store() -> tuple[Interviewer, InterviewStateStore]:
    store = InterviewStateStore()
    return Interviewer(api_key=None, store=store), store


@pytest.fixture(autouse=True)
def _info_logging(caplog):
    caplog.set_level(logging.INFO)


# --- wrong payload at wrong stage -------------------------------------------


def test_idle_text_prompts_for_photo():
    client = UClient()
    interviewer, _ = _interviewer_store()
    gateway = Gateway(
        client,
        interviewer=interviewer,
        reply_text="Hi Mate",
    )

    assert gateway._dispatch(_text_update(1, 101, "hello there")) is True

    assert client.sent_text == [(101, GATEWAY_REPLY_NEED_PHOTO)]
    assert "Hi Mate" not in [t for _, t in client.sent_text]


def test_photo_during_interview_never_runs_the_gate_or_overwrites_portrait():
    client = UClient()
    interviewer, _ = _interviewer_store()
    bouncer = RecordBouncer()
    portraits = PortraitStore()
    portraits.save(101, b"original-portrait")
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        portraits=portraits,
    )

    gateway.poll_once()
    client.queue([_photo_update(1, 101)])
    gateway.poll_once()  # approval → verdict + Q1
    assert bouncer.classify_calls == [101]
    assert any("Human detected" in text for _, text in client.sent_text)

    client.sent_text.clear()
    client.queue([_photo_update(2, 101)])  # second photo, mid-interview
    gateway.poll_once()
    gateway.poll_once()  # no more batches → nothing

    assert bouncer.classify_calls == [101], "gate must NOT run again mid-interview"
    assert client.sent_text == [
        (101, GATEWAY_REPLY_PHOTO_DURING_INTERVIEW)
    ]
    assert portraits.get(101) == b"image-bytes", "mid-interview portrait must not change"


def test_media_message_routes_per_phase():
    client = UClient()
    interviewer, _ = _interviewer_store()
    gateway = Gateway(client, interviewer=interviewer)

    # IDLE → the photo prompt, logged as media.
    gateway.poll_once()
    client.queue([_media_update(1, 101)])
    gateway.poll_once()
    assert client.sent_text == [(101, GATEWAY_REPLY_NEED_PHOTO)]

    # INTERVIEWING → the "answer with text or /restart" copy.
    client.sent_text.clear()
    interviewer.start(101)
    gateway.poll_once()
    client.queue([_media_update(2, 101)])
    gateway.poll_once()
    assert (101, GATEWAY_REPLY_PHOTO_DURING_INTERVIEW) in client.sent_text

    # COMPLETE → the stored profile is re-sent, exactly as text would (Phase 7
    # rule: media gets the same phase reply as text — never a bare "Hi Mate").
    store = InterviewStateStore()
    store.save(
        101,
        InterviewState(
            chat_id=101,
            phase=InterviewPhase.COMPLETE,
            profile=_profile(101),
            updated_at=utc_now_iso(),
        ),
    )
    client.sent_text.clear()
    complete_gateway = Gateway(client, interviewer=Interviewer(api_key=None, store=store))
    client.queue([_media_update(3, 101)])
    complete_gateway.poll_once()
    assert client.sent_text == [(101, _profile(101).summary)]


# --- duplicate updates ------------------------------------------------------


def test_duplicate_update_in_batch_applies_only_once(caplog):
    client = UClient()
    interviewer, _ = _interviewer_store()
    gateway = Gateway(client, interviewer=interviewer)
    client.queue([_text_update(7, 101), _text_update(7, 101)])

    gateway.poll_once()

    # One reply for one effective update; the replay is skipped.
    assert client.sent_text == [(101, GATEWAY_REPLY_NEED_PHOTO)]
    assert "event=skip_duplicate_update update_id=7" in caplog.text


# --- reset sweep ------------------------------------------------------------


def test_reset_command_purges_every_stage_portrait_and_temp_assets(tmp_path):
    client = UClient()
    interviewer, store = _interviewer_store()
    bouncer = RecordBouncer()
    converter = RecordConverter()
    scripter = RecordScripter()
    portraits = PortraitStore()
    portraits.save(101, b"portrait")
    temp_assets = TempAssets()
    staged = Path(tmp_path) / "leftover.ogg"
    staged.write_bytes(b"orphan")
    temp_assets.track(101, staged)
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=converter,
        portraits=portraits,
        scripter=scripter,
        temp_assets=temp_assets,
    )
    interviewer.start(101)  # make the chat non-IDLE

    assert gateway._handle_reset_command(101, 1) is True

    assert bouncer.resets == [101]
    assert converter.resets == [101]
    assert scripter.resets == [101]
    assert (101, INTERVIEWER_REPLY_RESET) in client.sent_text
    assert store.get(101).phase is InterviewPhase.IDLE
    assert portraits.get(101) is None
    assert not staged.exists(), "staged temp asset must be purged by the reset"


def test_reset_is_scoped_to_the_requesting_chat():
    client = UClient()
    interviewer, store = _interviewer_store()
    portraits = PortraitStore()
    portraits.save(101, b"a")
    portraits.save(202, b"b")
    temp_assets = TempAssets()
    gateway = Gateway(
        client,
        interviewer=interviewer,
        portraits=portraits,
        temp_assets=temp_assets,
    )
    interviewer.start(202)  # the OTHER chat stays busy

    gateway._handle_reset_command(101, 1)

    assert store.get(101).phase is InterviewPhase.IDLE
    assert store.get(202).phase is InterviewPhase.INTERVIEWING
    assert portraits.get(101) is None
    assert portraits.get(202) == b"b"


def test_reset_queued_behind_a_stage_update_is_honoured_at_the_next_free_step():
    """Sequential-dispatch guarantee (Phase 7, post-review): dispatch is strictly
    single-threaded, so a /restart can never interleave mid-generation. An
    answer that completes the run and a /restart in the SAME batch are processed
    strictly in order: the run completes and the hybrid IS delivered (a processed
    step's output is never retroactively dropped), and the reset then lands at
    the next free step with the full purge."""
    client = UClient()
    interviewer, store = _interviewer_store()
    portraits = PortraitStore()
    portraits.save(101, b"portrait")
    store.save(
        101,
        InterviewState(
            chat_id=101,
            phase=InterviewPhase.INTERVIEWING,
            question_index=7,  # Q7 pending; the next answer completes the run
            answers=[(INTERVIEW_QUESTIONS[i], "answer") for i in range(6)],
            updated_at=utc_now_iso(),
        ),
    )
    gateway = Gateway(
        client,
        interviewer=interviewer,
        converter=RecordConverter(),
        portraits=portraits,
        scripter=None,
        narrator=None,
    )
    client.queue([_text_update(1, 101, "wolf"), _text_update(2, 101, "/restart")])

    gateway.poll_once()

    assert client.sent_photos == [
        (101, len(b"hybrid-bytes"))
    ], "the in-flight step's delivery completes before the reset is processed"
    assert store.get(101).phase is InterviewPhase.IDLE, (
        "the reset is honoured immediately after, at the next free step"
    )
    assert portraits.get(101) is None
    assert (101, INTERVIEWER_REPLY_RESET) in client.sent_text


# --- graceful failure + retry hints -----------------------------------------


def test_vision_failure_sends_unavailable_reply_and_retry_hint():
    client = UClient()
    interviewer, _ = _interviewer_store()
    gateway = Gateway(client, bouncer=FailBouncer(), interviewer=interviewer)
    client.queue([_photo_update(1, 101)])

    gateway.poll_once()

    texts = [t for _, t in client.sent_text]
    assert BOUNCER_UNAVAILABLE_REPLY in texts
    assert GATEWAY_REPLY_RETRY_HINT in texts


# --- polling never dies -----------------------------------------------------


def test_poll_survives_exploding_stage_and_keeps_answering():
    client = UClient()
    interviewer, _ = _interviewer_store()

    class BoomBouncer:
        def classify(self, image_bytes, chat_id):
            raise RuntimeError("kaboom")

        def reset_chat(self, chat_id):
            pass

    gateway = Gateway(client, bouncer=BoomBouncer(), interviewer=interviewer)
    client.queue([_photo_update(1, 101), _text_update(2, 101)])

    gateway.poll_once()

    # The photo failed gracefully; the very next text still gets served.
    assert any(t == GATEWAY_REPLY_RETRY_HINT for _, t in client.sent_text)
    assert (101, GATEWAY_REPLY_NEED_PHOTO) in client.sent_text