import pytest

from reels_bot.config import ConfigError, Settings


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr("reels_bot.config.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:fake_test_token")
    monkeypatch.setenv("ALLOWED_USER_IDS", "")
    monkeypatch.delenv("COOKIES_FILE", raising=False)
    monkeypatch.delenv("DOWNLOAD_TIMEOUT", raising=False)
    monkeypatch.delenv("MAX_FILE_MB", raising=False)


def test_empty_allowlist_is_closed():
    assert Settings.from_env().allowed_user_ids == frozenset()


def test_allowlist_and_secret_repr(monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", "123, 456,123")
    settings = Settings.from_env()
    assert settings.allowed_user_ids == {123, 456}
    assert settings.token not in repr(settings)


@pytest.mark.parametrize("value", ["-1", "0", "123,hello"])
def test_invalid_user_ids(monkeypatch, value):
    monkeypatch.setenv("ALLOWED_USER_IDS", value)
    with pytest.raises(ConfigError):
        Settings.from_env()


@pytest.mark.parametrize(
    "name,value",
    [
        ("TELEGRAM_BOT_TOKEN", ""),
        ("DOWNLOAD_TIMEOUT", "0"),
        ("MAX_FILE_MB", "50"),
        ("COOKIES_FILE", "/missing/cookies.txt"),
    ],
)
def test_invalid_configuration(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError):
        Settings.from_env()
