import argparse
import logging
import shutil
from importlib.metadata import version

from .config import ConfigError, Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Личный Telegram-бот для видео и постов X")
    parser.add_argument("--check", action="store_true", help="Проверить настройки без сети")
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
    except ConfigError as error:
        parser.exit(2, f"Ошибка настройки: {error}\n")
    if args.check:
        print(f"Настройки корректны. Разрешённых пользователей: {len(settings.allowed_user_ids)}")
        print(f"yt-dlp: {version('yt-dlp')}")
        print(f"curl-cffi (запросы TikTok в режиме браузера): {version('curl-cffi')}")
        print(f"ffmpeg: {'найден' if shutil.which('ffmpeg') else 'не найден (необязателен)'}")
        if not settings.allowed_user_ids:
            print("Загрузки закрыты всем. Узнай свой ID через /id и заполни ALLOWED_USER_IDS.")
        return
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    from .bot import build_application

    app = build_application(settings)
    print("Бот запускается. Остановка: Ctrl+C.")
    if not settings.allowed_user_ids:
        print("Доступ к загрузкам закрыт. Отправь /id боту и заполни ALLOWED_USER_IDS в .env.")
    app.run_polling(allowed_updates=["message"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
