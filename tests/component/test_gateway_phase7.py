"""Component tests: Phase 7 resets and wrong-payload guards on the real flow.

The real Interviewer state machine, shared ``InterviewStateStore``, real
``PortraitStore`` and real ``TempAssets`` are wired; the transport, the ADK
Bouncer, and the heavy Converter/Scripter/Narrator work are scripted doubles
(transport and stage boundaries, exactly like ``test_gateway_interviewer.py``).
Proves the user-visible reset contract: a reset returns ONLY the requesting
chat to a clean ``IDLE`` state from any phase, purges its assets, and a fresh
photo re-enters the pipeline immediately — all in one running process.
"""
from __future__ import annotations

import logging

import pytest

from src.bouncer import BouncerDecision
from src.gateway import (
    GATEWAY_REPLY_PHOTO_DURING_INTERVIEW,
    Gateway,
)
from src.interview_state import InterviewPhase, InterviewStateStore
from src.interviewer import INTERVIEW_QUESTIONS, INTERVIEWER_REPLY_RESET, Interviewer
from src.portrait_store import PortraitStore
from src.telegram_models import Chat, Message, PhotoSize, TelegramFile, Update
from src.temp_assets import TempAssets


class GateClient:
    """Scripted Telegram client (house style, mirrors test_gateway_interviewer)."""

    def __init__(self) -> None:
        self.sent_text: list[tuple[int, str]] = []
        self.sent_photos: list[tuple[int, int]] = []
        self.sent_voices: list[tuple[int, int]] = []
        self._batches: list[list[Update]] = []

    def queue(self, updates: list[Update]) -> None:
        self._batches.append(updates)

    def get_updates(self, offset=None, timeout=30, limit=None):
        return self._batches.pop(0) if self._batches else []

    def send_message(self, chat_id: int, text: str) -> None:
        self.sent_text.append((chat_id, text))

    def send_photo(self, chat_id: int, data: bytes, filename: str | None = None) -> None:
        self.sent_photos.append((chat_id, len(data)))

    def send_voice(self, chat_id: int, data: bytes) -> None:
        self.sent_voices.append((chat_id, len(data)))

    def get_file(self, file_id: str) -> TelegramFile:
        return TelegramFile(file_id=file_id, file_path="photos/sample.jpg")

    def download_file(self, file_path: str) -> bytes:
        return b"image-bytes"


class ApproveBouncer:
    """Always-approving Bouncer double; records resets."""

    def __init__(self) -> None:
        self.resets: list[int] = []

    def classify(self, image_bytes: bytes, chat_id: int) -> BouncerDecision:
        return BouncerDecision(human_present=True, reason="clear face", source="gemini")

    def reset_chat(self, chat_id: int) -> None:
        self.resets.append(chat_id)


class SimpleConverter:
    def __init__(self) -> None:
        self.resets: list[int] = []

    def hybridize(self, chat_id: int, portrait: bytes, profile) -> bytes:
        return b"hybrid-bytes"

    def reset_chat(self, chat_id: int) -> None:
        self.resets.append(chat_id)


class SimpleScripter:
    """Writes straight onto the shared store so the narrator can read it."""

    def __init__(self, store: InterviewStateStore) -> None:
        self._store = store
        self.resets: list[int] = []

    def write_script(self, chat_id: int, profile, persona=None) -> str:
        state = self._store.get(chat_id)
        self._store.save(
            chat_id,
            state.model_copy(update={"script": "a documentary paragraph"}),
        )
        return "a documentary paragraph"

    def reset_chat(self, chat_id: int) -> None:
        self.resets.append(chat_id)


class SimpleNarrator:
    def __init__(self) -> None:
        self.synthesize_calls: list[int] = []

    def synthesize(self, chat_id: int, script: str, persona=None) -> bytes:
        self.synthesize_calls.append(chat_id)
        return b"voice-bytes"


def _text_update(update_id: int, chat_id: int, text: str) -> Update:
    return Update(update_id=update_id, message=Message(chat=Chat(id=chat_id), text=text))


def _photo_update(update_id: int, chat_id: int) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            chat=Chat(id=chat_id),
            photo=[PhotoSize(file_id="f1", width=640, height=480)],
        ),
    )


def _gateway(client: GateClient, store: InterviewStateStore) -> tuple[Gateway, dict]:
    """Wired Phase 7 gateway: real interviewer + store, doubled heavy stages."""
    interviewer = Interviewer(api_key=None, store=store)
    portraits = PortraitStore()
    temp_assets = TempAssets()
    bouncer = ApproveBouncer()
    converter = SimpleConverter()
    scripter = SimpleScripter(store)
    narrator = SimpleNarrator()
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=converter,
        portraits=portraits,
        scripter=scripter,
        narrator=narrator,
        temp_assets=temp_assets,
    )
    return gateway, {
        "interviewer": interviewer,
        "store": store,
        "portraits": portraits,
        "temp_assets": temp_assets,
        "bouncer": bouncer,
        "converter": converter,
        "scripter": scripter,
        "narrator": narrator,
    }


def _run_interview_to_completion(client: GateClient, gateway: Gateway, chat_id: int) -> None:
    """Drive the full interview by dispatching one update per question."""
    for index, _question in enumerate(INTERVIEW_QUESTIONS):
        client.queue([_text_update(100 + index, chat_id, f"answer {index}")])
        gateway.poll_once()


@pytest.fixture(autouse=True)
def _info_logging(caplog):
    caplog.set_level(logging.INFO)


def test_full_run_then_restart_then_second_run_in_one_process(tmp_path):
    client = GateClient()
    store = InterviewStateStore()
    gateway, parts = _gateway(client, store)
    chat_id = 101

    # --- run 1: photo → interview → completion chain → narrate -------------
    client.queue([_photo_update(1, chat_id)])
    gateway.poll_once()
    assert (chat_id, INTERVIEWER_REPLY_RESET) not in client.sent_text
    assert any("Human detected" in t for _, t in client.sent_text)
    _run_interview_to_completion(client, gateway, chat_id)
    assert client.sent_photos == [(chat_id, 12)]  # hybrid delivered (12 bytes)
    assert any(t == "a documentary paragraph" for _, t in client.sent_text)
    assert parts["narrator"].synthesize_calls == [chat_id]
    assert client.sent_voices == [(chat_id, 11)]

    # Stale narrator artifacts staged on disk for the chat, as an interrupted
    # TTS worker would have left them (the reset must sweep them).
    leftover = tmp_path / "test-reset-leftover.ogg"
    leftover.write_bytes(b"orphan")
    parts["temp_assets"].track(chat_id, leftover)

    # --- /restart: instant reset, no confirmation --------------------------
    client.queue([_text_update(200, chat_id, "/restart")])
    gateway.poll_once()

    assert (chat_id, INTERVIEWER_REPLY_RESET) in client.sent_text
    assert store.get(chat_id).phase is InterviewPhase.IDLE
    assert store.get(chat_id).profile is None
    assert store.get(chat_id).script is None
    assert store.get(chat_id).answers == []
    assert parts["portraits"].get(chat_id) is None
    assert parts["converter"].resets == [chat_id]
    assert parts["scripter"].resets == [chat_id]
    assert parts["bouncer"].resets == [chat_id]
    assert not leftover.exists(), "/restart must purge staged temp assets"

    # --- run 2 starts cleanly, in the SAME process, immediately ------------
    sent_before_run2 = len(client.sent_text)
    client.queue([_photo_update(2, chat_id)])
    gateway.poll_once()

    q1 = [t for _, t in client.sent_text[sent_before_run2:] if t == INTERVIEW_QUESTIONS[0]]
    assert q1, "a fresh photo must restart the interview with Q1 immediately"
    assert parts["narrator"].synthesize_calls == [chat_id], "no stale narration"


def test_two_chats_are_isolated_under_interleaved_resets():
    client = GateClient()
    store = InterviewStateStore()
    gateway, parts = _gateway(client, store)
    a, b = 101, 202

    # Both chats start interviews.
    client.queue([_photo_update(1, a), _photo_update(2, b)])
    gateway.poll_once()

    # A answers twice, B once; then ONLY A resets.
    client.queue([_text_update(10, a, "answer A1")])
    gateway.poll_once()
    client.queue([_text_update(11, b, "answer B1")])
    gateway.poll_once()
    client.queue([_text_update(12, a, "answer A2")])
    gateway.poll_once()
    client.queue([_text_update(20, a, "/start")])
    gateway.poll_once()

    assert store.get(a).phase is InterviewPhase.IDLE
    assert store.get(a).answers == []
    assert store.get(b).phase is InterviewPhase.INTERVIEWING
    assert store.get(b).answers == [(INTERVIEW_QUESTIONS[0], "answer B1")]
    assert parts["portraits"].get(a) is None
    assert parts["portraits"].get(b) is not None, "B's portrait must survive A's reset"
    assert parts["converter"].resets == [a], "only A's converter session is purged"
    assert parts["bouncer"].resets == [a]


def test_photo_during_interview_is_refused_without_retriggering_the_gate():
    client = GateClient()
    store = InterviewStateStore()
    gateway, parts = _gateway(client, store)
    chat_id = 101

    client.queue([_photo_update(1, chat_id)])
    gateway.poll_once()
    original_portrait = parts["portraits"].get(chat_id)

    client.queue([_photo_update(2, chat_id)])
    gateway.poll_once()

    assert (chat_id, GATEWAY_REPLY_PHOTO_DURING_INTERVIEW) in client.sent_text
    # The interview was NOT restarted by the second photo: still awaiting the
    # first answer (question_index 1 = Q2 is next; Q1's answer is pending), and
    # the approved portrait was not overwritten.
    state = store.get(chat_id)
    assert state.phase is InterviewPhase.INTERVIEWING
    assert state.question_index == 1, "the interview must not be restarted by the photo"
    assert parts["portraits"].get(chat_id) == original_portrait