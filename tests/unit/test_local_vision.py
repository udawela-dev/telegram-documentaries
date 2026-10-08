"""Unit tests for the key-free local vision fallback (Phase 2 resilience).

These are REAL offline verdicts on committed fixtures — no network, no Gemini
key. They are the reproducible form of the negative + positive tests the user
asked for:

* ``non_human.jpg`` (Judean-mountains landscape)  → no face → **non-human**
* ``person.jpg`` (elderly Gambian woman, face visible) → face → **human**
"""
from pathlib import Path

import pytest

from src.local_vision import LocalVisionClassifier

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
PERSON_JPEG = (FIXTURES / "person.jpg").read_bytes()
NON_HUMAN_JPEG = (FIXTURES / "non_human.jpg").read_bytes()


@pytest.fixture(scope="module")
def classifier() -> LocalVisionClassifier:
    detector = LocalVisionClassifier()
    if not detector.available:  # Cascade XMLs missing → nothing offline to test
        pytest.skip("Haar cascades unavailable (opencv data missing)")
    return detector


# --- negative / positive verdicts (the user's two test cases) ------------------


def test_negative_landscape_photo_is_detected_non_human(classifier):
    """No face in the landscape → the gate must call it non-human."""
    assert classifier.human_present(NON_HUMAN_JPEG) is False


def test_positive_person_photo_is_detected_human(classifier):
    """A clearly visible human face → the gate must call it human."""
    assert classifier.human_present(PERSON_JPEG) is True


# --- boundary: undecodable input ------------------------------------------------


def test_undecodable_bytes_raise_instead_of_silent_verdict(classifier):
    with pytest.raises(ValueError, match="decode"):
        classifier.human_present(b"this is definitely not a jpeg")