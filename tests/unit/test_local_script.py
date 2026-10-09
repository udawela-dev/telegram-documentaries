"""Unit tests for the key-free local script writer (Phase 5 — RED first).

The local writer is the deterministic, key-free fallback used while the real
``gemini-3.1-flash-lite`` path is blocked/unreachable. Its output must satisfy
the same shape contract as the LLM path (one paragraph, 60–90 words, no
markdown) so it can be the single fallback for the Scripter.
"""
import pytest

from src.interview_state import UserProfile
from src.interviewer import ANIMAL_KEYWORD_FAMILIES, DEFAULT_SUGGESTED_ANIMAL
from src.local_script import LocalScriptWriter
from src.scripter import SCRIPTER_MAX_WORDS, SCRIPTER_MIN_WORDS, validate_script

# Every canonical animal the Interviewer can produce, plus the locked default.
CANONICAL_ANIMALS = [animal for animal, _keywords in ANIMAL_KEYWORD_FAMILIES] + [
    DEFAULT_SUGGESTED_ANIMAL
]


def _profile(animal: str = DEFAULT_SUGGESTED_ANIMAL) -> UserProfile:
    return UserProfile(
        chat_id=7,
        summary=(
            "Personality profile:\n"
            "Habits: early coffee\n"
            "Quirks: talks to plants\n"
            "Routines: evening walk\n"
            "Preferences: quiet corners\n"
            f"Suggested animal: {animal}"
        ),
        suggested_animal=animal,
    )


# --- determinism ----------------------------------------------------------------


def test_write_is_deterministic_for_an_identical_profile():
    writer = LocalScriptWriter()
    profile = _profile("the Night Owl")

    assert writer.write(profile) == writer.write(profile)


def test_write_is_deterministic_across_writer_instances():
    profile = _profile("the Sea Otter")

    assert LocalScriptWriter().write(profile) == LocalScriptWriter().write(profile)


# --- shape contract for every canonical animal ----------------------------------


@pytest.mark.parametrize("animal", CANONICAL_ANIMALS)
def test_write_produces_one_valid_paragraph_grounded_in_the_animal(animal):
    script = LocalScriptWriter().write(_profile(animal))

    assert isinstance(script, str)
    assert "\n" not in script  # exactly one paragraph, no line breaks
    words = script.split()
    assert SCRIPTER_MIN_WORDS <= len(words) <= SCRIPTER_MAX_WORDS
    assert animal in script  # grounded in the suggested animal
    # The same single shape gate the LLM path must pass accepts this output.
    assert validate_script(script) is not None


def test_write_with_the_locked_default_animal_is_valid():
    script = LocalScriptWriter().write(_profile(DEFAULT_SUGGESTED_ANIMAL))

    assert DEFAULT_SUGGESTED_ANIMAL in script
    assert validate_script(script) is not None
