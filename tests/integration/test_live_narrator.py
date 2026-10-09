"""Live Gemini verification for The Narrator (guarded; skipped offline).

Proves the real ``gemini-3.1-flash-tts-preview`` path — a **direct** Gemini API
TTS call (no agent, no local fallback) turning the Phase 5 stored script into a
Telegram-compatible voice note. It only runs when ``RUN_LIVE_GEMINI=1`` *and* a
healthy key is present in ``.env``; otherwise it stays SKIPPED so offline CI is
never charged or flaked.

The conversion seam (``src/narrator.py``) stages Gemini TTS's headerless
LINEAR16 PCM payload (24 kHz mono s16le — ``audio/L16;codec=pcm;rate=24000``)
as a ``.pcm`` source with the raw input format declared to ffmpeg explicitly,
then hands back OGG/Opus bytes; the ``OggS`` assertion below locks that output
contract end-to-end.

The Narrator is built **without** any fallback: a real voice note can only come
from the real model.

Run with:
    RUN_LIVE_GEMINI=1 python3 -m pytest tests/integration/test_live_narrator.py -v
"""
import os

import pytest

from src.config import load_settings
from src.interview_state import InterviewStateStore
from src.narrator import Narrator

SCRIPT = (
    "Here, in the golden light of early morning, our subject emerges. "
    "A creature of quiet routine and curious habits, it navigates the "
    "suburban savannah with a confidence born of long practice."
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_GEMINI") != "1",
    reason="set RUN_LIVE_GEMINI=1 to run the live Gemini narration tests",
)


@pytest.fixture(scope="module")
def live_narrator() -> tuple[Narrator, InterviewStateStore]:
    settings = load_settings()
    if not settings.gemini_api_key:
        pytest.fail("GEMINI_API_KEY missing from .env — cannot run live Narrator tests")
    store = InterviewStateStore()
    narrator = Narrator(api_key=settings.gemini_api_key, store=store)
    return narrator, store


def test_real_tts_returns_a_telegram_compatible_voice_note(live_narrator) -> None:
    narrator, store = live_narrator
    store.save(99004, store.get(99004).model_copy(update={"script": SCRIPT}))

    audio = narrator.synthesize(99004, store.get(99004).script)

    assert isinstance(audio, bytes)
    assert len(audio) > 1000  # a real voice note, not a stub
    # Telegram sendVoice-compatible: our contract targets OGG/Opus (the
    # conversion seam runs whenever the model returns anything else).
    assert audio[:4] == b"OggS"
