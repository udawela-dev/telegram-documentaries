# Phase 6 — The Narrator (TTS voice-note delivery)

## Context
The Telegram Documentaries pipeline: Bouncer (2) → Interviewer (3) → Converter (4) → Scripter (5) → **Narrator (this feature)**.

Phase 5 stores the raw TTS-ready 60–90 word script on the shared per-chat `InterviewState` driver as `state.script` (schema v2). Phase 4 sends the hybrid photo after the profile text; Phase 5 sends the script text as the third message (chat order: profile text → hybrid photo → script text). The Narrator must render that exact paragraph to audio and send it as a Telegram voice note.

Key architectural points from TECH.md:
- **The Narrator is not an agent.** The script is routed directly to Gemini TTS; no LLM reasoning step.
- State is in-memory, versioned (InterviewState schema_version=2, has `script: str | None`), one shared state driver `src/interview_state.py`.
- Structured logging with redaction; never log secrets/tokens.
- Per-call fresh-and-reaped ADK sessions for agent stages (Converter/Scripter). The Narrator does **not** use an ADK LlmAgent — direct Gemini API call.
- Graceful locked replies when a stage cannot run (e.g. CONVERTER_REPLY_UNAVAILABLE, SCRIPTER_REPLY_UNAVAILABLE). The Narrator failure copy will be NARRATOR_REPLY_UNAVAILABLE.
- Resilience: key-free local fallbacks exist for earlier stages when blocked; for TTS the spec requires **NO local/key-free TTS fallback** (user decision). When TTS cannot run (blocked key, API error, timeout, missing script, missing ffmpeg when conversion needed), the gateway logs loudly and sends NARRATOR_REPLY_UNAVAILABLE.

## User Decisions (locked; do NOT re-ask)
1. The Narrator is a **DIRECT Gemini API call** (`client.models.generate_content` on `gemini-3.1-flash-tts-preview`) — explicitly **NOT** a reasoning agent / no ADK LlmAgent.
2. **NO local/key-free TTS fallback** — when TTS cannot run, gateway logs loudly and sends locked graceful `NARRATOR_REPLY_UNAVAILABLE`; never fake/non-natural audio.
3. Delivery via Telegram **sendVoice** (voice note, OGG/Opus).
4. The raw script comes from the shared driver's `state.script` (Phase 5 field).
5. Voice direction: **deep male voice, posh British accent, classic British-wildlife-documentary presenter** — expressed via a persona/style instruction in the request text AND via a prebuilt voice name/config if the API supports it; `NARRATOR_VOICE` env overridable.
6. The audio is saved to a **temporary file** (Telegram-compatible OGG/Opus or MP3), sent, and the temp file is **cleaned up in a finally** (success AND error).
7. Audio conversion handling: if the API returns a non-sendVoice-compatible format, a **scriptable conversion seam** converts to OGG/Opus (ffmpeg CLI if available); missing ffmpeg when conversion is needed → graceful unavailable. Nothing ever silently skipped.
8. **No new state fields** — `state.script` is the only hand-off; audio itself is transient (TECH.md: in-memory only, temp media purged).
9. Env knobs: `NARRATOR_MODEL` (default `gemini-3.1-flash-tts-preview`) and `NARRATOR_GEMINI_TIMEOUT` (bounded, default 60s, house daemon-thread+Event pattern).
10. Completion delivery order becomes **profile text → hybrid photo → script text → voice note**. narrator=None keeps Phases 1–5 unchanged (regression).

## Verified TTS API Facts (google-genai 2.29.0; critical)
Per websearch verification:
- Calling model `gemini-3.1-flash-tts-preview` via `client.models.generate_content` with `GenerateContentConfig` including `response_modalities = ["AUDIO"]` and `speech_config = types.SpeechConfig(voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=...)))`.
- For single-speaker TTS, request uses `speech_config.voice_config.prebuilt_voice_config.voice_name`. The canonical snake_case types names in python-genai (google.genai.types): `SpeechConfig`, `VoiceConfig`, `PrebuiltVoiceConfig`. (Some docs also show camelCase in other SDKs; the Python SDK types use snake_case field names.)
- Response: audio is returned in the model response parts as audio data (base64-encoded) with a mime type (commonly `audio/wav` or PCM-like depending on config; conversion seam handles format for Telegram sendVoice compatibility). Telegram sendVoice typically expects OGG/Opus (voice note); if raw PCM/WAV, ffmpeg conversion to OGG/Opus is required.
- Prebuilt voices available for TTS preview models include ~30 options (e.g. Kore, Puck, Fenrir, Zephyr, Orus, etc.). For "deep male voice, posh British accent, classic British-wildlife-documentary presenter", plausible candidates include voices described as Firm/Even/Informative/Mature (e.g. `Kore` described as "Firm" in some docs; `Orus` "Firm"; others vary). The spec will allow `NARRATOR_VOICE` override and lock the default choice after confirming common options, preferring a firm/low-register prebuilt voice; voice persona is reinforced via a short style instruction in the input text.

## Scope
- `src/narrator.py` — direct genai TTS call. Contains:
  - `NARRATOR_REPLY_UNAVAILABLE` (locked copy)
  - `DEFAULT_NARRATOR_MODEL = "gemini-3.1-flash-tts-preview"`
  - `DEFAULT_NARRATOR_TIMEOUT = 60`
  - `resolve_narrator_model()` reads `NARRATOR_MODEL` env at call time (house pattern)
  - `NarratorError(RuntimeError)` when TTS cannot produce usable audio
  - `Narrator` class: `synthesize(chat_id: int, script: str) -> bytes` returns audio bytes (Telegram-compatible). Internally calls `_run_tts(script: str) -> tuple[bytes, str | None]` (scriptable seam returning (bytes, mime) or (bytes, None)). 
  - `_run_tts` performs direct `client.models.generate_content` with single-speaker speech_config; extracts audio bytes from response (first audio part). Bounded by daemon thread + threading.Event (timeout). Persona/style instruction is prepended/embedded: e.g. "Narrate in a classic British wildlife documentary presenter voice: deep, male, posh British accent. Speak clearly and dramatically." followed by the raw script (or embedded in the single text input). 
  - Audio conversion seam: if mime/format not OGG/Opus-compatible for sendVoice, attempt conversion via ffmpeg (subprocess) to OGG/Opus. If ffmpeg not available when conversion needed → raise NarratorError (graceful unavailable, no silent skip).
  - Temp file lifecycle: write API result to temp file (or intermediate), convert to target temp file if needed, return bytes read from final temp file, always `try/finally` to unlink temp paths on success and error. Use `/tmp`-style temp (or tempfile.NamedTemporaryFile in /tmp).
- `src/telegram_client.py` — add `send_voice(chat_id: int, voice_bytes: bytes, *, filename: str = "narration.ogg", mime_type: str = "audio/ogg") -> None`. Mirrors `send_photo` (multipart POST to sendVoice with `voice` field). Returns validated minimal shape on ok; raises `TelegramAPIError` on failure/malformed (redacting token path unchanged).
- `src/gateway.py` — optional `narrator=` constructor param. After `_handle_scripting` (after script text sent), if `narrator` wired and `state.script` exists for the chat, call `_handle_narration(chat_id, update_id)`. Behavior:
  - narrator=None → Phases 1–5 unchanged.
  - Missing script (`state.script is None`) → log loudly (`event=narrator_failed reason=missing_script`) and send `NARRATOR_REPLY_UNAVAILABLE` (locked). Do not attempt TTS.
  - Synthesize via narrator.synthesize(chat_id, script). On success, send voice via `client.send_voice`. On synth failure → log loudly and send `NARRATOR_REPLY_UNAVAILABLE`. On send failure → log loudly (`event=narrator_send_failed`) with error details; **decide and lock**: sending voice may have partially failed or failed after upload; do not send NARRATOR_REPLY_UNAVAILABLE (that would mislead — script exists and TTS may have produced bytes but delivery failed). Log and continue (loop survives). Temp files cleaned in finally in narrator/at call site.
- `main.py` — wire narrator (no ADK agent). Instantiate `Narrator` with shared store/env; pass to `Gateway(narrator=...)`. Log `event=narrator_ready model=... voice=...` (no tokens).
- Tests (Red/Green, offline first): unit tests for narrator (scripted TTS bytes → temp file written+cleaned, send_voice called, conversion path, missing script, API error, timeout, copy-locks, redaction). Component tests via GateClient including `sent_voices` list, ordering `profile→photo→script→voice`, narrator=None regression, conversion-failure path, send-failure path. Guarded live test `tests/integration/test_live_narrator.py` (RUN_LIVE_GEMINI=1 + healthy key): real audio from seeded state.script; assert bytes + ogg/opus magic; skipped otherwise.
- No new state fields; `InterviewState` unchanged (schema_version remains 2).

## Locked Constants
- `NARRATOR_REPLY_UNAVAILABLE = "Hang on — the narrator lost his voice. Give that another go?"` (mirrors Converter/Scripter)
- `DEFAULT_NARRATOR_MODEL = "gemini-3.1-flash-tts-preview"`
- `DEFAULT_NARRATOR_TIMEOUT = 60` (seconds)
- Default voice name: choose a prebuilt voice suitable for "deep male, posh British, classic wildlife documentary" (e.g. `Kore` or `Orus` as firm male options; allow `NARRATOR_VOICE` env override). Lock default in spec after verification (record choice).

## Contracts
```python
# src/narrator.py
NARRATOR_REPLY_UNAVAILABLE: str
DEFAULT_NARRATOR_MODEL: str
DEFAULT_NARRATOR_TIMEOUT: int

class NarratorError(RuntimeError): pass

class Narrator:
    def __init__(self, *, store=None, model: str | None = None, timeout: int | None = None, voice: str | None = None): ...
    def synthesize(self, chat_id: int, script: str) -> bytes  # returns audio bytes (Telegram-compatible)
    # _run_tts(script: str) -> tuple[bytes, str | None]  # scriptable seam (bytes, mime)

# src/telegram_client.py
def send_voice(self, chat_id: int, voice_bytes: bytes, *, filename: str = "narration.ogg", mime_type: str = "audio/ogg") -> None

# src/gateway.py
Gateway(client, ..., narrator=None)
```

## Failure Handling
- Missing script → loud log + NARRATOR_REPLY_UNAVAILABLE; loop survives.
- TTS API error/timeout/blocked key/malformed response → NarratorError → gateway logs + NARRATOR_REPLY_UNAVAILABLE; temp files cleaned.
- Conversion needed but ffmpeg missing → NarratorError → unavailable.
- send_voice failure → log `event=narrator_send_failed` (no NARRATOR_REPLY_UNAVAILABLE); script remains on state; loop survives.

## Out of scope
- Local TTS fallback (explicitly no).
- New state fields; persistence.
- Multi-speaker TTS; voice cloning.
- Agent reasoning (no ADK LlmAgent).
- Regeneration/retries beyond bounded timeout.

## Reset semantics
`/start`, `/restart`, rejected photos purge portrait/state/converter sessions; audio is transient (no stored audio). Reset clears `state.script` via interviewer/store reset (Phase 5 semantics).
