"""Unit tests for config loading: secrets from .env, fail-loud on missing
token, masked secrets in string representations.

Isolation: tests use a scratch env file in tmp_path (never the repo's real
.env) plus monkeypatched environment variables, so results never depend on
what happens to be in the working tree.
"""

import pytest

from src.config import ConfigError, Settings, load_settings


def _env_file(tmp_path, **entries):
    path = tmp_path / "test.env"
    path.write_text("\n".join(f"{key}={value}" for key, value in entries.items()))
    return str(path)


def test_load_settings_returns_token_and_key_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    settings = load_settings(_env_file(tmp_path, TELEGRAM_BOT_TOKEN="123456:TEST", GEMINI_API_KEY="key-example"))
    assert settings.telegram_bot_token == "123456:TEST"
    assert settings.gemini_api_key == "key-example"


def test_load_settings_token_only_when_gemini_key_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    settings = load_settings(_env_file(tmp_path, TELEGRAM_BOT_TOKEN="123456:TEST"))
    assert settings.telegram_bot_token == "123456:TEST"
    assert settings.gemini_api_key is None


def test_load_settings_missing_token_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        load_settings(_env_file(tmp_path))


def test_real_workspace_env_is_never_read_when_absent(tmp_path, monkeypatch):
    """The loader must read only the given env file, never ambient state."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "AMBIENT-TOKEN")
    settings = load_settings(_env_file(tmp_path))
    assert settings.telegram_bot_token == "AMBIENT-TOKEN"  # env beats file, like dotenv


def test_settings_repr_and_str_never_expose_token():
    settings = Settings(telegram_bot_token="SUPER-SECRET-TOKEN-VALUE")
    for rendered in (repr(settings), str(settings)):
        assert "SUPER-SECRET" not in rendered


def test_config_error_message_names_the_variable_not_the_value(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(ConfigError) as excinfo:
        load_settings(_env_file(tmp_path))
    assert "TELEGRAM_BOT_TOKEN" in str(excinfo.value)
    assert ".env" in str(excinfo.value)