"""Integration tests: Phase 7 reset end-to-end, one process, two full runs.

Drives the real Gateway against a scripted Telegram transport with the REAL
Interviewer state machine, shared ``InterviewStateStore``, ``PortraitStore``
and ``TempAssets``. The ADK Bouncer and the heavy Gemini stages (Converter,
Scripter, Narrator) are scripted doubles — the transport and model boundaries,
honestly labelled per repo philosophy; the real voice-note path is covered by
the guarded live tests (``test_live_narrator.py`` etc.).

Proves requirement 4 of the spec: a complete run, then ``/restart`` (and
``/start``) WITHOUT restarting the Python process, a second complete run that
starts clean, no stale task output leaking across the reset, and a polling
loop that keeps answering after every invalid input.
"""
from __future__ import annotations

from src.gateway import (
    GATEWAY_REPLY_RETRY_HINT,
    Gateway,
)
from src.interview_state import InterviewPhase, InterviewStateStore
from src.interviewer import INTERVIEW_QUESTIONS, INTERVIEWER_REPLY_RESET, Interviewer
from src.portrait_store import PortraitStore
from src.telegram_models import Chat, Message, PhotoSize, TelegramFile, Update
from src.temp_assets import TempAssets


class E2EClient:
    """Recording Telegram transport; never touches the network."""

    def __init__(self) -> None:
        self.sent_text: list[tuple[int, str]] = []
        self.sent_photos: list[tuple[int, int]] = []
        self.sent_voices: list[tuple[int, int]] = []
        self.polls: int = 0
        self._batches: list[list[Update]] = []

    def queue(self, updates: list[Update]) -> None:
        self._batches.append(updates)

    def get_updates(self, offset=None, timeout=30, limit=None):
        self.polls += 1
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


class FakeBouncer:
    def __init__(self) -> None:
        self.resets: list[int] = []

    def classify(self, image_bytes: bytes, chat_id: int):
        from src.bouncer import BouncerDecision

        return BouncerDecision(human_present=True, reason="clear face", source="gemini")

    def reset_chat(self, chat_id: int) -> None:
        self.resets.append(chat_id)


class FakeConverter:
    def __init__(self) -> None:
        self.resets: list[int] = []

    def hybridize(self, chat_id: int, portrait: bytes, profile) -> bytes:
        return b"P" * 512  # a plausible JPEG-ish payload

    def reset_chat(self, chat_id: int) -> None:
        self.resets.append(chat_id)


class FakeScripter:
    """Mirrors the real Scripter's contract: store the script, then return it."""

    def __init__(self, store: InterviewStateStore) -> None:
        self._store = store
        self.resets: list[int] = []

    def write_script(self, chat_id: int, profile) -> str:
        state = self._store.get(chat_id)
        self._store.save(
            chat_id,
            state.model_copy(update={"script": "a documentary paragraph"}),
        )
        return "a documentary paragraph"

    def reset_chat(self, chat_id: int) -> None:
        self.resets.append(chat_id)


class FakeNarrator:
    def __init__(self) -> None:
        self.synthesize_calls: list[int] = []

    def synthesize(self, chat_id: int, script: str) -> bytes:
        self.synthesize_calls.append(chat_id)
        return b"O" * 256  # a plausible OGG payload


def _photo(update_id: int, chat_id: int) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            chat=Chat(id=chat_id),
            photo=[PhotoSize(file_id="f1", width=640, height=480)],
        ),
    )


def _text(update_id: int, chat_id: int, text: str) -> Update:
    return Update(update_id=update_id, message=Message(chat=Chat(id=chat_id), text=text))


def _media(update_id: int, chat_id: int) -> Update:
    return Update(update_id=update_id, message=Message(chat=Chat(id=chat_id)))


def _rig() -> tuple[Gateway, E2EClient, dict, InterviewStateStore]:
    client = E2EClient()
    store = InterviewStateStore()
    interviewer = Interviewer(api_key=None, store=store)
    portraits = PortraitStore()
    temp_assets = TempAssets()
    parts = {
        "interviewer": interviewer,
        "portraits": portraits,
        "temp_assets": temp_assets,
        "bouncer": FakeBouncer(),
        "converter": FakeConverter(),
        "scripter": FakeScripter(store),
        "narrator": FakeNarrator(),
    }
    gateway = Gateway(
        client,
        bouncer=parts["bouncer"],
        interviewer=interviewer,
        converter=parts["converter"],
        portraits=portraits,
        scripter=parts["scripter"],
        narrator=parts["narrator"],
        temp_assets=temp_assets,
    )
    return gateway, client, parts, store


def _run_interview(client: E2EClient, gateway: Gateway, chat_id: int, base_id: int) -> None:
    for index, _question in enumerate(INTERVIEW_QUESTIONS):
        client.queue([_text(base_id + index, chat_id, f"answer {index}")])
        gateway.poll_once()


def test_two_complete_runs_with_restart_between_them_one_process(tmp_path):
    gateway, client, parts, store = _rig()
    chat = 101

    # ---- run 1: complete portrait → interview → photo → script → voice -----
    client.queue([_photo(1, chat)])
    gateway.poll_once()
    _run_interview(client, gateway, chat, base_id=20)
    assert parts["narrator"].synthesize_calls == [chat]
    assert client.sent_voices and client.sent_photos
    assert any(t == "a documentary paragraph" for _, t in client.sent_text)

    # Simulate an orphaned TTS temp file the reset must sweep.
    orphan = tmp_path / "e2e-orphan.ogg"
    orphan.write_bytes(b"orphan")
    parts["temp_assets"].track(chat, orphan)

    # ---- /restart: instant, in-process, one chat only ----------------------
    client.queue([_text(90, chat, "/restart")])
    gateway.poll_once()

    state = store.get(chat)
    assert state.phase is InterviewPhase.IDLE
    assert state.answers == [] and state.profile is None and state.script is None
    assert parts["portraits"].get(chat) is None
    assert parts["converter"].resets == [chat]
    assert parts["scripter"].resets == [chat]
    assert parts["bouncer"].resets == [chat]
    assert not orphan.exists(), "reset purged the orphaned stage temp file"
    assert (chat, INTERVIEWER_REPLY_RESET) in client.sent_text

    # ---- run 2: fresh start in the SAME process, immediately ---------------
    run2_start = len(client.sent_text)
    client.queue([_photo(100, chat)])
    gateway.poll_once()
    run2_sent = client.sent_text[run2_start:]
    assert any(t == INTERVIEW_QUESTIONS[0] for _, t in run2_sent), "run 2 asks Q1"
    # No stale run-1 output may leak: narrator synthesize count unchanged.
    assert parts["narrator"].synthesize_calls == [chat]


def test_restart_mid_interview_invalidates_the_aborted_run():
    gateway, client, parts, store = _rig()
    chat = 202

    client.queue([_photo(1, chat)])
    gateway.poll_once()
    client.queue([_text(2, chat, "answer 1")])
    gateway.poll_once()
    assert store.get(chat).answers, "the interview is mid-flight"

    # Mid-interview restart (during a question, no confirmation).
    client.queue([_text(3, chat, "/start")])
    gateway.poll_once()

    assert store.get(chat).phase is InterviewPhase.IDLE
    assert store.get(chat).answers == []
    # The old run must not resurface: a completion chain never starts
    # spontaneously, and no script/photo/voice was produced for it.
    assert parts["narrator"].synthesize_calls == []
    assert client.sent_photos == [] and client.sent_voices == []

    # A fresh photo restarts the pipeline from the top.
    client.queue([_photo(4, chat)])
    gateway.poll_once()
    assert store.get(chat).phase is InterviewPhase.INTERVIEWING
    # Q1 asked (Q2 next) — question_index 1 means the pending answer is Q1's.
    assert store.get(chat).question_index == 1


def test_polling_survives_invalid_inputs_and_gemini_failures():
    gateway, client, parts, store = _rig()
    chat = 303

    # Phase 7: make the converter blow up during the completion chain so the
    # graceful-degradation + retry-hint path is exercised live in the flow.
    def _boom(chat_id, portrait, profile):
        raise RuntimeError("RESOURCE_EXHAUSTED")

    parts["converter"].hybridize = _boom

    # A run: photo → answers → completion → converter blows up → degraded reply.
    client.queue([_photo(1, chat)])
    gateway.poll_once()
    _run_interview(client, gateway, chat, base_id=20)

    texts = [t for _, t in client.sent_text]
    assert GATEWAY_REPLY_RETRY_HINT in texts, "a retry hint follows the failure"
    assert parts["narrator"].synthesize_calls == [], "no narrator on a failed run"

    # Invalid inputs in a row — the loop keeps answering every time.
    polls_before = client.polls
    client.queue([_media(50, chat)])
    gateway.poll_once()
    client.queue([_photo(51, chat)])  # photo mid-run (COMPLETE→IDLE here? after fail, state COMPLETE)
    gateway.poll_once()
    client.queue([_text(52, chat, "/restart")])
    gateway.poll_once()

    assert store.get(chat).phase is InterviewPhase.IDLE
    assert client.polls >= polls_before + 3, "the loop never stopped polling"
    # The last batch was still served after three rounds of hostile input.
    assert (chat, INTERVIEWER_REPLY_RESET) in client.sent_text