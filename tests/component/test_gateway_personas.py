"""Component tests: presenter personas and unknown-command guards on the real flow.

The real Interviewer state machine, shared ``InterviewStateStore``, real
``PortraitStore`` and real ``TempAssets`` are wired; the transport, the ADK
Bouncer, and the heavy Converter/Scripter/Narrator work are scripted doubles
(house style, exactly like ``test_gateway_phase7.py``).

Proves the Phase 8 user decisions end-to-end in one running process:

* ``/persona irwin`` selects the irwin presenter for THIS chat only;
* the chosen persona reaches both the Scripter (tone-flavoured script) and the
  Narrator (voice selection) on the real pipeline;
* the choice persists across ``/restart`` into a second run;
* two chats on different personas stay isolated;
* an unknown ``/command`` mid-interview is answered with the locked reply and
  is NEVER stored as an interview answer (the interview keeps its position).
"""
from __future__ import annotations

import logging

import pytest

from src.bouncer import BouncerDecision
from src.gateway import (
    GATEWAY_REPLY_UNKNOWN_COMMAND,
    PERSONA_REPLY_CONFIRMED,
    PERSONA_REPLY_USAGE,
    Gateway,
)
from src.interview_state import InterviewPhase, InterviewStateStore
from src.interviewer import INTERVIEW_QUESTIONS, INTERVIEWER_REPLY_RESET, Interviewer
from src.persona import Persona
from src.portrait_store import PortraitStore
from src.temp_assets import TempAssets
from src.telegram_models import Chat, Message, PhotoSize, TelegramFile, Update


class GateClient:
    """Scripted Telegram client (house style, mirrors test_gateway_phase7)."""

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


class PersonaScripter:
    """Writes onto the shared store; flavours the script by persona so the tone
    decision is observable end-to-end (mirrors LocalScriptWriter's opener)."""

    def __init__(self, store: InterviewStateStore) -> None:
        self._store = store
        self.resets: list[int] = []
        self.personas: list[Persona] = []

    def write_script(self, chat_id: int, profile, persona: Persona) -> str:
        state = self._store.get(chat_id)
        opener = "Crikey! " if persona is Persona.IRWIN else ""
        script = f"{opener}a documentary paragraph"
        self._store.save(
            chat_id,
            state.model_copy(update={"script": script}),
        )
        self.personas.append(persona)
        return script

    def reset_chat(self, chat_id: int) -> None:
        self.resets.append(chat_id)


class PersonaNarrator:
    def __init__(self) -> None:
        self.personas: list[Persona] = []

    def synthesize(self, chat_id: int, script: str, persona: Persona) -> bytes:
        self.personas.append(persona)
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
    """Wired gateway: real interviewer + store, doubled heavy stages."""
    interviewer = Interviewer(api_key=None, store=store)
    portraits = PortraitStore()
    temp_assets = TempAssets()
    gateway = Gateway(
        client,
        bouncer=ApproveBouncer(),
        interviewer=interviewer,
        converter=SimpleConverter(),
        portraits=portraits,
        scripter=PersonaScripter(store),
        narrator=PersonaNarrator(),
        temp_assets=temp_assets,
    )
    return gateway, {
        "interviewer": interviewer,
        "store": store,
        "portraits": portraits,
        "temp_assets": temp_assets,
        "bouncer": gateway._bouncer,
        "converter": gateway._converter,
        "scripter": gateway._scripter,
        "narrator": gateway._narrator,
    }


def _run_interview_to_completion(client: GateClient, gateway: Gateway, chat_id: int) -> None:
    """Drive the full interview by dispatching one update per question."""
    for index, _question in enumerate(INTERVIEW_QUESTIONS):
        client.queue([_text_update(100 + index, chat_id, f"answer {index}")])
        gateway.poll_once()


@pytest.fixture(autouse=True)
def _info_logging(caplog):
    caplog.set_level(logging.INFO)


def test_irwin_persona_reaches_scripter_and_narrator_on_a_full_run():
    client = GateClient()
    store = InterviewStateStore()
    gateway, parts = _gateway(client, store)
    chat_id = 101

    # Choose the irwin presenter before starting.
    client.queue([_text_update(1, chat_id, "/persona irwin")])
    gateway.poll_once()
    assert (chat_id, PERSONA_REPLY_CONFIRMED[Persona.IRWIN]) in client.sent_text

    # Full run: photo -> interview -> hybrid -> script -> voice note.
    client.queue([_photo_update(2, chat_id)])
    gateway.poll_once()
    assert any("Human detected" in t for _, t in client.sent_text)
    _run_interview_to_completion(client, gateway, chat_id)

    script = store.get(chat_id).script
    assert script is not None and script.startswith("Crikey! "), (
        "the irwin persona must flavour the script tone"
    )
    assert client.sent_voices == [(chat_id, 11)]
    assert parts["scripter"].personas == [Persona.IRWIN]
    assert parts["narrator"].personas == [Persona.IRWIN], (
        "the chosen persona must reach the Narrator (voice selection)"
    )


def test_persona_survives_restart_into_a_second_run():
    client = GateClient()
    store = InterviewStateStore()
    gateway, parts = _gateway(client, store)
    chat_id = 202

    client.queue([_text_update(1, chat_id, "/persona irwin")])
    gateway.poll_once()
    client.queue([_photo_update(2, chat_id)])
    gateway.poll_once()
    _run_interview_to_completion(client, gateway, chat_id)
    assert parts["narrator"].personas == [Persona.IRWIN]

    # Restart purges the pipeline state but MUST keep the persona choice.
    client.queue([_text_update(200, chat_id, "/restart")])
    gateway.poll_once()
    assert (chat_id, INTERVIEWER_REPLY_RESET) in client.sent_text
    assert store.get(chat_id).phase is InterviewPhase.IDLE
    assert store.get(chat_id).script is None

    # A second run in the same process still narrates as irwin.
    client.queue([_photo_update(20, chat_id)])
    gateway.poll_once()
    _run_interview_to_completion(client, gateway, chat_id)
    assert parts["narrator"].personas == [Persona.IRWIN, Persona.IRWIN], (
        "the persona must persist across /restart into the second run"
    )


def test_two_chats_with_different_personas_stay_isolated():
    client = GateClient()
    store = InterviewStateStore()
    gateway, parts = _gateway(client, store)
    a, b = 303, 404

    client.queue([_text_update(1, a, "/persona irwin")])
    gateway.poll_once()
    client.queue([_text_update(2, b, "/persona attenborough")])
    gateway.poll_once()
    assert (b, PERSONA_REPLY_CONFIRMED[Persona.ATTENBOROUGH]) in client.sent_text

    # Both start interviews; A narrates as irwin, B as attenborough.
    client.queue([_photo_update(3, a), _photo_update(4, b)])
    gateway.poll_once()
    _run_interview_to_completion(client, gateway, a)
    _run_interview_to_completion(client, gateway, b)

    assert parts["scripter"].personas == [Persona.IRWIN, Persona.ATTENBOROUGH]
    assert parts["narrator"].personas == [Persona.IRWIN, Persona.ATTENBOROUGH], (
        "each chat must keep its own presenter"
    )


def test_unknown_command_mid_interview_is_answered_not_stored():
    client = GateClient()
    store = InterviewStateStore()
    gateway, parts = _gateway(client, store)
    chat_id = 505

    client.queue([_photo_update(1, chat_id)])
    gateway.poll_once()
    assert any("Human detected" in t for _, t in client.sent_text)

    # One real answer lands, then a bogus /command arrives mid-interview.
    client.queue([_text_update(10, chat_id, "answer 0")])
    gateway.poll_once()
    before = store.get(chat_id)
    assert before.answers == [(before.answers[0][0], "answer 0")]

    client.queue([_text_update(11, chat_id, "/pizza")])
    gateway.poll_once()
    assert (chat_id, GATEWAY_REPLY_UNKNOWN_COMMAND) in client.sent_text
    frozen = store.get(chat_id)
    assert frozen.answers == before.answers, "an unknown command must never become an answer"
    assert frozen.question_index == before.question_index, (
        "an unknown command must never advance the interview"
    )

    # The next genuine answer lands on the SAME pending question and the
    # interview continues normally to completion (position preserved, slot
    # untouched by the bogus command).
    client.queue([_text_update(12, chat_id, "answer 0 again")])
    gateway.poll_once()
    after = store.get(chat_id)
    assert len(after.answers) == len(frozen.answers) + 1
    assert after.answers[: len(frozen.answers)] == frozen.answers, (
        "the bogus command must leave every recorded answer untouched"
    )
    assert after.answers[-1][1] == "answer 0 again"
    assert after.question_index == frozen.question_index + 1, (
        "the retry answers exactly the question that was pending at /pizza"
    )

    # The interview still completes normally afterwards (no gap, no double).
    # Per the Phase 7 sequential-dispatch guarantee the completion sweep lands
    # on a later free step, so drive until the narrator fires (bounded).
    guard = 0
    while not parts["narrator"].personas and guard < 20:
        client.queue(
            [_text_update(200 + guard, chat_id, f"answer tail {guard}")]
        )
        gateway.poll_once()
        guard += 1
    assert parts["narrator"].personas == [Persona.ATTENBOROUGH]
    assert client.sent_voices == [(chat_id, 11)]