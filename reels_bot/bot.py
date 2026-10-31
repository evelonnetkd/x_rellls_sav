from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

import httpx
from telegram import BotCommand, InputMediaPhoto, LinkPreviewOptions, Message, MessageEntity, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .config import Settings
from .download import Downloader, DownloadError
from .links import Link, LinkError, extract_links
from .posts import Post, PostClient, PostError
from .storage import DownloadWorkspace

logger = logging.getLogger(__name__)
HELP = (
    "Пришли ссылку на Instagram Reels, TikTok или пост X/Twitter.\n\n"
    "Видео пришлю файлом — его можно переслать друзьям. "
    "У постов X покажу также текст и фото. Можно переслать сообщение со ссылкой.\n\n"
    "До 5 ссылок в сообщении, до 4 видео в посте.\n"
    "/id — твой Telegram ID\n/help — эта подсказка"
)


class BotHandlers:
    def __init__(self, settings: Settings, downloader: Downloader, posts: PostClient):
        self.settings = settings
        self.downloader = downloader
        self.posts = posts
        self.workspace = DownloadWorkspace()
        self.active_users: set[int] = set()
        self.slots = asyncio.Semaphore(2)

    def is_allowed(self, update: Update) -> bool:
        return bool(
            update.effective_chat
            and update.effective_chat.type == "private"
            and update.effective_user
            and update.effective_user.id in self.settings.allowed_user_ids
        )

    async def identify(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if (
            update.effective_chat
            and update.effective_chat.type == "private"
            and update.effective_message
            and update.effective_user
        ):
            await update.effective_message.reply_text(
                f"Твой Telegram ID: {update.effective_user.id}"
            )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if not message or not update.effective_chat or update.effective_chat.type != "private":
            return
        if not self.is_allowed(update):
            await message.reply_text("Доступ закрыт. /id покажет твой ID для списка доступа.")
            return
        await message.reply_text(HELP)

    @staticmethod
    def message_links(message: Message) -> list[Link]:
        text = message.text or message.caption or ""
        # Hidden text links need their actual target, not the displayed label.
        entities = message.parse_entities(types=[MessageEntity.TEXT_LINK])
        entities.update(message.parse_caption_entities(types=[MessageEntity.TEXT_LINK]))
        text += "\n" + "\n".join(entity.url for entity in entities if entity.url)
        return extract_links(text)

    async def receive(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if not message or not update.effective_chat or update.effective_chat.type != "private":
            return
        if not self.is_allowed(update):
            await message.reply_text("Доступ закрыт. /id покажет твой ID для списка доступа.")
            return
        try:
            links = self.message_links(message)
        except LinkError as error:
            await message.reply_text(str(error))
            return
        if len(links) > 5:
            await message.reply_text("Отправь не больше 5 ссылок за раз.")
            return
        user_id = update.effective_user.id
        if user_id in self.active_users:
            await message.reply_text("Ещё обрабатываю предыдущие ссылки. Подожди завершения.")
            return
        self.active_users.add(user_id)
        try:
            status = await message.reply_text("Принял, жду очередь…")
            async with self.slots:
                for link in links:
                    await self.process_link(message, status, link)
            with suppress(TelegramError):
                await status.delete()
        finally:
            self.active_users.discard(user_id)

    @staticmethod
    async def set_status(status: Message, text: str) -> None:
        with suppress(TelegramError):
            await status.edit_text(text)

    async def send_post(self, message: Message, post: Post) -> None:
        author = f"{post.author} (@{post.handle})" if post.handle else post.author
        text = f"{author}\n\n{post.text or '[Пост без текста]'}"
        if post.quote_text:
            text += f"\n\nЦитата:\n{post.quote_text}"
        text += f"\n\n{post.url}"
        if post.photos:
            # Telegram photo captions have a smaller limit than text messages.
            # Keep long posts intact in a follow-up, with author/source on the photo.
            fits_caption = len(text.encode("utf-16-le")) // 2 <= 1024
            caption = text if fits_caption else f"{author}\n\n{post.url}"
            if len(caption.encode("utf-16-le")) // 2 > 1024:
                caption = post.url
            attached = await self.send_post_photos(message, post.photos[:4], caption)
            if attached and fits_caption:
                return
        # At most 3600 UTF-16 units, including posts made entirely of emoji.
        for offset in range(0, len(text), 1800):
            await message.reply_text(
                text[offset : offset + 1800],
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )

    @staticmethod
    async def send_post_photos(message: Message, photos: list[str], caption: str) -> bool:
        if len(photos) > 1:
            try:
                await message.reply_media_group(
                    [
                        InputMediaPhoto(
                            photo,
                            caption=caption if index == 0 else None,
                            show_caption_above_media=index == 0,
                        )
                        for index, photo in enumerate(photos)
                    ],
                    read_timeout=60,
                    write_timeout=60,
                )
                return True
            except BadRequest:
                # A rejected item can invalidate an album. Try its photos individually.
                pass
        attached = False
        for photo in photos:
            try:
                await message.reply_photo(
                    photo,
                    caption=None if attached else caption,
                    show_caption_above_media=not attached,
                    read_timeout=60,
                    write_timeout=60,
                )
                attached = True
            except BadRequest:
                await message.reply_text(
                    f"Не удалось прикрепить фото: {photo}",
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
        return attached

    async def process_link(self, message: Message, status: Message, link: Link) -> None:
        try:
            if link.platform == "twitter":
                await self.set_status(status, "Получаю пост X…")
                try:
                    post = await self.posts.fetch(link.post_id)
                except PostError as error:
                    await message.reply_text(f"{error}\nПопробую отдельно загрузить видео.")
                else:
                    await self.send_post(message, post)
                    if not post.has_video:
                        return
            await self.set_status(status, "Скачиваю видео…")
            # Directory lives through upload and is removed on every exit path.
            with self.workspace.request() as directory:
                videos = await self.downloader.download(link.url, directory)
                await self.set_status(status, "Отправляю видео…")
                for video in videos:
                    caption = f"Источник: {link.url}"
                    with video.path.open("rb") as media:
                        if video.path.suffix.lower() == ".mp4":
                            try:
                                await message.reply_video(
                                    media,
                                    caption=caption,
                                    supports_streaming=True,
                                    read_timeout=180,
                                    write_timeout=180,
                                    connect_timeout=30,
                                )
                            except BadRequest:
                                media.seek(0)
                                await message.reply_document(
                                    media,
                                    filename=video.path.name,
                                    caption=caption,
                                    read_timeout=180,
                                    write_timeout=180,
                                    connect_timeout=30,
                                )
                        else:
                            await message.reply_document(
                                media,
                                filename=video.path.name,
                                caption=caption,
                                read_timeout=180,
                                write_timeout=180,
                                connect_timeout=30,
                            )
                    # Telegram confirmed receipt; release disk space before
                    # uploading the next video from a multi-video post.
                    video.path.unlink(missing_ok=True)
        except DownloadError as error:
            await message.reply_text(f"{error}\n{link.url}")
        except TelegramError:
            await message.reply_text(
                "Telegram не принял сообщение или файл. Попробуй ещё раз чуть позже."
            )
        except Exception as error:
            # Exception strings can contain upstream URLs and authentication data.
            logger.error("Link processing failed: %s", type(error).__name__)
            await message.reply_text("Не получилось обработать ссылку. Попробуй ещё раз позже.")


def build_application(settings: Settings) -> Application:
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(20, connect=10),
        headers={"User-Agent": "ReelsSavPersonalBot/0.1 (private Telegram reader)"},
        follow_redirects=False,
    )
    handlers = BotHandlers(
        settings,
        Downloader(
            max_bytes=settings.max_bytes,
            timeout=settings.download_timeout,
            cookies_file=settings.cookies_file,
        ),
        PostClient(client),
    )
    removed = handlers.workspace.cleanup_abandoned()
    if removed:
        logger.info("Removed %d abandoned download directories", removed)

    async def initialize(application: Application) -> None:
        await application.bot.set_my_commands(
            [
                BotCommand("start", "Как пользоваться"),
                BotCommand("help", "Помощь"),
                BotCommand("id", "Мой Telegram ID"),
            ]
        )

    async def shutdown(application: Application) -> None:
        await client.aclose()

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Update failed: %s", type(context.error).__name__)

    app = (
        Application.builder()
        .token(settings.token)
        .concurrent_updates(8)
        .post_init(initialize)
        .post_shutdown(shutdown)
        .build()
    )
    app.add_handler(CommandHandler("id", handlers.identify))
    app.add_handler(CommandHandler(["start", "help"], handlers.help))
    app.add_handler(
        MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handlers.receive)
    )
    app.add_error_handler(on_error)
    return app
