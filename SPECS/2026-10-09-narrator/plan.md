# Phase 6 — Narrator (TTS voice-note delivery) Plan

## Task Groups (numbered)

### 1. Verify API facts and lock defaults
- Confirm exact field names/types for `gemini-3.1-flash-tts-preview` with python-genai 2.29.0: `client.models.generate_content`, `GenerateContentConfig(response_modalities=["AUDIO"], speech_config=SpeechConfig(voice_config=VoiceConfig(prebuilt_voice_config=PrebuiltVoiceConfig(voice_name=...))))`. 
- Identify response structure: audio bytes in first audio part; determine mime (e.g. audio/wav or audio/ogg). 
- Review prebuilt voices list; select default voice for "deep male, posh British, classic wildlife documentary" (e.g. `Kore` or `Orus`) and record rationale; allow `NARRATOR_VOICE` override. 
- Lock `NARRATOR_REPLY_UNAVAILABLE`, timeouts, model.

### 2. Branch setup (done)
- Branch `feature/2026-10-09-narrator` cut from `feature/2026-10-09-scripter` (commit 08b8f4d), checked out. 

### 3. Add send_voice to TelegramClient
- In `src/telegram_client.py`, add `SendVoiceResponse` model (ok + optional description). 
- Implement `send_voice(chat_id: int, voice_bytes: bytes, *, filename: str = "narration.ogg", mime_type: str = "audio/ogg") -> None`. Uses `_request` with `data={"chat_id": chat_id}` and `files={"voice": (filename, voice_bytes, mime_type)}` to POST `/sendVoice`. Validate response ok; raise `TelegramAPIError` on failure/malformed (redacting token path). Mirror `send_photo`. Add `@log_call()`.

### 4. Implement Narrator (direct genai TTS, no ADK agent)
- Create `src/narrator.py`. 
- Constants: `NARRATOR_REPLY_UNAVAILABLE`, `DEFAULT_NARRATOR_MODEL="gemini-3.1-flash-tts-preview"`, `DEFAULT_NARRATOR_TIMEOUT=60`. 
- `resolve_narrator_model()` reads `NARRATOR_MODEL` from env at call time (house pattern). `resolve_narrator_voice()` reads `NARRATOR_VOICE`. 
- `NarratorError(RuntimeError)`. 
- `Narrator` class: accepts optional `store` (for consistency, not used for audio), `model`, `timeout`, `voice`. 
- `_run_tts(script: str) -> tuple[bytes, str | None]` (scriptable seam). Build persona instruction + script: "Narrate in a classic British wildlife documentary presenter voice: deep, male, posh British accent. Speak clearly and dramatically. " + script. Call `client.models.generate_content(model=..., contents=[...], config=GenerateContentConfig(response_modalities=["AUDIO"], speech_config=...))`. Extract audio bytes from response candidates/content parts (first audio part). Return (bytes, mime/type if present). Bounded timeout via daemon thread + `threading.Event` (house pattern). Log via structured logger; never log script content in full if sensitive, but script is user content — follow existing redaction rules (no tokens). 
- Conversion seam: if returned format not suitable for Telegram sendVoice (e.g. PCM/WAV), convert to OGG/Opus using ffmpeg (`ffmpeg -i input -c:a libopus -b:a 64k -ar 16000 output.ogg` or appropriate). Check for ffmpeg availability (`shutil.which("ffmpeg")`); if needed but missing → raise `NarratorError`. Use temp files (tempfile.NamedTemporaryFile) in `/tmp` or system temp; always cleanup in finally (input/output temp paths). 
- `synthesize(chat_id: int, script: str) -> bytes`: validate inputs; call `_run_tts`; return final bytes. Raise `NarratorError` on any failure.

### 5. Wire Gateway for narration (after script)
- In `src/gateway.py`: add `narrator=None` param to `__init__`. Store `self._narrator = narrator`. 
- In `_handle_scripting`, after successful script send (or consider: narration should run after script is sent? Order is profile→photo→script→voice; sending script text succeeded; now send voice). Also handle case where script send failed: per requirements, script was produced and stored — but delivery failed; should we still attempt narration? The stored `state.script` exists. However the voice note is the fourth message; if script text didn't reach user, sending voice alone may be odd. But spec says "completion delivery order becomes..." and narration uses `state.script`. Follow: attempt narration when `narrator` wired and we have script; if script send failed earlier, we still have script in state — proceed to narration (log event). 
- Add `_handle_narration(chat_id: int, update_id: int) -> None`. 
  - If `self._narrator is None`: return (Phases 1–5 unchanged). 
  - Get script from shared state? Gateway doesn't own state driver directly except via interviewer/store patterns? Interviewer holds `self._state` (InterviewStateStore). Gateway can access via `self._interviewer.state(chat_id).script` if interviewer wired? Or add store param. Check existing: interviewer is `src.interviewer.Interviewer` with `state()` method returning `InterviewState`. Scripter uses shared store; narration needs script from same driver. So if interviewer is wired, we can read `self._interviewer.state(chat_id).script`. Also handle case where interviewer None? Unusual if scripter ran, but be defensive. 
  - If script missing/empty → log `event=narrator_failed reason=missing_script chat_id=...` and send `NARRATOR_REPLY_UNAVAILABLE`. 
  - Call `self._narrator.synthesize(chat_id, script)`. On success, call `self._client.send_voice(chat_id, audio_bytes)`. Log `event=narrator_sent chat_id=... bytes=...`. 
  - On synth exception → log `event=narrator_failed stage=synthesize` + exception, send `NARRATOR_REPLY_UNAVAILABLE`. 
  - On send_voice exception → log `event=narrator_send_failed chat_id=...` (no NARRATOR_REPLY_UNAVAILABLE). Loop survives.

### 6. Wire main.py
- Import `Narrator` from `src.narrator`. Read env: `NARRATOR_MODEL`, `NARRATOR_VOICE`, `NARRATOR_GEMINI_TIMEOUT`. 
- Instantiate `Narrator(model=..., timeout=int(...), voice=...)`. Pass `narrator=...` to `Gateway`. 
- Log `event=narrator_ready model=... voice=...` (no secrets).

### 7. Tests (offline, Red first)
- Unit tests for narrator: 
  - synthesize returns bytes when API returns audio; temp files cleaned on success and error. 
  - _run_tts uses correct model/config fields. 
  - timeout path raises NarratorError. 
  - conversion path invoked when mime indicates non-OGG; ffmpeg missing → NarratorError. 
  - missing script (if passed empty) handled. 
  - API error → NarratorError; redaction not violated in logs. 
- Component tests (GateClient): 
  - ordering profile→photo→script→voice when narrator wired. 
  - narrator=None regression (no voice). 
  - missing script → NARRATOR_REPLY_UNAVAILABLE. 
  - synth error → NARRATOR_REPLY_UNAVAILABLE. 
  - send_voice failure → narrator_send_failed logged, no NARRATOR_REPLY_UNAVAILABLE. 
  - conversion-failure path. 
- Integration guarded: `tests/integration/test_live_narrator.py` checks RUN_LIVE_GEMINI=1 + healthy key; requires seeded state.script; asserts bytes and OGG/Opus magic (e.g. OggS or Opus signature); skips otherwise.

### 8. Validation
- Run `scripts/test` (per repo). 
- Typecheck/lint if available via scripts/hooks or common commands. 
- Verify ordering assertions in component tests. 
- Manual sanity: narrator_ready logged with model/voice.

### 9. Report
- Verified API facts, branch, spec paths, files, open questions.
