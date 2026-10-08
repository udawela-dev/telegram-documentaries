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
        image = self._cv2.imdecode(
            np.frombuffer(image_bytes, dtype=np.uint8), self._cv2.IMREAD_COLOR
        )
        if image is None:
            raise ValueError("unable to decode image bytes as a picture")
        count = self._detect(image, upscale=1)
        if count == 0:
            # Small faces (distant / wide shots) are caught by a 2x plate.
            count = self._detect(image, upscale=2)
        logger.info("event=local_vision_verdict faces=%d", count)
        return count > 0

    def _detect(self, image: np.ndarray, *, upscale: int) -> int:
        """Run YuNet once at native or upscaled resolution; return face count."""
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
        return 0 if faces is None else int(len(faces))