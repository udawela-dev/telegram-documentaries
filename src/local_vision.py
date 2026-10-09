"""Key-free local vision fallback: YuNet deep face detection (OpenCV).

The Bouncer's primary judge is the ADK/Gemini agent. When that agent cannot
be reached — missing/blocked API key, network failure, or a request that
exceeds ``BOUNCER_GEMINI_TIMEOUT`` — the gateway falls back to this local
detector so users still get a real **human / non-human** verdict with zero
API key, zero network, zero cost.

The detector is **YuNet** (OpenCV zoo, Apache-2.0, bundled in ``src/data/``),
a tiny neural face detector chosen for robustness across skin tones and
lighting — classic Haar cascades miss too many real faces (e.g. dark-skinned
or side-lit portraits) to be a dependable fallback.

Honest limits: it detects *faces*, not "a clearly discernible human body".
A visible face → human; no detectable face (objects, animals, landscapes,
hidden faces) → non-human. This is a fallback, strictly less smart than
Gemini, and is never consulted when Gemini answers.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import cv2
import numpy as np

from src.logging_utils import log_call

logger = logging.getLogger(__name__)

# Bundled YuNet model (Apache-2.0, OpenCV zoo) — see src/data/ATTRIBUTION.md.
DEFAULT_FACE_MODEL = str(Path(__file__).resolve().parent / "data" / "face_detection_yunet_2023mar.onnx")
# Deployment override for environments that must vendor the model elsewhere.
FACE_MODEL = os.getenv("BOUNCER_FACE_MODEL", DEFAULT_FACE_MODEL)
# Detection confidence floor (see the fixture sweep: 0.6 misses small faces,
# 0.3 catches them with zero false positives on the landscape fixture).
DEFAULT_SCORE_THRESHOLD = 0.3
SCORE_THRESHOLD = float(os.getenv("BOUNCER_YUNET_THRESHOLD", str(DEFAULT_SCORE_THRESHOLD)))


def decode_image_bytes(image_bytes: bytes) -> np.ndarray:
    """Decode untrusted bytes into a BGR image; undecodable/empty → loud error.

    Shared by the Bouncer's face detection and the Converter's local composer
    so the codebase has exactly one decoding boundary for uploaded photos.
    """
    if not image_bytes:
        raise ValueError("unable to decode empty image bytes as a picture")
    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("unable to decode image bytes as a picture")
    return image


class LocalVisionClassifier:
    """Key-free human/non-human verdict via YuNet (in-process, no network)."""

    def __init__(self) -> None:
        self._cv2 = cv2
        self._probe = cv2.FaceDetectorYN.create(
            FACE_MODEL, "", (320, 320), score_threshold=SCORE_THRESHOLD
        )
        self.available = self._probe is not None
        if self.available:
            logger.info("event=local_vision_ready model=yuNet threshold=%.2f", SCORE_THRESHOLD)
        else:
            logger.warning("event=local_vision_unavailable model=%s", FACE_MODEL)

    @log_call(event="local_vision_classify")
    def human_present(self, image_bytes: bytes) -> bool:
        """True iff at least one face is found. Raises on undecodable input."""
        if not self.available:
            raise RuntimeError("local vision unavailable: face model did not load")
        image = decode_image_bytes(image_bytes)
        faces = self._detect(image, upscale=1)
        if faces is None or len(faces) == 0:
            # Small faces (distant / wide shots) are caught by a 2x plate.
            faces = self._detect(image, upscale=2)
        count = 0 if faces is None else int(len(faces))
        logger.info("event=local_vision_verdict faces=%d", count)
        return count > 0

    @log_call(event="local_vision_largest_face_box")
    def largest_face_box(self, image_bytes: bytes) -> tuple[int, int, int, int] | None:
        """Largest detected face as ``(x, y, w, h)`` in ORIGINAL-image coords.

        Reuses the same YuNet detector and threshold as ``human_present`` — a
        native pass first, then the 2x rescue pass for small faces. Any box
        found in the 2x image is scaled back to original coordinates. Returns
        ``None`` when no face is detected; raises on undecodable input.
        """
        if not self.available:
            raise RuntimeError("local vision unavailable: face model did not load")
        return self.largest_face_box_from_image(decode_image_bytes(image_bytes))

    @log_call(event="local_vision_largest_face_box")
    def largest_face_box_from_image(self, image: np.ndarray) -> tuple[int, int, int, int] | None:
        """Largest YuNet face box ``(x, y, w, h)`` in this image's coordinates.

        Works on an already-decoded image (the Composer decodes once); a 2×
        rescue pass catches small faces and its box is scaled back so the
        coordinates always match the caller's image.
        """
        if not self.available:
            raise RuntimeError("local vision unavailable: face model did not load")
        faces = self._detect(image, upscale=1)
        scale = 1
        if faces is None or len(faces) == 0:
            faces = self._detect(image, upscale=2)
            scale = 2
        if faces is None or len(faces) == 0:
            logger.info("event=local_vision_no_face")
            return None

        # Each row: x, y, w, h, landmarks…, score. Largest by area wins.
        largest = max(faces, key=lambda row: float(row[2]) * float(row[3]))
        x, y, w, h = (float(largest[index]) for index in range(4))
        if scale != 1:
            x, y, w, h = x / scale, y / scale, w / scale, h / scale
        box = (int(round(x)), int(round(y)), int(round(w)), int(round(h)))
        logger.info(
            "event=local_vision_face_box x=%d y=%d w=%d h=%d upscale=%d",
            *box,
            scale,
        )
        return box

    def _detect(self, image: np.ndarray, *, upscale: int):
        """Run YuNet once at native or upscaled resolution.

        Returns the raw ``(N, 15)`` faces array (or ``None`` when nothing is
        found) so callers can count faces or locate the largest box.
        """
        if upscale != 1:
            image = self._cv2.resize(
                image, (image.shape[1] * upscale, image.shape[0] * upscale)
            )
        height, width = image.shape[:2]
        detector = self._cv2.FaceDetectorYN.create(
            FACE_MODEL,
            "",
            (width, height),
            score_threshold=SCORE_THRESHOLD,
        )
        _, faces = detector.detect(image)
        return faces
