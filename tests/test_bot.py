from datetime import UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import Chat, Message, MessageEntity
from telegram.error import BadRequest

from reels_bot.bot import BotHandlers
from reels_bot.config import Settings
from reels_bot.download import DownloadedVideo, DownloadError
from reels_bot.links import parse_link
from reels_bot.posts import Post


@pytest.fixture(autouse=True)
def isolated_workspace(monkeypatch, tmp_path):
    from reels_bot.storage import DownloadWorkspace

    monkeypatch.setattr(
        "reels_bot.bot.DownloadWorkspace", lambda: DownloadWorkspace(tmp_path / "downloads")
    )


def make_handlers(ids=frozenset({123})):
    return BotHandlers(
        Settings("123:fake", ids), Mock(download=AsyncMock()), Mock(fetch=AsyncMock())
    )


def update(user=123, chat_type="private", text="https://www.instagram.com/reel/abc/"):
    message = Mock(text=text, caption=None)
    message.parse_entities.return_value = {}
    message.parse_caption_entities.return_value = {}
    message.reply_text = AsyncMock()
    message.reply_text.return_value = Mock(edit_text=AsyncMock(), delete=AsyncMock())
    message.reply_video = AsyncMock()
    message.reply_document = AsyncMock()
    message.reply_photo = AsyncMock()
    message.reply_media_group = AsyncMock()
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user),
        effective_chat=SimpleNamespace(type=chat_type),
        effective_message=message,
    )


@pytest.mark.parametrize(
    "ids,user,chat_type",
    [
        (frozenset(), 123, "private"),
        (frozenset({123}), 999, "private"),
        (frozenset({123}), 123, "group"),
        (frozenset({123}), 123, "supergroup"),
    ],
)
async def test_denied_users_never_touch_network_or_download(ids, user, chat_type):
    handlers = make_handlers(ids)
    await handlers.receive(update(user=user, chat_type=chat_type), None)
    handlers.downloader.download.assert_not_awaited()
    handlers.posts.fetch.assert_not_awaited()


async def test_id_available_for_bootstrap():
    handlers = make_handlers(frozenset())
    event = update(user=999)
    await handlers.identify(event, None)
    event.effective_message.reply_text.assert_awaited_once_with("Твой Telegram ID: 999")


async def test_duplicate_user_gets_busy_message():
    handlers = make_handlers()
    handlers.active_users.add(123)
    event = update()
    await handlers.receive(event, None)
    handlers.downloader.download.assert_not_awaited()
    assert "предыдущие" in event.effective_message.reply_text.call_args.args[0]


async def test_video_upload_cleanup_and_fallback_to_document():
    handlers = make_handlers()
    paths = []

    async def download(url, directory):
        path = directory / "video.mp4"
        path.write_bytes(b"fake media")
        paths.append(path)
        return [DownloadedVideo(path)]

    handlers.downloader.download.side_effect = download
    event = update()
    event.effective_message.reply_video.side_effect = BadRequest("unsupported video")
    await handlers.receive(event, None)
    event.effective_message.reply_video.assert_awaited_once()
    event.effective_message.reply_document.assert_awaited_once()
    assert not paths[0].parent.exists()
    assert not handlers.active_users


async def test_download_failure_cleans_temp_and_releases_user():
    handlers = make_handlers()
    paths = []

    async def download(url, directory):
        paths.append(directory)
        raise DownloadError("Не удалось скачать видео.")

    handlers.downloader.download.side_effect = download
    await handlers.receive(update(), None)
    assert not paths[0].exists()
    assert not handlers.active_users


async def test_each_sent_video_deleted_before_next_upload():
    handlers = make_handlers()
    paths = []

    async def download(url, directory):
        paths.extend([directory / "01.mp4", directory / "02.mp4"])
        for path in paths:
            path.write_bytes(b"fake media")
        (directory / "download.part").write_bytes(b"temporary fragment")
        return [DownloadedVideo(path) for path in paths]

    sent = 0

    async def upload(media, **kwargs):
        nonlocal sent
        if sent:
            assert not paths[0].exists()
        assert paths[sent].exists()
        sent += 1

    handlers.downloader.download.side_effect = download
    event = update()
    event.effective_message.reply_video.side_effect = upload
    await handlers.receive(event, None)
    assert sent == 2
    assert not paths[0].parent.exists()


async def test_cancelled_upload_cleans_downloads_and_releases_user():
    import asyncio

    handlers = make_handlers()
    paths = []

    async def download(url, directory):
        path = directory / "01.mp4"
        path.write_bytes(b"fake media")
        paths.append(path)
        return [DownloadedVideo(path)]

    handlers.downloader.download.side_effect = download
    event = update()
    event.effective_message.reply_video.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await handlers.receive(event, None)
    assert not paths[0].parent.exists()
    assert not handlers.active_users


async def test_x_text_post_does_not_start_video_download():
    handlers = make_handlers()
    handlers.posts.fetch.return_value = Post(
        author="Автор",
        handle="author",
        text="Текст поста",
        url="https://x.com/author/status/123",
        photos=["https://pbs.twimg.com/media/photo.jpg"],
        has_video=False,
    )
    event = update(text="https://x.com/author/status/123")
    await handlers.receive(event, None)
    handlers.posts.fetch.assert_awaited_once_with("123")
    handlers.downloader.download.assert_not_awaited()
    event.effective_message.reply_photo.assert_awaited_once()
    assert "Текст поста" in event.effective_message.reply_photo.call_args.kwargs["caption"]
    assert (
        "https://x.com/author/status/123"
        in (event.effective_message.reply_photo.call_args.kwargs["caption"])
    )


async def test_x_photos_are_one_album_with_post_caption():
    handlers = make_handlers()
    message = update().effective_message
    photos = [f"https://pbs.twimg.com/media/{n}.jpg" for n in range(3)]
    await handlers.send_post(
        message, Post(text="Альбом", photos=photos, url="https://x.com/i/status/1")
    )
    media = message.reply_media_group.call_args.args[0]
    assert [item.media for item in media] == photos
    assert "Альбом" in media[0].caption
    assert all(item.caption is None for item in media[1:])
    message.reply_photo.assert_not_awaited()
    message.reply_text.assert_not_awaited()


async def test_long_emoji_post_keeps_complete_text_and_short_photo_caption():
    handlers = make_handlers()
    message = update().effective_message
    body = "😀" * 600
    await handlers.send_post(
        message,
        Post(
            author="Автор",
            text=body,
            url="https://x.com/i/status/1",
            photos=["https://pbs.twimg.com/media/1.jpg"],
        ),
    )
    caption = message.reply_photo.call_args.kwargs["caption"]
    assert len(caption.encode("utf-16-le")) // 2 <= 1024
    assert "https://x.com/i/status/1" in caption
    assert body in "".join(call.args[0] for call in message.reply_text.call_args_list)


async def test_rejected_album_falls_back_to_individual_photos():
    handlers = make_handlers()
    message = update().effective_message
    message.reply_media_group.side_effect = BadRequest("bad media")
    message.reply_photo.side_effect = [BadRequest("bad photo"), None]
    await handlers.send_post(
        message,
        Post(
            text="Пост",
            photos=["https://pbs.twimg.com/media/1.jpg", "https://pbs.twimg.com/media/2.jpg"],
        ),
    )
    assert message.reply_photo.await_count == 2
    assert "Пост" in message.reply_photo.call_args_list[1].kwargs["caption"]


async def test_rejected_photo_preserves_post_text():
    handlers = make_handlers()
    message = update().effective_message
    message.reply_photo.side_effect = BadRequest("bad photo")
    await handlers.send_post(
        message,
        Post(
            text="Текст не потерян",
            photos=["https://pbs.twimg.com/media/1.jpg"],
        ),
    )
    assert "Текст не потерян" in message.reply_text.call_args.args[0]


async def test_multiple_links_continue_after_failed_download():
    handlers = make_handlers()
    handlers.downloader.download.side_effect = DownloadError("Ошибка загрузки.")
    event = update(text="https://instagram.com/reel/abc/ https://instagram.com/reel/def/")
    await handlers.receive(event, None)
    assert handlers.downloader.download.await_count == 2


def test_hidden_text_link():
    from datetime import datetime

    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(123, "private"),
        text="Смотри",
        entities=[MessageEntity(MessageEntity.TEXT_LINK, 0, 6, url="https://x.com/u/status/123")],
    )
    assert BotHandlers.message_links(message) == [parse_link("https://x.com/u/status/123")]
