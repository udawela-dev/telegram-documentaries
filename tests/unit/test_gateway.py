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
    GATEWAY_REPLY_UNKNOWN_COMMAND,
    PERSONA_REPLY_CONFIRMED_ATTENBOROUGH,
    PERSONA_REPLY_CONFIRMED_IRWIN,
    PERSONA_REPLY_USAGE,
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
from src.persona import Persona
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
        self.personas: list[Persona] = []
        self._on_write = on_write

    def write_script(self, chat_id: int, profile, persona: Persona = Persona.ATTENBOROUGH) -> str:
        self.write_calls.append(chat_id)
        self.personas.append(persona)
        if self._on_write is not None:
            self._on_write()
        return "a perfectly formed screenplay paragraph"

    def reset_chat(self, chat_id: int) -> None:
        self.resets.append(chat_id)


class RecordNarrator:
    def __init__(self, on_synthesize=None) -> None:
        self.synthesize_calls: list[int] = []
        self.personas: list[Persona] = []
        self._on_synthesize = on_synthesize

    def synthesize(self, chat_id: int, script: str, persona: Persona = Persona.ATTENBOROUGH) -> bytes:
        self.synthesize_calls.append(chat_id)
        self.personas.append(persona)
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


# --- /persona command + persona store (Phase 8) ---------------------------------


def _complete_store(chat_id: int) -> InterviewStateStore:
    store = InterviewStateStore()
    store.save(
        chat_id,
        InterviewState(
            chat_id=chat_id,
            phase=InterviewPhase.COMPLETE,
            profile=_profile(chat_id),
            updated_at=utc_now_iso(),
        ),
    )
    return store


def test_persona_command_without_an_argument_replies_usage():
    client = UClient()
    interviewer, _ = _interviewer_store()
    gateway = Gateway(client, interviewer=interviewer)

    assert gateway._dispatch(_text_update(1, 101, "/persona")) is True

    assert client.sent_text == [(101, PERSONA_REPLY_USAGE)]
    assert gateway._personas == {}, "no persona selected on a bare /persona"


@pytest.mark.parametrize(
    "token,persona,expected",
    [
        ("attenborough", Persona.ATTENBOROUGH, PERSONA_REPLY_CONFIRMED_ATTENBOROUGH),
        ("irwin", Persona.IRWIN, PERSONA_REPLY_CONFIRMED_IRWIN),
        (" IRWIN ", Persona.IRWIN, PERSONA_REPLY_CONFIRMED_IRWIN),
    ],
)
def test_persona_command_switches_and_confirms(token, persona, expected, caplog):
    client = UClient()
    interviewer, _ = _interviewer_store()
    gateway = Gateway(client, interviewer=interviewer)

    gateway._dispatch(_text_update(1, 101, f"/persona {token}"))

    assert client.sent_text == [(101, expected)]
    assert gateway._personas[101] is persona
    assert "event=persona_set" in caplog.text
    assert f"persona={persona.value}" in caplog.text


@pytest.mark.parametrize("token", ["bogus", "orson", "atten", "crikey"])
def test_persona_command_with_an_unknown_token_replies_usage_and_logs(token, caplog):
    client = UClient()
    interviewer, _ = _interviewer_store()
    gateway = Gateway(client, interviewer=interviewer)

    gateway._dispatch(_text_update(1, 101, f"/persona {token}"))

    assert client.sent_text == [(101, PERSONA_REPLY_USAGE)]
    assert 101 not in gateway._personas, "a bad token can never enter state"
    assert "event=persona_unknown_reply" in caplog.text


def test_persona_command_with_an_at_bot_suffix_still_switches():
    client = UClient()
    interviewer, _ = _interviewer_store()
    gateway = Gateway(client, interviewer=interviewer)

    gateway._dispatch(_text_update(1, 101, "/persona@MyBot irwin"))

    assert client.sent_text == [(101, PERSONA_REPLY_CONFIRMED_IRWIN)]
    assert gateway._personas[101] is Persona.IRWIN


def test_persona_command_is_safe_during_interviewing_and_does_not_touch_state():
    client = UClient()
    interviewer, store = _interviewer_store()
    interviewer.start(101)  # INTERVIEWING
    answers_before = store.get(101).answers
    gateway = Gateway(client, interviewer=interviewer)

    gateway._dispatch(_text_update(1, 101, "/persona irwin"))

    assert gateway._personas[101] is Persona.IRWIN
    assert store.get(101).phase is InterviewPhase.INTERVIEWING
    assert store.get(101).answers == answers_before
    assert client.sent_text == [(101, PERSONA_REPLY_CONFIRMED_IRWIN)]


def test_persona_command_works_without_an_interviewer():
    client = UClient()
    gateway = Gateway(client)  # Phase-1 mode: no interviewer wired

    gateway._dispatch(_text_update(1, 101, "/persona irwin"))

    assert client.sent_text == [(101, PERSONA_REPLY_CONFIRMED_IRWIN)]
    assert gateway._personas[101] is Persona.IRWIN


def test_persona_persists_across_restart_and_start():
    client = UClient()
    interviewer, store = _interviewer_store()
    gateway = Gateway(client, interviewer=interviewer)

    gateway._dispatch(_text_update(1, 101, "/persona irwin"))
    gateway._dispatch(_text_update(2, 101, "/restart"))
    gateway._dispatch(_text_update(3, 101, "/start"))

    assert gateway._personas[101] is Persona.IRWIN, (
        "a persona is a preference, not conversation memory"
    )
    assert store.get(101).phase is InterviewPhase.IDLE


def test_persona_is_isolated_per_chat():
    client = UClient()
    interviewer, _ = _interviewer_store()
    gateway = Gateway(client, interviewer=interviewer)

    gateway._dispatch(_text_update(1, 101, "/persona irwin"))

    assert gateway._resolve_persona(101) is Persona.IRWIN
    assert gateway._resolve_persona(202) is Persona.ATTENBOROUGH


def test_resolve_persona_falls_back_to_the_env_default(monkeypatch, caplog):
    monkeypatch.setenv("PERSONA_DEFAULT", "irwin")
    client = UClient()
    gateway = Gateway(client)

    assert gateway._resolve_persona(999) is Persona.IRWIN
    assert "event=persona_resolved" in caplog.text


def test_resolve_persona_prefers_the_stored_chat_choice(monkeypatch):
    monkeypatch.setenv("PERSONA_DEFAULT", "attenborough")
    client = UClient()
    gateway = Gateway(client)
    gateway._personas[101] = Persona.IRWIN

    assert gateway._resolve_persona(101) is Persona.IRWIN


def test_persona_reply_copy_is_locked():
    assert PERSONA_REPLY_USAGE == (
        "pick a presenter: /persona attenborough or /persona irwin"
    )
    assert PERSONA_REPLY_CONFIRMED_ATTENBOROUGH == (
        "Right then — Sir David it is: posh British documentary narration, coming up."
    )
    assert PERSONA_REPLY_CONFIRMED_IRWIN == (
        "Crikey! The wildlife warrior is in — get ready for some Aussie energy."
    )


# --- unknown-command hardening (Phase 8) ----------------------------------------


def test_unknown_command_reply_copy_is_locked():
    assert GATEWAY_REPLY_UNKNOWN_COMMAND == (
        "hmm, I don't know that one — try /start, /restart or /persona"
    )


@pytest.mark.parametrize("command", ["/foo", "/help", "/Wibble", "/personas"])
def test_unknown_command_at_idle_gets_the_unknown_reply_not_a_photo_prompt(
    command, caplog
):
    client = UClient()
    interviewer, _ = _interviewer_store()
    gateway = Gateway(client, interviewer=interviewer)

    gateway._dispatch(_text_update(1, 101, command))

    assert client.sent_text == [(101, GATEWAY_REPLY_UNKNOWN_COMMAND)]
    assert "event=unknown_command" in caplog.text


def test_unknown_command_during_interviewing_is_never_stored_as_an_answer():
    client = UClient()
    interviewer, store = _interviewer_store()
    interviewer.start(101)
    before = store.get(101)
    gateway = Gateway(client, interviewer=interviewer)

    gateway._dispatch(_text_update(1, 101, "/foo"))

    after = store.get(101)
    assert client.sent_text == [(101, GATEWAY_REPLY_UNKNOWN_COMMAND)]
    assert after.phase is InterviewPhase.INTERVIEWING
    assert after.question_index == before.question_index
    assert after.answers == before.answers, "an unknown command is not an answer"


def test_unknown_command_at_complete_gets_the_unknown_reply():
    client = UClient()
    store = _complete_store(101)
    gateway = Gateway(client, interviewer=Interviewer(api_key=None, store=store))

    gateway._dispatch(_text_update(1, 101, "/foo"))

    assert client.sent_text == [(101, GATEWAY_REPLY_UNKNOWN_COMMAND)]


def test_unknown_command_works_without_an_interviewer():
    client = UClient()
    gateway = Gateway(client)

    gateway._dispatch(_text_update(1, 101, "/foo"))

    assert client.sent_text == [(101, GATEWAY_REPLY_UNKNOWN_COMMAND)]


def test_command_matching_is_token_based_not_prefix_based():
    """A command-like prefix (`/startle`) is an unknown command, not a reset —
    and, crucially, is never slurped as an interview answer."""
    client = UClient()
    interviewer, _ = _interviewer_store()
    gateway = Gateway(client, interviewer=interviewer)

    gateway._dispatch(_text_update(1, 101, "/startle"))

    assert client.sent_text == [(101, GATEWAY_REPLY_UNKNOWN_COMMAND)]


def test_start_command_with_an_at_bot_suffix_still_resets():
    client = UClient()
    interviewer, store = _interviewer_store()
    interviewer.start(101)
    gateway = Gateway(client, interviewer=interviewer)

    gateway._dispatch(_text_update(1, 101, "/start@MyBot"))

    assert (101, INTERVIEWER_REPLY_RESET) in client.sent_text
    assert store.get(101).phase is InterviewPhase.IDLE


def test_non_command_text_still_routes_by_phase():
    """Regression: ordinary text is untouched by the command guard."""
    client = UClient()
    interviewer, _ = _interviewer_store()
    gateway = Gateway(client, interviewer=interviewer)

    gateway._dispatch(_text_update(1, 101, "hello there"))

    assert client.sent_text == [(101, GATEWAY_REPLY_NEED_PHOTO)]