# The Telegram Documentaries

Send a portrait to a Telegram bot and receive a narrated, comedy-wildlife
documentary about yourself. **Current state: Phase 1 — the gateway.** A
long-polling loop that replies to every message with a hardcoded `"Hi Mate"`.
No LLM / Gemini calls yet.

## Setup

Requires Python 3.11+.

```bash
pip install -r requirements.txt
```

## Environment variables

Copy `.env.example` to `.env` and fill in real values:

| Variable             | Required | Used in Phase 1? |
| -------------------- | -------- | ---------------- |
| `TELEGRAM_BOT_TOKEN` | yes      | yes — long polling auth |
| `GEMINI_API_KEY`     | no       | no (unused until later phases) |

Never commit `.env` — it is gitignored. The bot token must never appear in
logs; the app applies a secret-redaction filter to every log record as a
safety net.

## Run the bot

```bash
python main.py
```

The bot starts a Telegram long-polling loop (`getUpdates`, no webhook, no
public URL) and answers **every** message it receives with:

```
Hi Mate
```

Stop it with `Ctrl-C` (SIGINT) or SIGTERM — it shuts down gracefully.

## Checks (ground truth)

| Command       | What it does |
| ------------- | ------------ |
| `bash scripts/test`  | Runs the full pytest suite (unit, component, integration). |
| `bash scripts/hooks` | Pre-commit gate: syntax check (`compileall`) + `scripts/test`. |

## Architecture (Phase 1)

`main.py` (entry point) → `src/config.py` (`.env` → validated `Settings`) →
`src/telegram_client.py` (raw-HTTP httpx client) → `src/gateway.py`
(polling loop + dispatcher).

Key rules from `SPECS/` honoured here:

- **Typed boundaries** — raw Telegram JSON is parsed into Pydantic models
  (`src/telegram_models.py`) before it is used; malformed items are skipped
  with a log, never fatal.
- **Long polling** — updates are consumed with an advancing `offset`, so the
  same update is never processed twice.
- **Resilience** — a failed poll or failed reply is logged and the loop keeps
  running; one bad update never kills the bot.
- **Logging policy** — structured decorator-based logging
  (`src/logging_utils.py`); no bare `except: pass`, no swallowed errors, no
  un-logged fallbacks.