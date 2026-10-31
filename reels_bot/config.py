from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    allowed_user_ids: frozenset[int]
    cookies_file: Path | None = None
    download_timeout: int = 180
    max_bytes: int = 49_000_000

    @classmethod
    def from_env(cls) -> Settings:
        # Read only the project's .env, never an ancestor directory's configuration.
        load_dotenv(Path.cwd() / ".env", override=False)
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
            raise ConfigError("Заполни TELEGRAM_BOT_TOKEN в .env токеном из @BotFather.")
        try:
            ids = frozenset(
                int(item.strip())
                for item in os.environ.get("ALLOWED_USER_IDS", "").split(",")
                if item.strip()
            )
            if any(user_id <= 0 for user_id in ids):
                raise ValueError
        except ValueError:
            raise ConfigError(
                "ALLOWED_USER_IDS: нужны положительные числовые ID через запятую."
            ) from None
        try:
            timeout = int(os.environ.get("DOWNLOAD_TIMEOUT", "180"))
            max_mb = int(os.environ.get("MAX_FILE_MB", "49"))
            if not 10 <= timeout <= 600 or not 1 <= max_mb <= 49:
                raise ValueError
        except ValueError:
            raise ConfigError("DOWNLOAD_TIMEOUT: 10–600 секунд; MAX_FILE_MB: 1–49.") from None
        cookies_value = os.environ.get("COOKIES_FILE", "").strip()
        cookies = Path(cookies_value).expanduser().resolve() if cookies_value else None
        if cookies is not None and not cookies.is_file():
            raise ConfigError("COOKIES_FILE указывает на несуществующий файл.")
        return cls(token, ids, cookies, timeout, max_mb * 1_000_000)
