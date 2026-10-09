"""Live Gemini verification for The Scripter (guarded; skipped offline).

Proves the real ``gemini-3.1-flash-lite`` path — one text turn on the completed
profile, returning a genuine British-wildlife-documentary paragraph. It only
runs when ``RUN_LIVE_GEMINI=1`` *and* a healthy key is present in ``.env``;
otherwise it stays SKIPPED so offline CI is never charged or flaked.

Built **without** a local writer, so a valid paragraph can only come from the
real model (a local fallback would be off-contract for this proof).

Run with:
    RUN_LIVE_GEMINI=1 python3 -m pytest tests/integration/test_live_scripter.py -v
"""
import os
import re

import pytest

from src.config import load_settings
from src.interview_state import InterviewStateStore, UserProfile
from src.scripter import SCRIPTER_MODEL, Scripter, validate_script

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_GEMINI") != "1",
    reason="set RUN_LIVE_GEMINI=1 to run the live Gemini narration tests",
)

# Dossier-fact grounding cues (case-insensitive). The spec's grounding contract
# is semantic — "Ground it in the suggested animal" (requirements.md) — and the
# locked prompt requirement (requirements.md L126) embeds the animal in the
# *prompt*, not the output. A valid paragraph may therefore be grounded in the
# subject's habits without repeating "the House Cat" verbatim. These cues are the
# *stated* dossier habits ("early riser", "hums while thinking", "long evening
# walks", "quiet corners") — phrases unlikely to appear as generic scene-setting,
# unlike bare "rises"/"walks"/"corners".
_GROUNDING_CUES = re.compile(
    r"\bearly riser\b"
    r"|\bevening walk(?:s)?\b"
    r"|\bquiet corner(?:s)?\b"
    r"|\bhum(?:s|ming|med)?\b",
    re.IGNORECASE,
)


@pytest.fixture(scope="module")
def live_scripter() -> tuple[Scripter, InterviewStateStore]:
    settings = load_settings()
    if not settings.gemini_api_key:
        pytest.fail("GEMINI_API_KEY missing from .env — cannot run live Scripter tests")
    store = InterviewStateStore()
    scripter = Scripter(
        api_key=settings.gemini_api_key,
        model=SCRIPTER_MODEL,
        store=store,
        local_writer=None,  # no fallback: this must be the real model's paragraph
    )
    return scripter, store


def _profile() -> UserProfile:
    return UserProfile(
        chat_id=99003,
        summary=(
            "Personality profile:\n"
            "Habits: early riser\n"
            "Quirks: hums while thinking\n"
            "Routines: long evening walks\n"
            "Preferences: quiet corners\n"
            "Suggested animal: the House Cat"
        ),
        suggested_animal="the House Cat",
    )


def test_real_flash_lite_returns_a_valid_grounded_paragraph(live_scripter) -> None:
    scripter, store = live_scripter
    profile = _profile()

    script = scripter.write_script(99003, profile)

    assert isinstance(script, str)
    assert "\n\n" not in script.strip()  # exactly one paragraph
    words = script.split()
    assert 60 <= len(words) <= 90
    # Dossier groundedness, not a literal animal name (the spec does not require
    # the output to repeat "the House Cat"): at least one habit/routine cue.
    assert _GROUNDING_CUES.search(script), (
        f"paragraph is not grounded in any dossier fact: {script!r}"
    )
    assert validate_script(script) == script  # passes the single shape gate
    assert store.get(99003).script == script  # stored for Phase 6
