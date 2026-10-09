"""Unit tests for The Narrator (Phase 6 — RED first).

The Narrator is a **direct** Gemini TTS call (``client.models.generate_content``
on ``gemini-3.1-flash-tts-preview``) — explicitly *not* an agent, and with **no
local/key-free fallback**. Offline strategy mirrors ``test_scripter.py``: the
TTS step (``_run_tts``) and the ffmpeg conversion seam (``_convert_to_ogg``) are
scripted, so no network (and no ffmpeg) is required.

The real behaviour under test: the direct-call request shape (model, AUDIO
modality, single-speaker speech config, persona-prefixed text), audio
extraction from the response parts, the bounded daemon-thread timeout, the temp
file write/read/unlink lifecycle (success AND error), the ffmpeg conversion
seam (and its loud failure when ffmpeg is missing), the locked copy/constants
and the token-redaction contract.
"""
import array
import io
import logging
import math
import os
import shutil
import subprocess
import tempfile
import time
import wave

import pytest
from google.genai import types as genai_types

import src.narrator as narrator_module
from src.narrator import (
    DEFAULT_NARRATOR_MODEL,
    DEFAULT_NARRATOR_TIMEOUT,
    DEFAULT_NARRATOR_VOICE,
    NARRATOR_CHANNELS,
    NARRATOR_PCM_CODEC,
    NARRATOR_PERSONA,
    NARRATOR_REPLY_UNAVAILABLE,
    NARRATOR_SAMPLE_RATE_HZ,
    Narrator,
    NarratorError,
    resolve_narrator_model,
    resolve_narrator_timeout,
    resolve_narrator_voice,
)
from src.persona import (
    PERSONA_NARRATOR_INSTRUCTION,
    PERSONA_NARRATOR_VOICE,
    Persona,
    resolve_persona_voice,
)

SCRIPT = "The sun rises over the savannah, and our subject stirs."

# Minimal magic-byte samples so format sniffing is exercised for real.
OGG_BYTES = b"OggS\x00\x02\x00\x00opus-audio-payload"
WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt audio-payload"
# Headerless raw LINEAR16 PCM looks like this in the first bytes — no magic at
# all, which is exactly why ffmpeg needs the demuxer declared explicitly.
PCM_HEAD = b"\xe1\x3f\x2c\x40\x00\x00\x11\x40"

# Real-ffmpeg tests stay CI-safe: skipped when the binary is not installed.
REQUIRES_FFMPEG = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg not installed — real Ogg/Opus conversion tests skipped",
)


def _sample_pcm_frames(
    duration_seconds: float = 0.25, sample_rate: int = 24000
) -> bytes:
    """Deterministic mono 16-bit LE sine frames (a real headerless PCM payload)."""
    count = int(duration_seconds * sample_rate)
    frames = array.array("h")
    for i in range(count):
        frames.append(int(4000 * math.sin(2 * math.pi * 440 * i / sample_rate)))
    return frames.tobytes()


def _sample_wav_bytes() -> bytes:
    """A real, ffmpeg-sniffable WAV file wrapping ``_sample_pcm_frames``."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(_sample_pcm_frames())
    return buffer.getvalue()


# --- scriptable doubles ---------------------------------------------------------


class ScriptedNarrator(Narrator):
    """Narrator whose TTS seam returns preset ``(bytes, mime)`` — no network."""

    def __init__(self, audio: bytes = OGG_BYTES, mime: str | None = "audio/ogg", **kwargs) -> None:
        super().__init__(**kwargs)
        self.audio = audio
        self.mime = mime
        self.tts_calls: list[str] = []

    def _run_tts(self, script: str) -> tuple[bytes, str | None]:
        self.tts_calls.append(script)
        return self.audio, self.mime


class FailingNarrator(Narrator):
    """Narrator whose TTS seam always fails — models a blocked/unreachable key."""

    def _run_tts(self, script: str) -> tuple[bytes, str | None]:
        raise NarratorError("403 PERMISSION_DENIED: the API key is flagged")


class HangingNarrator(Narrator):
    """Narrator whose TTS seam outlives the configured timeout."""

    def _run_tts(self, script: str) -> tuple[bytes, str | None]:
        time.sleep(1.0)
        return OGG_BYTES, "audio/ogg"


class PersonaRecordingNarrator(ScriptedNarrator):
    """Records the voice id + request text the seam actually used for a run."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.seen_voices: list[str] = []
        self.seen_requests: list[str] = []

    def _run_tts(self, script: str) -> tuple[bytes, str | None]:
        self.seen_requests.append(self._request_text(script))
        config = self._build_config()
        self.seen_voices.append(
            config.speech_config.voice_config.prebuilt_voice_config.voice_name
        )
        return self.audio, self.mime


# --- fakes for the direct genai call --------------------------------------------


class FakeModels:
    def __init__(self, response) -> None:
        self._response = response
        self.kwargs: dict | None = None

    def generate_content(self, **kwargs):
        self.kwargs = kwargs
        return self._response


class FakeClient:
    def __init__(self, response) -> None:
        self.models = FakeModels(response)


def _response_with_audio(data: bytes, mime: str = "audio/ogg"):
    blob = genai_types.Blob(data=data, mime_type=mime)
    part = genai_types.Part(inline_data=blob)
    content = genai_types.Content(parts=[part])
    return genai_types.GenerateContentResponse(
        candidates=[genai_types.Candidate(content=content)]
    )


def _response_with_text_only():
    content = genai_types.Content(parts=[genai_types.Part(text="no audio here")])
    return genai_types.GenerateContentResponse(
        candidates=[genai_types.Candidate(content=content)]
    )


def _spy_tempfiles(monkeypatch) -> list[str]:
    """Record every temp path the Narrator creates (real temp files still made)."""
    created: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def spy_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        created.append(path)
        return fd, path

    monkeypatch.setattr(narrator_module.tempfile, "mkstemp", spy_mkstemp)
    return created


# --- direct-call request shape --------------------------------------------------


def test_run_tts_builds_the_locked_direct_gemini_audio_call():
    client = FakeClient(_response_with_audio(OGG_BYTES))
    narrator = Narrator(model="gemini-3.1-flash-tts-preview", voice="Orus", client=client)

    audio, mime = narrator._run_tts(SCRIPT)

    assert audio == OGG_BYTES
    assert mime == "audio/ogg"
    kwargs = client.models.kwargs
    assert kwargs is not None
    assert kwargs["model"] == "gemini-3.1-flash-tts-preview"
    config = kwargs["config"]
    assert config.response_modalities == ["AUDIO"]
    voice_config = config.speech_config.voice_config.prebuilt_voice_config
    assert voice_config.voice_name == "Orus"
    # A direct generate_content call: one text request carrying the persona.
    assert len(kwargs["contents"]) == 1
    assert NARRATOR_PERSONA in kwargs["contents"][0]
    assert SCRIPT in kwargs["contents"][0]


def test_run_tts_raises_when_the_response_contains_no_audio():
    client = FakeClient(_response_with_text_only())
    narrator = Narrator(client=client)

    with pytest.raises(NarratorError):
        narrator._run_tts(SCRIPT)


def test_run_tts_without_a_key_raises_narrator_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    narrator = Narrator(api_key=None)

    with pytest.raises(NarratorError):
        narrator._run_tts(SCRIPT)


# --- synthesize happy path ------------------------------------------------------


def test_synthesize_returns_audio_and_passes_the_raw_script_to_the_seam(caplog):
    narrator = ScriptedNarrator()

    with caplog.at_level(logging.INFO):
        audio = narrator.synthesize(42, SCRIPT)

    assert audio == OGG_BYTES
    assert narrator.tts_calls == [SCRIPT]


# --- temp-file lifecycle --------------------------------------------------------


def test_synthesize_creates_a_temp_file_and_unlinks_it_on_success(monkeypatch):
    created = _spy_tempfiles(monkeypatch)
    narrator = ScriptedNarrator()  # already OGG → no conversion

    audio = narrator.synthesize(1, SCRIPT)

    assert audio == OGG_BYTES
    assert created, "the audio must be staged through a temp file"
    assert all(not os.path.exists(path) for path in created), "temp files must be unlinked"


def test_synthesize_unlinks_every_temp_file_when_conversion_fails(monkeypatch, caplog):
    created = _spy_tempfiles(monkeypatch)

    class ExplodingConvert(ScriptedNarrator):
        def _run_tts(self, script):
            return WAV_BYTES, "audio/wav"

        def _convert_to_ogg(self, input_path, output_path, **kwargs):
            raise NarratorError("ffmpeg is not installed")

    narrator = ExplodingConvert()

    with caplog.at_level(logging.ERROR), pytest.raises(NarratorError):
        narrator.synthesize(2, SCRIPT)

    assert created, "the input temp file must have been written before conversion"
    assert all(not os.path.exists(path) for path in created)


# --- conversion seam ------------------------------------------------------------


def test_synthesize_converts_non_ogg_audio_end_to_end_to_a_real_ogg_temp_path(
    monkeypatch,
):
    """Closes the B1 blind spot: exercise the **real** conversion seam.

    A FakeClient returns a WAV payload through the real ``_run_tts`` /
    ``_run_tts_bounded`` path; ``synthesize`` must stage it to a real temp file,
    run the *real* ``_convert_to_ogg`` (only the ``subprocess.run`` boundary is
    faked) with the forced Ogg output format, and return bytes a voice note can
    send. The earlier suite hid B1 because every conversion test replaced
    ``_convert_to_ogg`` with a recorder that never built the ffmpeg command.
    """
    monkeypatch.setattr(narrator_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    created = _spy_tempfiles(monkeypatch)
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        # The target must be a real path with an Ogg-recognisable extension.
        target = cmd[-1]
        assert target.endswith(".ogg"), "ffmpeg infers the container from the suffix"
        with open(cmd[cmd.index("-i") + 1], "rb") as source_handle:
            captured["input_bytes"] = source_handle.read()
        with open(target, "wb") as handle:
            handle.write(OGG_BYTES)

        class Completed:
            returncode = 0
            stderr = b""

        return Completed()

    monkeypatch.setattr(narrator_module.subprocess, "run", fake_run)
    client = FakeClient(_response_with_audio(WAV_BYTES, mime="audio/wav"))
    narrator = Narrator(client=client)

    audio = narrator.synthesize(3, SCRIPT)

    assert audio == OGG_BYTES
    # The returned bytes are voice-note ready (no further conversion).
    assert narrator._needs_conversion(audio, "audio/ogg") is False
    # The real ffmpeg command was built by the real seam, with a forced Ogg
    # format immediately before the (real) output temp path.
    cmd = captured["cmd"]
    assert cmd[-3:] == ["-f", "ogg", cmd[-1]]
    # The raw non-OGG TTS bytes hit a real input temp file first.
    assert captured["input_bytes"] == WAV_BYTES
    # A real ``.ogg`` target temp path was created and every temp path unlinked.
    assert any(path.endswith(".ogg") for path in created)
    assert all(not os.path.exists(path) for path in created)


# --- real-ffmpeg input contract (CI-safe: skipped when ffmpeg is absent) --------
#
# The live B2 bug: the model's default payload is *headerless* LINEAR16 PCM, and
# the seam staged it as an opaque ``.tmp`` with no declared demuxer, so ffmpeg
# aborted with "Invalid data found when processing input". These tests exercise
# the real binary and lock the new input contract.


@REQUIRES_FFMPEG
def test_real_ffmpeg_converts_a_wav_header_payload_to_oggs():
    """A RIFF/WAVE payload is staged with a ``.wav`` suffix and ffmpeg sniffs it."""
    narrator = ScriptedNarrator(audio=_sample_wav_bytes(), mime="audio/wav")

    audio = narrator.synthesize(20, SCRIPT)

    assert audio[:4] == b"OggS"
    assert len(audio) > 100


@REQUIRES_FFMPEG
def test_real_ffmpeg_converts_headerless_pcm_payload_to_oggs():
    """The live bug case: headerless LINEAR16 PCM must convert to real Ogg/Opus.

    A ``.tmp`` stage with no declared demuxer fails here ("Invalid data found");
    the seam must declare Gemini TTS's known raw format (s16le/24000/mono).
    """
    narrator = ScriptedNarrator(
        audio=_sample_pcm_frames(), mime="audio/L16; rate=24000"
    )

    audio = narrator.synthesize(21, SCRIPT)

    assert audio[:4] == b"OggS"
    assert len(audio) > 100


@REQUIRES_FFMPEG
def test_real_ffmpeg_probing_an_opaque_tmp_headerless_pcm_source_fails(tmp_path):
    """Negative control for the B2 bug: the *same* headerless LINEAR16 payload
    staged as an opaque ``.tmp`` with no declared input format is exactly the
    live failure.

    Real ffmpeg cannot probe a headerless payload — it exits 1 with "Invalid
    data found when processing input". This pins the class of bug the seam
    prevents: the declared ``-f s16le -ar 24000 -ac 1`` input args (proven green
    by ``test_real_ffmpeg_converts_headerless_pcm_payload_to_oggs``) are
    load-bearing, not cosmetic — reverting the seam re-introduces this error.
    """
    source = tmp_path / "narrator-source-opaque.tmp"
    source.write_bytes(_sample_pcm_frames())
    target = tmp_path / "out.ogg"
    narrator = Narrator()

    with pytest.raises(NarratorError) as excinfo:
        narrator._convert_to_ogg(str(source), str(target))

    assert "Invalid data found" in str(excinfo.value)


def test_headerless_pcm_is_declared_as_s16le_24000_mono_and_staged_as_pcm(monkeypatch):
    """Prevention lock: the raw-PCM contract is declared to ffmpeg explicitly and
    the source temp file gets a ``.pcm`` suffix (not an opaque ``.tmp``)."""
    monkeypatch.setattr(narrator_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    created = _spy_tempfiles(monkeypatch)
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        # Read the staged source before ``synthesize``'s ``finally`` unlinks it.
        with open(cmd[cmd.index("-i") + 1], "rb") as handle:
            captured["input_bytes"] = handle.read()
        with open(cmd[-1], "wb") as handle:
            handle.write(OGG_BYTES)

        class Completed:
            returncode = 0
            stderr = b""

        return Completed()

    monkeypatch.setattr(narrator_module.subprocess, "run", fake_run)
    pcm = _sample_pcm_frames()
    narrator = ScriptedNarrator(audio=pcm, mime="audio/L16; rate=24000")

    audio = narrator.synthesize(22, SCRIPT)

    assert audio == OGG_BYTES
    cmd = captured["cmd"]
    # The declared input format comes *before* ``-i``; the output ``-f ogg`` last.
    assert cmd[cmd.index("-f") + 1] == NARRATOR_PCM_CODEC
    assert cmd[cmd.index("-ar") + 1] == str(NARRATOR_SAMPLE_RATE_HZ)
    assert cmd[cmd.index("-ac") + 1] == str(NARRATOR_CHANNELS)
    assert cmd[cmd.index("-i") + 1].endswith(".pcm")
    assert captured["input_bytes"] == pcm
    assert any(path.endswith(".pcm") for path in created)
    assert cmd[-3:] == ["-f", "ogg", cmd[-1]]


def test_wav_payload_is_staged_as_dot_wav_without_a_declared_input_format(monkeypatch):
    """A self-describing RIFF/WAVE payload needs no ``-f`` hint (ffmpeg probes)."""
    monkeypatch.setattr(narrator_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    created = _spy_tempfiles(monkeypatch)
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        # Read the staged source before ``synthesize``'s ``finally`` unlinks it.
        with open(cmd[cmd.index("-i") + 1], "rb") as handle:
            captured["input_bytes"] = handle.read()
        with open(cmd[-1], "wb") as handle:
            handle.write(OGG_BYTES)

        class Completed:
            returncode = 0
            stderr = b""

        return Completed()

    monkeypatch.setattr(narrator_module.subprocess, "run", fake_run)
    wav = _sample_wav_bytes()
    narrator = ScriptedNarrator(audio=wav, mime="audio/wav")

    audio = narrator.synthesize(23, SCRIPT)

    assert audio == OGG_BYTES
    cmd = captured["cmd"]
    # No demuxer arg between ``-loglevel error`` and ``-i`` for a sniffable WAV.
    i = cmd.index("-i")
    assert cmd[i - 1] == "error"
    assert cmd[i + 1].endswith(".wav")
    assert captured["input_bytes"] == wav
    assert any(path.endswith(".wav") for path in created)


def test_non_pcm_mime_is_probed_by_ffmpeg_without_a_declared_input_format(
    monkeypatch,
):
    """N1: an MP3/FLAC (or otherwise non-PCM) payload must not be force-decoded.

    The old seam treated *everything* non-RIFF as headerless s16le/24000/mono. A
    future non-PCM response would then be decoded as raw PCM → silent noise
    (a fail-open honesty bug). The seam must instead stage the source opaquely
    (``.tmp``) with no declared format so ffmpeg probes the real container.
    """
    monkeypatch.setattr(narrator_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    created = _spy_tempfiles(monkeypatch)
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        with open(cmd[-1], "wb") as handle:
            handle.write(OGG_BYTES)

        class Completed:
            returncode = 0
            stderr = b""

        return Completed()

    monkeypatch.setattr(narrator_module.subprocess, "run", fake_run)
    # A real MP3 begins with an ID3 tag / frame sync, never RIFF/L16.
    mp3 = b"ID3\x04\x00\x00\x00\x00\x00\x00mp3-audio-payload"
    narrator = ScriptedNarrator(audio=mp3, mime="audio/mpeg")

    audio = narrator.synthesize(24, SCRIPT)

    assert audio == OGG_BYTES
    cmd = captured["cmd"]
    i = cmd.index("-i")
    # No demuxer args between ``-loglevel error`` and ``-i``: ffmpeg probes.
    assert cmd[i - 1] == "error"
    assert cmd[i + 1].endswith(".tmp")
    assert any(path.endswith(".tmp") for path in created)


def test_linear_pcm_mime_rate_is_honoured_in_the_declared_input_format(monkeypatch):
    """N1: when the mime declares linear PCM, its ``rate=`` pins ``-ar`` (not the
    24000 default), so a differently-sampled payload is not played back at the
    wrong speed."""
    monkeypatch.setattr(narrator_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    _spy_tempfiles(monkeypatch)
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        with open(cmd[-1], "wb") as handle:
            handle.write(OGG_BYTES)

        class Completed:
            returncode = 0
            stderr = b""

        return Completed()

    monkeypatch.setattr(narrator_module.subprocess, "run", fake_run)
    narrator = ScriptedNarrator(
        audio=_sample_pcm_frames(sample_rate=16000),
        mime="audio/L16;codec=pcm;rate=16000",
    )

    audio = narrator.synthesize(25, SCRIPT)

    assert audio == OGG_BYTES
    cmd = captured["cmd"]
    assert cmd[cmd.index("-f") + 1] == NARRATOR_PCM_CODEC
    assert cmd[cmd.index("-ar") + 1] == "16000"
    assert cmd[cmd.index("-i") + 1].endswith(".pcm")


def test_ogg_audio_is_not_converted(monkeypatch):
    narrator = ScriptedNarrator(mime="audio/ogg")

    def _forbidden(input_path, output_path):  # pragma: no cover - must not run
        raise AssertionError("already-OGG audio must not be converted")

    narrator._convert_to_ogg = _forbidden  # type: ignore[method-assign]

    assert narrator.synthesize(4, SCRIPT) == OGG_BYTES


def test_ogg_magic_bytes_skip_conversion_even_with_an_unknown_mime():
    narrator = ScriptedNarrator(mime="application/octet-stream")

    def _forbidden(input_path, output_path):  # pragma: no cover - must not run
        raise AssertionError("OggS magic must be trusted without conversion")

    narrator._convert_to_ogg = _forbidden  # type: ignore[method-assign]

    assert narrator.synthesize(5, SCRIPT) == OGG_BYTES


def test_missing_ffmpeg_when_conversion_needed_raises_unavailable(monkeypatch):
    monkeypatch.setattr(narrator_module.shutil, "which", lambda name: None)
    narrator = ScriptedNarrator(audio=WAV_BYTES, mime="audio/wav")

    with pytest.raises(NarratorError) as excinfo:
        narrator.synthesize(6, SCRIPT)

    assert "ffmpeg" in str(excinfo.value).lower()


def test_convert_to_ogg_invokes_ffmpeg_opus_encoder_and_forces_ogg_output(
    monkeypatch, tmp_path
):
    """B1 lock-in: ffmpeg infers the output container from the extension, so the
    command must carry an explicit ``-f ogg`` for an opaque (``.tmp``) target."""
    monkeypatch.setattr(narrator_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        with open(cmd[-1], "wb") as handle:
            handle.write(b"OggSfromffmpeg")

        class Completed:
            returncode = 0
            stderr = b""

        return Completed()

    monkeypatch.setattr(narrator_module.subprocess, "run", fake_run)
    source = tmp_path / "in.wav"
    source.write_bytes(WAV_BYTES)
    # Deliberately not ``.ogg``: the explicit format flag must make this work.
    target = tmp_path / "out.tmp"
    narrator = Narrator()

    narrator._convert_to_ogg(str(source), str(target))

    cmd = captured["cmd"]
    assert cmd[0] == "/usr/bin/ffmpeg"
    assert "libopus" in cmd
    assert str(source) in cmd
    assert str(target) == cmd[-1]
    # ``-f ogg`` must appear immediately before the output path.
    assert cmd[-3:] == ["-f", "ogg", str(target)]


def test_convert_to_ogg_raises_when_ffmpeg_exits_nonzero(monkeypatch, tmp_path):
    monkeypatch.setattr(narrator_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    def fake_run(cmd, **kwargs):
        class Completed:
            returncode = 1
            stderr = b"Invalid data found"

        return Completed()

    monkeypatch.setattr(narrator_module.subprocess, "run", fake_run)
    source = tmp_path / "in.wav"
    source.write_bytes(WAV_BYTES)
    narrator = Narrator()

    with pytest.raises(NarratorError):
        narrator._convert_to_ogg(str(source), str(tmp_path / "out.ogg"))


def test_convert_to_ogg_raises_when_ffmpeg_produces_no_output(monkeypatch, tmp_path):
    """Exit 0 but an empty/absent output file is still a failure — never silent."""
    monkeypatch.setattr(narrator_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    def fake_run(cmd, **kwargs):
        open(cmd[-1], "wb").close()  # create a zero-byte target

        class Completed:
            returncode = 0
            stderr = b""

        return Completed()

    monkeypatch.setattr(narrator_module.subprocess, "run", fake_run)
    source = tmp_path / "in.wav"
    source.write_bytes(WAV_BYTES)
    narrator = Narrator()

    with pytest.raises(NarratorError):
        narrator._convert_to_ogg(str(source), str(tmp_path / "out.ogg"))


def test_convert_to_ogg_raises_when_ffmpeg_disappears(monkeypatch, tmp_path):
    monkeypatch.setattr(narrator_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("ffmpeg vanished")

    monkeypatch.setattr(narrator_module.subprocess, "run", fake_run)
    source = tmp_path / "in.wav"
    source.write_bytes(WAV_BYTES)
    narrator = Narrator()

    with pytest.raises(NarratorError):
        narrator._convert_to_ogg(str(source), str(tmp_path / "out.ogg"))


def test_convert_to_ogg_raises_when_ffmpeg_times_out(monkeypatch, tmp_path):
    monkeypatch.setattr(narrator_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1.0)

    monkeypatch.setattr(narrator_module.subprocess, "run", fake_run)
    source = tmp_path / "in.wav"
    source.write_bytes(WAV_BYTES)
    narrator = Narrator()

    with pytest.raises(NarratorError):
        narrator._convert_to_ogg(str(source), str(tmp_path / "out.ogg"))


# --- errors, timeout and input validation ---------------------------------------


def test_api_failure_raises_narrator_error():
    narrator = FailingNarrator()

    with pytest.raises(NarratorError):
        narrator.synthesize(7, SCRIPT)


def test_empty_audio_raises_narrator_error():
    narrator = ScriptedNarrator(audio=b"", mime="audio/ogg")

    with pytest.raises(NarratorError):
        narrator.synthesize(8, SCRIPT)


def test_timeout_raises_narrator_error_within_budget(monkeypatch):
    monkeypatch.setenv("NARRATOR_GEMINI_TIMEOUT", "0.2")
    narrator = HangingNarrator(timeout=0.2)
    started = time.monotonic()

    with pytest.raises(NarratorError):
        narrator.synthesize(9, SCRIPT)

    assert time.monotonic() - started < 0.8  # bounded by the timeout, not the 1s sleep


@pytest.mark.parametrize("script", ["", "   ", "\n\t"])
def test_synthesize_rejects_a_missing_script(script):
    narrator = ScriptedNarrator()

    with pytest.raises(NarratorError):
        narrator.synthesize(10, script)


def test_synthesize_rejects_a_non_string_script():
    narrator = ScriptedNarrator()

    with pytest.raises(NarratorError):
        narrator.synthesize(11, None)  # type: ignore[arg-type]


@pytest.mark.parametrize("chat_id", ["42", None, True])
def test_synthesize_rejects_a_non_int_chat_id(chat_id):
    narrator = ScriptedNarrator()

    with pytest.raises(NarratorError):
        narrator.synthesize(chat_id, SCRIPT)  # type: ignore[arg-type]


# --- redaction ------------------------------------------------------------------


def test_api_key_is_redacted_from_the_error_and_the_logs(monkeypatch, caplog):
    secret = "SUPER-SECRET-GEMINI-KEY"

    class LeakyNarrator(Narrator):
        def _run_tts(self, script):
            raise RuntimeError(f"403 forbidden: key={secret} was rejected")

    monkeypatch.setattr(narrator_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    narrator = LeakyNarrator(api_key=secret)

    with caplog.at_level(logging.DEBUG), pytest.raises(NarratorError) as excinfo:
        narrator.synthesize(12, SCRIPT)

    assert secret not in str(excinfo.value)
    assert secret not in caplog.text


# --- constants / env resolution -------------------------------------------------


def test_unavailable_reply_copy_is_locked():
    assert NARRATOR_REPLY_UNAVAILABLE == (
        "Hang on — the narrator lost his voice. Give that another go?"
    )


def test_default_model_is_locked():
    assert DEFAULT_NARRATOR_MODEL == "gemini-3.1-flash-tts-preview"


def test_default_timeout_is_locked():
    assert DEFAULT_NARRATOR_TIMEOUT == 60


def test_default_voice_is_a_firm_prebuilt_voice():
    assert DEFAULT_NARRATOR_VOICE == "Orus"


def test_resolve_narrator_model_uses_default_when_env_absent(monkeypatch):
    monkeypatch.delenv("NARRATOR_MODEL", raising=False)

    assert resolve_narrator_model() == "gemini-3.1-flash-tts-preview"


def test_resolve_narrator_model_honours_env_override(monkeypatch):
    monkeypatch.setenv("NARRATOR_MODEL", "gemini-9.9-tts-override")

    assert resolve_narrator_model() == "gemini-9.9-tts-override"
    assert narrator_module.resolve_narrator_model() == "gemini-9.9-tts-override"


def test_resolve_narrator_voice_uses_default_when_env_absent(monkeypatch):
    monkeypatch.delenv("NARRATOR_VOICE", raising=False)

    assert resolve_narrator_voice() == "Orus"


def test_resolve_narrator_voice_honours_env_override(monkeypatch):
    monkeypatch.setenv("NARRATOR_VOICE", "Fenrir")

    assert resolve_narrator_voice() == "Fenrir"


def test_resolve_narrator_timeout_uses_default_when_env_absent(monkeypatch):
    monkeypatch.delenv("NARRATOR_GEMINI_TIMEOUT", raising=False)

    assert resolve_narrator_timeout() == 60


def test_resolve_narrator_timeout_honours_env_override(monkeypatch):
    monkeypatch.setenv("NARRATOR_GEMINI_TIMEOUT", "75")

    assert resolve_narrator_timeout() == 75


@pytest.mark.parametrize("raw", ["", "  ", "soon", "0", "-5"])
def test_resolve_narrator_timeout_falls_back_on_invalid_values(monkeypatch, raw):
    monkeypatch.setenv("NARRATOR_GEMINI_TIMEOUT", raw)

    assert resolve_narrator_timeout() == 60


def test_timeout_env_is_re_read_at_call_time(monkeypatch):
    monkeypatch.setenv("NARRATOR_GEMINI_TIMEOUT", "30")
    narrator = Narrator(api_key="test-key")

    assert narrator.timeout == 30

    monkeypatch.setenv("NARRATOR_GEMINI_TIMEOUT", "90")

    assert narrator.timeout == 90, "a running deployment's override must apply"


def test_explicit_timeout_pins_the_value(monkeypatch):
    monkeypatch.setenv("NARRATOR_GEMINI_TIMEOUT", "30")
    narrator = Narrator(api_key="test-key", timeout=45)

    assert narrator.timeout == 45

    monkeypatch.setenv("NARRATOR_GEMINI_TIMEOUT", "90")

    assert narrator.timeout == 45, "an explicit timeout must not be env-overridden"


def test_constructor_honours_explicit_model_and_voice():
    narrator = Narrator(model="gemini-custom-tts", voice="Puck")

    assert narrator.model == "gemini-custom-tts"
    assert narrator.voice == "Puck"


# --- persona voice + instruction (Phase 8) --------------------------------------


@pytest.fixture(autouse=True)
def _clean_persona_voice_env(monkeypatch):
    """Persona voice resolution must not inherit a developer's host exports."""
    monkeypatch.delenv("NARRATOR_VOICE", raising=False)
    monkeypatch.delenv("NARRATOR_VOICE_IRWIN", raising=False)


@pytest.mark.parametrize("persona", list(Persona))
def test_synthesize_uses_the_persona_voice_and_instruction(persona):
    narrator = PersonaRecordingNarrator()

    audio = narrator.synthesize(30, SCRIPT, persona)

    assert audio == OGG_BYTES
    assert narrator.seen_voices == [resolve_persona_voice(persona)]
    request = narrator.seen_requests[0]
    assert request.startswith(PERSONA_NARRATOR_INSTRUCTION[persona])
    assert SCRIPT in request


def test_synthesize_defaults_to_the_attenborough_voice_and_instruction():
    """Backward compatibility by construction: no persona == exactly Phase 6."""
    narrator = PersonaRecordingNarrator()

    narrator.synthesize(31, SCRIPT)

    assert narrator.seen_voices == [DEFAULT_NARRATOR_VOICE]
    assert narrator.seen_requests[0].startswith(NARRATOR_PERSONA)


def test_synthesize_attenborough_voice_is_the_phase6_default():
    narrator = PersonaRecordingNarrator()

    narrator.synthesize(32, SCRIPT, Persona.ATTENBOROUGH)

    assert narrator.seen_voices == [resolve_narrator_voice()]


def test_synthesize_irwin_uses_a_different_voice_than_attenborough():
    narrator = PersonaRecordingNarrator()

    narrator.synthesize(33, SCRIPT, Persona.ATTENBOROUGH)
    narrator.synthesize(34, SCRIPT, Persona.IRWIN)

    assert narrator.seen_voices[0] != narrator.seen_voices[1], (
        "the two personas must sound different"
    )
    assert narrator.seen_voices[1] == PERSONA_NARRATOR_VOICE[Persona.IRWIN]


def test_synthesize_persona_voice_is_resolved_at_call_time(monkeypatch):
    narrator = PersonaRecordingNarrator()

    narrator.synthesize(35, SCRIPT, Persona.IRWIN)
    monkeypatch.setenv("NARRATOR_VOICE_IRWIN", "Aoife")
    narrator.synthesize(36, SCRIPT, Persona.IRWIN)

    assert narrator.seen_voices == ["Charon", "Aoife"]


def test_synthesize_persona_does_not_mutate_the_constructor_voice():
    narrator = PersonaRecordingNarrator(voice="Puck")

    narrator.synthesize(37, SCRIPT, Persona.IRWIN)

    assert narrator.seen_voices == [resolve_persona_voice(Persona.IRWIN)]
    assert narrator.voice == "Puck", "the constructor voice must stay intact"


def test_synthesize_clears_the_persona_context_after_the_call():
    """No persona leaks into a later direct TTS call on the same instance."""
    narrator = PersonaRecordingNarrator()

    narrator.synthesize(38, SCRIPT, Persona.IRWIN)
    narrator._run_tts(SCRIPT)  # direct call — must be back on the defaults

    assert narrator.seen_voices[0] == resolve_persona_voice(Persona.IRWIN)
    assert narrator.seen_voices[1] == DEFAULT_NARRATOR_VOICE
    assert narrator.seen_requests[1].startswith(NARRATOR_PERSONA)


def test_synthesize_clears_the_persona_context_even_when_tts_fails():
    narrator = FailingNarrator()

    with pytest.raises(NarratorError):
        narrator.synthesize(39, SCRIPT, Persona.IRWIN)

    assert narrator._active_voice is None
    assert narrator._active_instruction is None


def test_synthesize_rejects_a_free_text_persona():
    narrator = ScriptedNarrator()

    with pytest.raises(NarratorError):
        narrator.synthesize(40, SCRIPT, "irwin")  # type: ignore[arg-type]


def test_direct_run_tts_uses_the_phase6_defaults_without_a_persona_call():
    """A direct seam call (no synthesize) reproduces Phase 6 exactly."""
    client = FakeClient(_response_with_audio(OGG_BYTES))

    class CapturingNarrator(Narrator):
        def _run_tts(self, script):
            content = self._request_text(script)
            config = self._build_config()
            self.captured = (
                content,
                config.speech_config.voice_config.prebuilt_voice_config.voice_name,
            )
            return OGG_BYTES, "audio/ogg"

    narrator = CapturingNarrator(client=client)
    narrator._run_tts(SCRIPT)

    content, voice = narrator.captured
    assert voice == DEFAULT_NARRATOR_VOICE
    assert content.startswith(NARRATOR_PERSONA)


# --- "The Narrator is not an agent" (source lock) -------------------------------


def test_narrator_module_has_no_adk_agent_machinery():
    """NB4: Phase 6 locks "The Narrator is not an agent".

    The script goes straight to Gemini TTS via a direct ``generate_content``
    call — there is no ADK ``LlmAgent`` and no ``Runner`` session management like
    the Scripter/Converter. Walk the compiled AST so a mere mention in a comment
    cannot mask a real regression.
    """
    import ast
    from pathlib import Path

    source = Path(narrator_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(narrator_module.__file__))

    imported_roots: set[str] = set()
    referenced_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(
                alias.name.split(".")[0].lower() for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0].lower())
        elif isinstance(node, ast.Name):
            referenced_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced_names.add(node.attr)

    assert "adk" not in imported_roots, "narrator must not import ADK"
    assert not ({"LlmAgent", "Runner"} & referenced_names), (
        "narrator must not use an ADK LlmAgent or Runner"
    )
