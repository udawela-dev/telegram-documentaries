"""Live Gemini verification for presenter personas (guarded; skipped offline).

Proves the Phase 8 persona decision end-to-end against the real
``gemini-3.1-flash-tts-preview`` path: the ``irwin`` persona resolves to a
different prebuilt voice than ``attenborough`` (``NARRATOR_VOICE_IRWIN`` env,
default ``Charon`` — vs ``NARRATOR_VOICE`` default ``Orus``) and BOTH produce
a real Telegram-compatible OGG/Opus voice note that is byte-distinct.

Only runs when ``RUN_LIVE_GEMINI=1`` *and* a healthy key is present in
``.env``; otherwise it stays SKIPPED so offline CI is never charged or flaked.

Run with:
    RUN_LIVE_GEMINI=1 python3 -m pytest tests/integration/test_live_personas.py -v
"""
import os

import pytest

from src.config import load_settings
from src.interview_state import InterviewStateStore
from src.narrator import Narrator
from src.persona import Persona, resolve_persona_voice

SCRIPT = (
    "Here, in the golden light of early morning, our subject emerges. "
    "A creature of quiet routine and curious habits, it navigates the "
    "suburban savannah with a confidence born of long practice."
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_GEMINI") != "1",
    reason="set RUN_LIVE_GEMINI=1 to run the live Gemini persona tests",
)


@pytest.fixture(scope="module")
def live_narrator() -> tuple[Narrator, InterviewStateStore]:
    settings = load_settings()
    if not settings.gemini_api_key:
        pytest.fail("GEMINI_API_KEY missing from .env — cannot run live persona tests")
    store = InterviewStateStore()
    narrator = Narrator(api_key=settings.gemini_api_key, store=store)
    return narrator, store


def test_the_two_personas_resolve_to_different_prebuilt_voices() -> None:
    """The whole point of Phase 8: the presenter choice must change the voice."""
    attenborough_voice = resolve_persona_voice(Persona.ATTENBOROUGH)
    irwin_voice = resolve_persona_voice(Persona.IRWIN)
    assert attenborough_voice != irwin_voice, (
        "the irwin persona must use a different prebuilt voice than attenborough"
    )


def test_irwin_synthesizes_a_distinct_real_voice_note(live_narrator) -> None:
    narrator, store = live_narrator
    store.save(99041, store.get(99041).model_copy(update={"script": SCRIPT}))

    attenborough_audio = narrator.synthesize(
        99041, store.get(99041).script, Persona.ATTENBOROUGH
    )
    irwin_audio = narrator.synthesize(99041, store.get(99041).script, Persona.IRWIN)

    for audio in (attenborough_audio, irwin_audio):
        assert isinstance(audio, bytes)
        assert len(audio) > 1000  # a real voice note, not a stub
        assert audio[:4] == b"OggS", "Telegram sendVoice-compatible OGG/Opus"
    assert irwin_audio != attenborough_audio, (
        "the irwin voice note must be byte-distinct from the attenborough one"
    )