"""The Narrator — TTS voice-note delivery (ROADMAP Phase 6).

Pipeline position: Telegram → Bouncer → Interviewer → Converter → Scripter →
**Narrator** (this module) → Telegram voice note.

The Narrator is deliberately **not** an agent and performs no reasoning step:
the Phase 5 script stored on the shared state is routed straight to Gemini's TTS
preview model through a single direct ``client.models.generate_content`` call
(``response_modalities=["AUDIO"]`` + a single-speaker speech config). The
returned audio is staged through a temp file, converted to OGG/Opus via the
ffmpeg CLI when the model returns any other format, and handed back to the
gateway for ``sendVoice``. Gemini TTS normally returns **headerless LINEAR16
PCM** (24 kHz mono s16le); the seam decides the source format from the response
mime and declares that raw input format to ffmpeg explicitly, because ffmpeg
cannot probe a headerless payload (``Invalid data found when processing
input``). Any other (non-RIFF, non-PCM) payload is staged opaquely so ffmpeg
probes its real container rather than force-decoding it as PCM.

There is **no local/key-free TTS fallback** (locked user decision): any failure —
no key, API error, timeout, malformed/empty response, or a needed ffmpeg that is
not installed — raises a loud :class:`NarratorError`, and the gateway sends the
locked ``NARRATOR_REPLY_UNAVAILABLE`` copy. Audio is never faked.

Temp files are always unlinked in a ``finally`` (success AND error), and the API
key is redacted from every error message and log line (SPECS/TECH.md: never log
secrets).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from typing import Any

from google.genai import types as genai_types

from src.logging_utils import redact

logger = logging.getLogger(__name__)

# Locked copy used by the gateway when the narrator cannot run (mirrors
# Converter/Scripter). Failure is loud and user-visible — never silent.
NARRATOR_REPLY_UNAVAILABLE = (
    "Hang on — the narrator lost his voice. Give that another go?"
)

# Locked model/timeout defaults (see SPECS/2026-10-09-narrator/requirements.md).
DEFAULT_NARRATOR_MODEL = "gemini-3.1-flash-tts-preview"
DEFAULT_NARRATOR_TIMEOUT = 60  # seconds (int; bounded via daemon thread + Event)

# Default prebuilt voice for the locked direction "deep male, posh British,
# classic British-wildlife-documentary presenter". "Orus" is documented with a
# firm, low-register timbre — the closest prebuilt fit. Overridable per
# deployment with ``NARRATOR_VOICE``.
DEFAULT_NARRATOR_VOICE = "Orus"

# Short style/persona instruction embedded in the request text. The prebuilt
# voice name carries the timbre; this reinforces the delivery/character.
NARRATOR_PERSONA = (
    "Narrate in a classic British wildlife documentary presenter voice: deep, "
    "male, posh British accent. Speak clearly and dramatically."
)

# MIME types our conversion seam treats as already sendVoice-compatible.
_OGG_MIME_TYPES = frozenset({"audio/ogg", "audio/opus", "application/ogg"})

# Known raw-audio config for Gemini's TTS preview models. When the model returns
# no container header, the payload is headerless LINEAR16 PCM. Google's own TTS
# sample code writes those bytes straight into a 24 kHz mono 16-bit WAV, which
# pins the format: signed 16-bit little-endian, 1 channel, 24000 Hz. ffmpeg
# cannot sniff headerless PCM, so the conversion seam must declare it explicitly
# (otherwise it aborts with "Invalid data found when processing input").
NARRATOR_SAMPLE_RATE_HZ = 24000
NARRATOR_CHANNELS = 1
NARRATOR_PCM_CODEC = "s16le"


def resolve_narrator_model() -> str:
    """Resolve the Narrator's Gemini TTS model at call time — env override wins."""
    return os.getenv("NARRATOR_MODEL", DEFAULT_NARRATOR_MODEL)


def resolve_narrator_voice() -> str:
    """Resolve the prebuilt voice name at call time — ``NARRATOR_VOICE`` wins."""
    return os.getenv("NARRATOR_VOICE", DEFAULT_NARRATOR_VOICE)


def resolve_narrator_timeout() -> int:
    """Resolve the bounded TTS timeout (seconds) at call time.

    A missing/blank/non-numeric/non-positive override falls back to the locked
    default with a loud warning rather than a silent zero-timeout.
    """
    raw = os.getenv("NARRATOR_GEMINI_TIMEOUT")
    if raw is None or not raw.strip():
        return DEFAULT_NARRATOR_TIMEOUT
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        logger.warning(
            "event=narrator_timeout_invalid value=%r using_default=%d",
            raw,
            DEFAULT_NARRATOR_TIMEOUT,
        )
        return DEFAULT_NARRATOR_TIMEOUT
    if value <= 0:
        logger.warning(
            "event=narrator_timeout_invalid value=%r using_default=%d",
            raw,
            DEFAULT_NARRATOR_TIMEOUT,
        )
        return DEFAULT_NARRATOR_TIMEOUT
    return value


class NarratorError(RuntimeError):
    """Raised when TTS cannot produce usable, Telegram-compatible audio."""


class Narrator:
    """Direct Gemini TTS caller + ffmpeg conversion seam (no agent, no fallback)."""

    def __init__(
        self,
        *,
        store=None,
        model: str | None = None,
        timeout: int | None = None,
        voice: str | None = None,
        api_key: str | None = None,
        client=None,
    ) -> None:
        # Make a settings-provided key visible to the genai client without ever
        # hardcoding it (mirrors Bouncer/Converter/Scripter).
        if api_key:
            os.environ.setdefault("GEMINI_API_KEY", api_key)
        # Resolved at construction time so a runtime env override is honoured.
        self._model = model or resolve_narrator_model()
        self._voice = voice or resolve_narrator_voice()
        # An explicit constructor timeout pins the value; otherwise the env is
        # re-read at call time (house pattern) so a running deployment's
        # NARRATOR_GEMINI_TIMEOUT override actually applies.
        self._timeout_override = timeout
        # Kept for parity with the other stages; audio itself is transient and
        # never stored on the driver (Phase 6 adds no state fields).
        self._store = store
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY")
        # Inject honouring a test/production-supplied genai client. When absent
        # the client is built lazily on the first real TTS call.
        self._client = client

    @property
    def model(self) -> str:
        return self._model

    @property
    def voice(self) -> str:
        return self._voice

    def _resolve_timeout(self) -> int:
        """The effective TTS timeout: explicit override, else the live env."""
        if self._timeout_override is not None:
            return self._timeout_override
        return resolve_narrator_timeout()

    @property
    def timeout(self) -> int:
        return self._resolve_timeout()

    # --- direct Gemini TTS call -------------------------------------------------

    def _get_client(self):
        """Return the genai client, building it lazily; no key → loud failure."""
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise NarratorError("narrator unavailable: no GEMINI_API_KEY configured")
        from google import genai

        self._client = genai.Client(api_key=self._api_key)
        return self._client

    def _request_text(self, script: str) -> str:
        """Persona/style instruction followed by the raw Phase 5 script."""
        return f"{NARRATOR_PERSONA}\n\n{script}"

    def _build_config(self) -> genai_types.GenerateContentConfig:
        """The locked single-speaker AUDIO request config."""
        return genai_types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=genai_types.SpeechConfig(
                voice_config=genai_types.VoiceConfig(
                    prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                        voice_name=self._voice
                    )
                )
            ),
        )

    @staticmethod
    def _extract_audio(response) -> tuple[bytes, str | None]:
        """Return the first audio ``inline_data`` (bytes) and its mime type.

        A response with no audio part is a failure to produce audio — never a
        silent empty success (typed-boundary rule, SPECS/TECH.md).
        """
        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", None) or []
            for part in parts:
                inline = getattr(part, "inline_data", None)
                data = getattr(inline, "data", None) if inline is not None else None
                if data:
                    mime = getattr(inline, "mime_type", None)
                    return bytes(data), mime
        raise NarratorError("narrator model response contained no audio data")

    def _run_tts(self, script: str) -> tuple[bytes, str | None]:
        """Perform the direct Gemini TTS call and return ``(audio, mime)``.

        Scriptable seam for offline tests (no network).
        """
        client = self._get_client()
        response = client.models.generate_content(
            model=self._model,
            contents=[self._request_text(script)],
            config=self._build_config(),
        )
        return self._extract_audio(response)

    def _run_tts_bounded(self, script: str) -> tuple[bytes, str | None]:
        """Run the TTS call with a hard timeout (house daemon+Event pattern).

        A hung Gemini request must not freeze the bot: past
        ``NARRATOR_GEMINI_TIMEOUT`` the orphaned worker is abandoned and a
        :class:`NarratorError` is raised (there is no local fallback).
        """
        timeout = float(self.timeout)
        done = threading.Event()
        outcome: dict[str, Any] = {}

        def _run() -> None:
            try:
                outcome["value"] = self._run_tts(script)
            except BaseException as exc:  # noqa: BLE001 — re-raised verbatim below
                outcome["error"] = exc
            finally:
                done.set()

        worker = threading.Thread(target=_run, name="narrator-tts", daemon=True)
        worker.start()
        if not done.wait(timeout=timeout):
            raise NarratorError(
                f"narrator TTS call exceeded {timeout:.0f}s"
            ) from None
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]

    # --- temp-file staging + ffmpeg conversion ----------------------------------

    @staticmethod
    def _temp_path(prefix: str, registry: list[str], suffix: str = ".tmp") -> str:
        """Create a real temp file, register it for ``finally`` cleanup.

        ``suffix`` matters on both ends of the conversion: ffmpeg infers the
        output container from the target extension (the target is created with
        ``.ogg``), and it uses the source extension as a probe hint (``.wav`` for
        RIFF/WAVE, ``.pcm`` for the TTS model's headerless LINEAR16; see
        :meth:`_source_format`). An opaque ``.tmp`` source was one reason the
        real conversion path failed (B2).
        """
        fd, path = tempfile.mkstemp(
            prefix=f"{prefix}-", suffix=suffix, dir=tempfile.gettempdir()
        )
        os.close(fd)
        registry.append(path)
        return path

    @staticmethod
    def _needs_conversion(audio: bytes, mime: str | None) -> bool:
        """True unless the audio already looks like a sendVoice-ready OGG/Opus."""
        # NOTE (NB2): the OggS magic identifies the *Ogg* container, not the
        # codec — an Ogg/Vorbis stream would also match. Telegram voice notes
        # require Opus, but the model returns either OGG/Opus or a raw PCM/WAV
        # payload; the pipeline converts anything that is not OggS and does not
        # over-engineer an OpusHead page parse here.
        if audio[:4] == b"OggS":
            return False
        if mime:
            normalized = mime.split(";")[0].strip().lower()
            if normalized in _OGG_MIME_TYPES:
                return False
        return True

    @staticmethod
    def _is_wav(audio: bytes) -> bool:
        """True for a self-describing RIFF/WAVE payload ffmpeg can probe."""
        return audio[:4] == b"RIFF" and audio[8:12] == b"WAVE"

    @staticmethod
    def _is_linear_pcm_mime(mime: str | None) -> bool:
        """True when the response mime identifies headerless LINEAR16 PCM.

        Gemini TTS advertises its raw payload as ``audio/L16``/``audio/LPCM`` or
        with ``codec=pcm``. Only those need the explicit demuxer declaration; an
        unknown/other format must be probed by ffmpeg instead of being
        force-decoded as PCM (which would emit silent noise).
        """
        if not mime:
            return False
        normalized = mime.lower()
        return "l16" in normalized or "lpcm" in normalized or "codec=pcm" in normalized

    @staticmethod
    def _pcm_sample_rate(mime: str | None) -> int:
        """The ``rate=`` (Hz) a linear-PCM mime declares, else the 24 kHz default."""
        if mime:
            match = re.search(r"rate=(\d+)", mime.lower())
            if match:
                return int(match.group(1))
        return NARRATOR_SAMPLE_RATE_HZ

    @classmethod
    def _source_format(
        cls, audio: bytes, mime: str | None = None
    ) -> tuple[str, list[str]]:
        """Return ``(suffix, ffmpeg input args)`` for staging a TTS payload.

        * A self-describing RIFF/WAVE payload → ``.wav`` suffix, no declaration
          (ffmpeg probes it).
        * Headerless LINEAR16 PCM — identified from the response *mime*
          (``audio/L16``/``audio/LPCM``/``codec=pcm``), honouring any declared
          ``rate=`` — → ``.pcm`` suffix plus an explicit ``s16le``/rate/mono
          declaration, because ffmpeg cannot probe a headerless payload.
        * Anything else (e.g. a future MP3/FLAC response) → opaque ``.tmp`` with
          no declared format, so ffmpeg probes by content. If it cannot, the
          conversion fails loudly rather than force-decoding the bytes as PCM.
        """
        if cls._is_wav(audio):
            return ".wav", []
        if cls._is_linear_pcm_mime(mime):
            return (
                ".pcm",
                [
                    "-f",
                    NARRATOR_PCM_CODEC,
                    "-ar",
                    str(cls._pcm_sample_rate(mime)),
                    "-ac",
                    str(NARRATOR_CHANNELS),
                ],
            )
        return ".tmp", []

    def _convert_to_ogg(
        self,
        input_path: str,
        output_path: str,
        *,
        input_args: list[str] | None = None,
    ) -> None:
        """Convert an arbitrary audio temp file to OGG/Opus via the ffmpeg CLI.

        ``input_args`` carries an explicit input demuxer/format declaration
        (``-f s16le -ar 24000 -ac 1`` for headerless LINEAR16 PCM); when omitted
        ffmpeg probes the container from the file body. Scriptable seam for
        offline tests. Missing ffmpeg when conversion is needed raises
        :class:`NarratorError` — the gateway degrades to the locked unavailable
        copy, and nothing is ever silently skipped.
        """
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise NarratorError(
                "ffmpeg is required to convert narrator audio to OGG/Opus but "
                "was not found on PATH"
            )
        cmd = [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
        ]
        # Declare the input format when the payload is headerless (raw PCM); a
        # self-describing container passes no hint and ffmpeg probes it.
        cmd += list(input_args or [])
        cmd += [
            "-i",
            input_path,
            "-c:a",
            "libopus",
            "-b:a",
            "64k",
            "-ar",
            "16000",
            "-ac",
            "1",
            # Force the output container explicitly: ffmpeg otherwise infers it
            # from the extension, and the temp target is opaque (B1).
            "-f",
            "ogg",
            output_path,
        ]
        logger.info(
            "event=narrator_conversion_started input_declared=%s",
            " ".join(input_args) if input_args else "auto",
        )
        try:
            completed = subprocess.run(
                cmd, capture_output=True, timeout=float(self.timeout), check=False
            )
        except FileNotFoundError as exc:
            raise NarratorError(
                "ffmpeg executable disappeared before the narrator conversion"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise NarratorError("narrator audio conversion timed out") from exc
        if completed.returncode != 0:
            stderr = (completed.stderr or b"")[-300:].decode(errors="replace")
            raise NarratorError(
                f"ffmpeg conversion failed (exit {completed.returncode}): {stderr}"
            )
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise NarratorError("ffmpeg conversion produced no OGG/Opus output")

    def _sanitized_error(self, exc: BaseException) -> NarratorError:
        """Wrap a raw TTS failure with the API key redacted from message + log."""
        detail = redact(f"{type(exc).__name__}: {exc}", self._api_key or "")
        logger.error("event=narrator_tts_failed error=%s", detail)
        return NarratorError(f"narrator TTS failed: {detail}")

    @staticmethod
    def _cleanup_temp_paths(paths: list[str]) -> None:
        """Unlink every staged temp path; a cleanup failure is logged, not hidden."""
        for path in paths:
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue
            except OSError:
                logger.exception("event=narrator_temp_cleanup_failed path=%s", path)

    # --- public API -------------------------------------------------------------

    def synthesize(self, chat_id: int, script: str) -> bytes:
        """Render ``script`` to Telegram-compatible audio bytes for ``chat_id``.

        The audio is staged through temp file(s), converted to OGG/Opus when
        needed, and the temp files are always unlinked in a ``finally`` (success
        AND error). Any failure raises :class:`NarratorError`; there is no local
        fallback and no fake audio.
        """
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            raise NarratorError(f"chat_id must be int, got {type(chat_id).__name__}")
        if not isinstance(script, str) or not script.strip():
            raise NarratorError(
                f"narrator requires a non-empty script for chat {chat_id}"
            )

        temp_paths: list[str] = []
        try:
            try:
                audio, mime = self._run_tts_bounded(script)
            except NarratorError:
                raise
            except Exception as exc:  # noqa: BLE001 — sanitized + re-raised loud
                raise self._sanitized_error(exc) from None

            if not audio:
                raise NarratorError(
                    f"narrator model returned empty audio for chat {chat_id}"
                )

            if self._needs_conversion(audio, mime):
                # Decide the source suffix/demuxer from the response mime:
                # ``.wav`` (self-describing), ``.pcm`` (declared LINEAR16), or an
                # opaque ``.tmp`` that ffmpeg probes by content.
                suffix, input_args = self._source_format(audio, mime)
                source = self._temp_path("narrator-source", temp_paths, suffix=suffix)
                # ``.ogg`` so ffmpeg can also infer the container from the name;
                # the explicit ``-f ogg`` is the belt-and-braces (B1).
                target = self._temp_path("narrator-ogg", temp_paths, suffix=".ogg")
                with open(source, "wb") as handle:
                    handle.write(audio)
                self._convert_to_ogg(source, target, input_args=input_args)
                if not os.path.exists(target) or os.path.getsize(target) == 0:
                    raise NarratorError("narrator conversion produced no audio")
                with open(target, "rb") as handle:
                    final = handle.read()
            else:
                staged = self._temp_path("narrator-audio", temp_paths)
                with open(staged, "wb") as handle:
                    handle.write(audio)
                with open(staged, "rb") as handle:
                    final = handle.read()

            if not final:
                raise NarratorError(
                    f"narrator produced empty audio for chat {chat_id}"
                )
            logger.info(
                "event=narrator_synthesized chat_id=%s bytes=%d", chat_id, len(final)
            )
            return final
        finally:
            self._cleanup_temp_paths(temp_paths)
