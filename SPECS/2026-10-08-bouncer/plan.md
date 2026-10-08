# Plan: The Bouncer — ADK vision gate (Phase 2)

Red/Green TDD. Tests first; `scripts/test` / `scripts/hooks` are ground truth.

## Task 1 — Fixtures & gitignore
- [ ] Create `tests/fixtures/` with (a) `person.jpg` (clear human photo,
      CC0/public domain — e.g. a blanked portrait or well-known free photo) and
      (b) `non_human.jpg` (landscape/object, no humans). If the internet fetch
      fails, generate the negative with Pillow (solid gradient landscape) and
      make the positive test skip with a clear logged reason — but TRY hard
      for a real person photo first.
- [ ] Add `.gitignore` exception so `tests/fixtures/*.jpg` (etc.) are tracked:
      `!tests/fixtures/` + `!tests/fixtures/*.*`.

## Task 2 — Typed models: photo messages (RED first)
- [ ] `PhotoSize` model (file_id, width, height, file_size optional) and
      `Message.photo: list[PhotoSize] | None`; keep `extra="allow"`, `strict=True`.
- [ ] Tests: photo message parses to typed PhotoSize list; photo absent = None;
      file_id missing → skipped-with-log semantics preserved.

## Task 3 — Telegram file download (RED first)
- [ ] `File` model (file_id, file_path, file_size|None). `TelegramClient.get_file(
      file_id)` and `download_file(file_path) -> bytes` via
      `https://api.telegram.org/file/bot<token>/<file_path>`. Errors →
      `TelegramAPIError`, redacted, never the token.
- [ ] Tests (MockTransport): get_file parses typed File; download returns bytes;
      network error → TelegramAPIError; token never in errors/logs.

## Task 4 — Bouncer: ADK agent + structured decision (RED first)
- [ ] `src/bouncer.py`: `BOUNCER_MODEL` (env `BOUNCER_MODEL`, default
      `gemini-3.1-flash-lite`), `BOUNCER_REJECTION`, `BOUNCER_UNAVAILABLE_REPLY`,
      `BouncerDecision(human_present, reason)`, class `Bouncer`:
      `LlmAgent(name="bouncer", model=..., instruction=...)` with a strict-JSON
      instruction; `InMemorySessionService`; `Runner`.
      `classify(image_bytes) -> BouncerDecision` (per-chat session), and
      `reset_chat(chat_id)`. Robust JSON extraction (tolerate fenced/messy
      output); parse failure → reject-safe False.
- [ ] Tests (offline, recorded/scripted responses): valid JSON → typed decision;
      fenced JSON parsed; non-JSON → False + logged; uncertain wording → False.

## Task 5 — Gateway gate wiring (RED first)
- [ ] `Gateway(client, bouncer=None, ...)`. In `_dispatch`: text messages
      unchanged (Hi Mate). `message.photo` → if bouncer is None: reply
      `BOUNCER_UNAVAILABLE_REPLY` + loud log. Else: choose largest photo →
      `client.get_file` → `download_file` → `bouncer.classify` →
      accepted: reply `REPLY_TEXT` (unchanged flow); rejected:
      `BOUNCER_REJECTION` reply + `bouncer.reset_chat(chat_id)`; any failure:
      `BOUNCER_UNAVAILABLE_REPLY` + loud log (no reset, no proceed). Loop always
      survives; offsets still advance.
- [ ] Tests with scripted double: photo-accepted → "Hi Mate"; photo-rejected →
      rejection + reset_chat called; failure → unavailable reply + loop alive;
      text unaffected; chat isolation (reset chat A never clears chat B).

## Task 6 — main.py wiring (RED first)
- [ ] Build `Bouncer` only when GEMINI_API_KEY present (never hardcode keys);
      pass into `Gateway`. Add Gemini key to `configure_logging` secrets list.
- [ ] Tests: wiring via injected doubles; config without Gemini key → bouncer
      None path handled (component level).

## Task 7 — Live verification (guarded; RUN_LIVE_GEMINI=1)
- [ ] Integration tests: positive `person.jpg` → human_present True reasoned;
      negative `non_human.jpg` → human_present False. Skip unless flag set.
- [ ] Run them once with the real key; record the actual outcomes in
      `validation.md`.

## Task 8 — Docs & hygiene
- [ ] README: Bouncer section (why, model, env vars incl. BOUNCER_MODEL, live
      tests how-to: `RUN_LIVE_GEMINI=1 python3 -m pytest tests/integration -v`).
- [ ] `.gitignore` fixtures exception; no keys/bytes/tokens committed; pinned
      deps updated (google-adk, google-genai, Pillow only if needed for the
      negative fixture generation — prefer NOT adding Pillow by fetching a real
      negative image instead).