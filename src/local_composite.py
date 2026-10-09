"""Key-free local photo-booth composer (ROADMAP Phase 4).

While the real ``gemini-3.1-flash-image`` path is blocked (403) or otherwise
unreachable, The Converter falls back to this deterministic OpenCV composite so
the pipeline still emits a real hybrid image in Telegram today. A healthy key
later swaps in the real hybrid with no code change.

The composer locates the largest face via the existing YuNet detector
(:meth:`LocalVisionClassifier.largest_face_box`), then draws a photo-booth
animal overlay (ears / muzzle / whiskers, per archetype) on that face. No face
→ the locked default overlay is placed centrally. Identical input yields
byte-identical output; undecodable bytes raise loudly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np

from src.local_vision import LocalVisionClassifier, decode_image_bytes

logger = logging.getLogger(__name__)

# BGR colours (OpenCV order). Kept as plain tuples so the drawing is fully
# deterministic and independent of any external state.
_GREY = (180, 180, 180)
_PINK = (185, 135, 210)
_DARK = (40, 40, 40)
_LIGHT = (225, 225, 225)
_WHISKER = (70, 70, 70)


@dataclass(frozen=True)
class _Archetype:
    name: str
    ear_style: str  # "pointed" | "round" | "horn" | "tufted"
    fur: tuple[int, int, int]
    inner: tuple[int, int, int]
    muzzle: tuple[int, int, int]
    nose: tuple[int, int, int]
    eyes: str  # "round" | "large" | "slit"
    whiskers: bool
    stripes: bool
    snout: float  # vertical muzzle offset as a fraction of face height


# The locked default is cat-like (ears + muzzle + whiskers). Other archetypes
# are keyed on the Interviewer's suggested-animal strings.
_CAT = _Archetype("cat", "pointed", _GREY, _PINK, (215, 215, 215), (150, 150, 220), "round", True, True, 0.28)
_WOLF = _Archetype("wolf", "pointed", (120, 110, 95), (95, 85, 75), (165, 155, 140), _DARK, "slit", True, True, 0.34)
_GOAT = _Archetype("goat", "horn", (200, 200, 190), (175, 175, 165), (220, 220, 210), (105, 105, 105), "slit", False, False, 0.30)
_OWL = _Archetype("owl", "tufted", (95, 140, 165), (60, 95, 120), (170, 185, 120), (55, 120, 185), "large", False, False, 0.18)
_OTTER = _Archetype("otter", "round", (150, 120, 90), (115, 95, 75), (200, 190, 170), (75, 65, 55), "round", True, False, 0.30)

_ANIMAL_ARCHETYPES: tuple[tuple[str, _Archetype], ...] = (
    ("wolf", _WOLF),
    ("goat", _GOAT),
    ("owl", _OWL),
    ("otter", _OTTER),
    ("cat", _CAT),
)
_DEFAULT_ARCHETYPE = _CAT


def _archetype_for(animal: str) -> _Archetype:
    """Pick a deterministic archetype from the suggested-animal string."""
    text = (animal or "").lower()
    for keyword, archetype in _ANIMAL_ARCHETYPES:
        if keyword in text:
            return archetype
    return _DEFAULT_ARCHETYPE


class LocalHybridComposer:
    """Deterministic, key-free photo-booth animal composite (OpenCV)."""

    def __init__(self, detector: LocalVisionClassifier | None = None) -> None:
        self._detector = detector or LocalVisionClassifier()
        logger.info(
            "event=local_composite_ready detector_available=%s",
            self._detector.available,
        )

    def compose(self, portrait_bytes: bytes, animal: str) -> bytes:
        """Return deterministic JPEG bytes of the hybrid portrait.

        ``portrait_bytes`` must decode as an image; anything else raises
        ``ValueError`` loudly. A missing face falls back to the locked default
        overlay placement (never a crash), so the pipeline always gets bytes.
        """
        image = decode_image_bytes(portrait_bytes)  # decode exactly once
        box = self._locate_face(image)
        archetype = _archetype_for(animal)
        self._draw_overlay(image, box, archetype)

        ok, buffer = cv2.imencode(".jpg", image)
        if not ok:
            raise RuntimeError("failed to encode hybrid image as JPEG")
        payload = buffer.tobytes()
        logger.info(
            "event=local_composite_done archetype=%s bytes=%d",
            archetype.name,
            len(payload),
        )
        return payload

    def _locate_face(self, image: np.ndarray) -> tuple[int, int, int, int]:
        """Largest face box (on the already-decoded image), or a centred
        default box when no face is found."""
        try:
            box = self._detector.largest_face_box_from_image(image)
        except Exception as exc:  # detector unavailable → still compose
            logger.warning(
                "event=local_composite_face_detection_failed error_type=%s error=%.120r",
                type(exc).__name__,
                exc,
            )
            box = None
        if box is None or box[2] <= 0 or box[3] <= 0:
            height, width = image.shape[:2]
            box = (
                int(width * 0.25),
                int(height * 0.12),
                max(1, int(width * 0.5)),
                max(1, int(height * 0.5)),
            )
        return box

    def _draw_overlay(self, image: np.ndarray, box: tuple[int, int, int, int], arch: _Archetype) -> None:
        x, y, w, h = box
        cx = x + w // 2

        if arch.ear_style in ("pointed", "tufted"):
            self._draw_pointed_ears(image, x, y, w, h, arch)
        elif arch.ear_style == "horn":
            self._draw_horns(image, x, y, w, h, arch)
        else:
            self._draw_round_ears(image, x, y, w, h, arch)

        if arch.stripes:
            self._draw_stripes(image, x, y, w, h, arch)

        self._draw_eyes(image, cx, y, w, h, arch)
        self._draw_muzzle(image, cx, y, w, h, arch)

    def _draw_pointed_ears(self, image, x, y, w, h, arch) -> None:
        left = np.array(
            [
                [x + int(w * 0.06), y + int(h * 0.20)],
                [x + int(w * 0.30), y - int(h * 0.45)],
                [x + int(w * 0.50), y + int(h * 0.12)],
            ],
            dtype=np.int32,
        )
        right = np.array(
            [
                [x + int(w * 0.94), y + int(h * 0.20)],
                [x + int(w * 0.70), y - int(h * 0.45)],
                [x + int(w * 0.50), y + int(h * 0.12)],
            ],
            dtype=np.int32,
        )
        cv2.fillPoly(image, [left], arch.fur)
        cv2.fillPoly(image, [right], arch.fur)
        cv2.fillPoly(image, [_shrink(left, 0.55)], arch.inner)
        cv2.fillPoly(image, [_shrink(right, 0.55)], arch.inner)

    def _draw_round_ears(self, image, x, y, w, h, arch) -> None:
        radius = max(3, int(min(w, h) * 0.22))
        for ex in (x + int(w * 0.18), x + int(w * 0.82)):
            center = (ex, y + int(h * 0.08))
            cv2.circle(image, center, radius, arch.fur, -1)
            cv2.circle(image, center, max(1, int(radius * 0.5)), arch.inner, -1)

    def _draw_horns(self, image, x, y, w, h, arch) -> None:
        thickness = max(2, int(w * 0.06))
        for cx, start, end in (
            (x + int(w * 0.22), 190, 320),
            (x + int(w * 0.78), 220, 350),
        ):
            cv2.ellipse(
                image,
                (cx, y - int(h * 0.10)),
                (max(2, int(w * 0.16)), max(2, int(h * 0.42))),
                0,
                start,
                end,
                arch.fur,
                thickness,
            )

    def _draw_stripes(self, image, x, y, w, h, arch) -> None:
        thickness = max(2, int(h * 0.04))
        for offset in (-0.18, 0.0, 0.18):
            px = x + int(w * (0.5 + offset))
            cv2.line(
                image,
                (px, y + int(h * 0.06)),
                (px, y + int(h * 0.22)),
                arch.nose,
                thickness,
            )

    def _draw_eyes(self, image, cx, y, w, h, arch) -> None:
        eye_y = y + int(h * 0.40)
        large = arch.eyes == "large"
        white = (255, 255, 255)
        pupil = (30, 30, 30)
        for ex in (cx - int(w * 0.20), cx + int(w * 0.20)):
            if large:
                cv2.circle(image, (ex, eye_y), max(3, int(w * 0.14)), white, -1)
                cv2.circle(image, (ex, eye_y), max(2, int(w * 0.07)), pupil, -1)
            else:
                cv2.circle(image, (ex, eye_y), max(2, int(w * 0.07)), white, -1)
                cv2.circle(image, (ex, eye_y), max(1, int(w * 0.03)), pupil, -1)

    def _draw_muzzle(self, image, cx, y, w, h, arch) -> None:
        mug_x = max(3, int(w * 0.30))
        mug_y = max(3, int(h * 0.22))
        center = (cx, y + int(h * (0.55 + arch.snout)))
        cv2.ellipse(image, center, (mug_x, mug_y), 0, 0, 360, arch.muzzle, -1)
        nose_center = (center[0], center[1] - int(mug_y * 0.35))
        cv2.ellipse(
            image,
            nose_center,
            (max(2, int(mug_x * 0.22)), max(2, int(mug_y * 0.30))),
            0,
            0,
            360,
            arch.nose,
            -1,
        )
        if arch.whiskers:
            for dy in (-0.30, 0.0, 0.30):
                wy = center[1] + int(mug_y * dy)
                for direction in (-1, 1):
                    cv2.line(
                        image,
                        (center[0] + direction * int(mug_x * 0.2), wy),
                        (center[0] + direction * int(w * 0.62), wy - int(h * 0.05)),
                        _WHISKER,
                        1,
                    )


def _shrink(points: np.ndarray, factor: float) -> np.ndarray:
    """Scale a polygon toward its centroid (for inner-ear shapes)."""
    centroid = points.mean(axis=0)
    return (centroid + (points - centroid) * factor).astype(np.int32)
