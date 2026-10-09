"""Unit tests for the key-free local vision fallback (Phase 2 resilience).

These are REAL offline verdicts on committed fixtures — no network, no Gemini
key. They are the reproducible form of the negative + positive tests the user
asked for:

* ``non_human.jpg`` (Judean-mountains landscape)  → no face → **non-human**
* ``person.jpg`` (elderly Gambian woman, face visible) → face → **human**
"""
from pathlib import Path

import cv2
import numpy as np
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


# --- largest_face_box (Phase 4 — the Converter's face locator) ------------------


def test_largest_face_box_returns_a_box_inside_the_person_image(classifier):
    """The person photo has a face → a positive box in original coordinates."""
    image = cv2.imdecode(np.frombuffer(PERSON_JPEG, dtype=np.uint8), cv2.IMREAD_COLOR)
    height, width = image.shape[:2]

    box = classifier.largest_face_box(PERSON_JPEG)

    assert box is not None
    x, y, w, h = box
    assert w > 0 and h > 0
    assert x >= 0 and y >= 0
    assert x + w <= width
    assert y + h <= height


def test_largest_face_box_returns_none_on_the_landscape_fixture(classifier):
    assert classifier.largest_face_box(NON_HUMAN_JPEG) is None


def test_largest_face_box_raises_on_undecodable_input(classifier):
    with pytest.raises(ValueError, match="decode"):
        classifier.largest_face_box(b"definitely not an image")


def test_largest_face_box_scales_back_a_two_x_only_detection(classifier):
    """A face only caught by the 2x rescue pass must be reported in ORIGINAL
    image coordinates (the coordinates are halved back)."""

    class TwoXOnlyClassifier(LocalVisionClassifier):
        def __init__(self) -> None:
            super().__init__()
            self.upscales: list[int] = []

        def _detect(self, image, *, upscale: int):  # noqa: ANN001
            self.upscales.append(upscale)
            if upscale == 2:
                # x, y, w, h in the 2x image + 11 trailing columns (landmarks/score)
                return np.array([[100.0, 50.0, 80.0, 100.0] + [0.0] * 11], dtype=np.float32)
            return None

    detector = TwoXOnlyClassifier()
    if not detector.available:
        pytest.skip("face model unavailable")

    box = detector.largest_face_box(PERSON_JPEG)

    assert detector.upscales == [1, 2]
    assert box == (50, 25, 40, 50)