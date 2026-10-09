"""Component tests: the Gateway's Phase 6 Narrator integration.

Transport, the ADK Bouncer and the Converter are doubled; the real Interviewer
state machine, the shared state driver and the real PortraitStore are exercised
for real. The Phase 5 helpers (GateClient / FakeBouncer / FakeConverter /
FakeScripter / ``_run_full_interview``) are **extended, not duplicated**, from
``test_gateway_scripter.py``.

The locked chat-visible completion order is asserted end to end:
**profile text → hybrid photo → script text → voice note**.
"""
import logging

from test_gateway_scripter import (
    HYBRID_BYTES,
    SCRIPT,
    FakeBouncer,
    FakeConverter,
    FakeScripter,
    _run_full_interview,
    _text_update,
)
from test_gateway_scripter import (
    GateClient as _BaseGateClient,
)

from src.gateway import Gateway
from src.interview_state import InterviewStateStore
from src.interviewer import INTERVIEWER_REPLY_RESET, Interviewer
from src.narrator import NARRATOR_REPLY_UNAVAILABLE, NarratorError
from src.portrait_store import PortraitStore
from src.telegram_models import TelegramAPIError

AUDIO_BYTES = b"OggS\x00\x02opus-narration-bytes"


class GateClient(_BaseGateClient):
    """Phase 5 scripted client + voice-note delivery (``sent_voices``)."""

    def __init__(self, *, fail_send_voice: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.sent_voices: list[tuple[int, bytes]] = []
        self._fail_send_voice = fail_send_voice

    def send_voice(
        self,
        chat_id: int,
        voice_bytes: bytes,
        *,
        filename: str = "narration.ogg",
        mime_type: str = "audio/ogg",
    ) -> None:
        if self._fail_send_voice:
            raise TelegramAPIError("Bad Request: sendVoice failed")
        self.sent_voices.append((chat_id, voice_bytes))
        self.calls.append(("voice", chat_id, voice_bytes))


class FakeNarrator:
    """Scripted narrator: preset audio bytes, recorded calls, switchable failure."""

    def __init__(self, audio: bytes = AUDIO_BYTES, fail: bool = False) -> None:
        self._audio = audio
        self.fail = fail
        self.calls: list[tuple[int, str]] = []

    def synthesize(self, chat_id: int, script: str) -> bytes:
        self.calls.append((chat_id, script))
        if self.fail:
            raise NarratorError("ffmpeg is not installed")
        return self._audio


class NoStoreScripter(FakeScripter):
    """Scripter that produces text but never persists it — a missing-script state."""

    def _store_script(self, chat_id: int, script: str) -> None:
        return None


def _gateway(client, store, *, scripter=None, narrator=None, bouncer_verdicts=(True,)):
    return Gateway(
        client,
        bouncer=FakeBouncer(verdicts=list(bouncer_verdicts)),
        interviewer=Interviewer(store=store),
        converter=FakeConverter(),
        portraits=PortraitStore(),
        scripter=scripter,
        narrator=narrator,
    )


# --- locked ordering: profile text → hybrid photo → script text → voice note -----


def test_full_pipeline_sends_profile_photo_script_then_voice(caplog):
    client = GateClient()
    store = InterviewStateStore()
    scripter = FakeScripter(store=store)
    narrator = FakeNarrator()
    gateway = _gateway(client, store, scripter=scripter, narrator=narrator)

    with caplog.at_level(logging.INFO):
        _run_full_interview(gateway, client, chat_id=222, first_update=10)

    profile = gateway._interviewer.state(222).profile
    assert profile is not None
    # The locked four-message completion order, observed as it was sent.
    assert client.calls[-4:] == [
        ("message", 222, profile.summary),
        ("photo", 222, HYBRID_BYTES),
        ("message", 222, SCRIPT),
        ("voice", 222, AUDIO_BYTES),
    ]
    assert client.sent_voices == [(222, AUDIO_BYTES)]
    assert narrator.calls == [(222, SCRIPT)]  # narration uses the stored script
    messages = [r.message for r in caplog.records]
    assert any("event=narrator_started" in m for m in messages)
    assert any("event=narrator_generated" in m for m in messages)
    # plan.md locks the post-send success event as ``narrator_sent`` (distinct
    # from main.py's startup ``narrator_ready model=... voice=...`` log).
    assert any("event=narrator_sent" in m for m in messages)


def test_successful_narration_logs_narrator_sent_not_narrator_ready(caplog):
    """The post-send event is ``narrator_sent``; ``narrator_ready`` is reserved
    for main.py's startup wiring log (no event-name collision)."""
    client = GateClient()
    store = InterviewStateStore()
    gateway = _gateway(
        client, store, scripter=FakeScripter(store=store), narrator=FakeNarrator()
    )

    with caplog.at_level(logging.INFO):
        _run_full_interview(gateway, client, chat_id=223, first_update=10)

    messages = [r.message for r in caplog.records]
    assert any("event=narrator_sent" in m for m in messages)
    assert not any(
        "event=narrator_ready" in m and "chat_id" in m for m in messages
    )


# --- narrator=None regression ---------------------------------------------------


def test_narrator_none_keeps_phases_1_to_5_unchanged(caplog):
    client = GateClient()
    store = InterviewStateStore()
    gateway = _gateway(client, store, scripter=FakeScripter(store=store), narrator=None)

    with caplog.at_level(logging.INFO):
        _run_full_interview(gateway, client, chat_id=333, first_update=50)

    assert client.sent_voices == []
    # Exactly Phases 1-5: the script text is the last thing sent.
    assert client.calls[-1] == ("message", 333, SCRIPT)
    messages = [r.message for r in caplog.records]
    assert not any("event=narrator_" in m for m in messages)


# --- synth / conversion failure -------------------------------------------------


def test_synth_failure_sends_unavailable_and_loop_survives(caplog):
    client = GateClient()
    store = InterviewStateStore()
    gateway = _gateway(
        client, store, scripter=FakeScripter(store=store), narrator=FakeNarrator(fail=True)
    )

    with caplog.at_level(logging.ERROR):
        _run_full_interview(gateway, client, chat_id=444, first_update=90)

    assert client.sent_voices == []
    assert client.sent[-1] == (444, NARRATOR_REPLY_UNAVAILABLE)
    assert gateway._interviewer.state(444).script == SCRIPT  # script stays on state
    assert any("event=narrator_failed" in r.message for r in caplog.records)

    # The loop survives: a later update is still processed.
    client.queue([_text_update(100, chat_id=444, text="/restart")])
    replied = gateway.poll_once()
    assert replied == 1
    assert client.sent[-1] == (444, INTERVIEWER_REPLY_RESET)


# --- send failure ---------------------------------------------------------------


def test_send_voice_failure_does_not_apologise_and_keeps_the_script(caplog):
    client = GateClient(fail_send_voice=True)
    store = InterviewStateStore()
    gateway = _gateway(client, store, scripter=FakeScripter(store=store), narrator=FakeNarrator())

    with caplog.at_level(logging.ERROR):
        _run_full_interview(gateway, client, chat_id=555, first_update=120)

    assert client.sent_voices == []
    # No misleading apology: TTS produced bytes, only delivery failed.
    assert (555, NARRATOR_REPLY_UNAVAILABLE) not in client.sent
    assert gateway._interviewer.state(555).script == SCRIPT
    assert any(
        "event=narrator_send_failed" in r.message and "error_type=TelegramAPIError" in r.message
        for r in caplog.records
    )

    # The loop survives: a later update is still processed.
    client.queue([_text_update(130, chat_id=555, text="/restart")])
    assert gateway.poll_once() == 1
    assert client.sent[-1] == (555, INTERVIEWER_REPLY_RESET)


# --- missing script -------------------------------------------------------------


def test_missing_script_sends_unavailable_without_calling_tts(caplog):
    client = GateClient()
    store = InterviewStateStore()
    scripter = NoStoreScripter(store=store)  # produces text but stores nothing
    narrator = FakeNarrator()
    gateway = _gateway(client, store, scripter=scripter, narrator=narrator)

    with caplog.at_level(logging.ERROR):
        _run_full_interview(gateway, client, chat_id=666, first_update=150)

    assert client.sent_voices == []
    assert gateway._interviewer.state(666).script is None
    assert client.sent[-1] == (666, NARRATOR_REPLY_UNAVAILABLE)
    assert narrator.calls == []  # no TTS without a script
    assert any(
        "event=narrator_failed" in r.message and "reason=missing_script" in r.message
        for r in caplog.records
    )


# --- script source: the voice uses exactly the stored paragraph ------------------


def test_voice_note_uses_the_stored_script_for_the_same_chat(caplog):
    client = GateClient()
    store = InterviewStateStore()
    scripter = FakeScripter(store=store)
    narrator = FakeNarrator()
    gateway = _gateway(client, store, scripter=scripter, narrator=narrator)

    _run_full_interview(gateway, client, chat_id=777, first_update=180)

    stored = gateway._interviewer.state(777).script
    assert stored == SCRIPT
    assert narrator.calls == [(777, stored)]


# --- isolation: one chat's narration never touches another's --------------------


def test_narration_is_chat_scoped():
    client = GateClient()
    store = InterviewStateStore()
    scripter = FakeScripter(store=store)
    narrator = FakeNarrator()
    gateway = _gateway(
        client, store, scripter=scripter, narrator=narrator, bouncer_verdicts=(True, True)
    )

    _run_full_interview(gateway, client, chat_id=881, first_update=200)
    _run_full_interview(gateway, client, chat_id=882, first_update=240)

    assert client.sent_voices[-2:] == [(881, AUDIO_BYTES), (882, AUDIO_BYTES)]
    assert all(chat in (881, 882) for chat, _script in narrator.calls)
    assert store.get(881).script == SCRIPT
    assert store.get(882).script == SCRIPT
