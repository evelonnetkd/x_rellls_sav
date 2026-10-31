"""Fetch individual public X posts through the public FxEmbed API."""

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx


class PostError(Exception):
    """A safe explanation intended for the user, without upstream details."""


@dataclass
class Post:
    author: str = "Автор X"
    handle: str = ""
    text: str = ""
    url: str = ""
    photos: list[str] = field(default_factory=list)
    has_video: bool = False
    quote_text: str = ""


_POST_ID = re.compile(r"[0-9]{1,20}")
_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")
_UNAVAILABLE = "Публикация недоступна: она удалена или доступ к ней ограничен."
_INVALID_RESPONSE = "Не удалось получить публикацию X. Попробуйте позже."
_RATE_LIMIT = "Сервис X временно ограничил запросы. Попробуйте позже."


def _string(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _photo_url(value: object) -> str | None:
    if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value):
        return None
    if "\\" in value:
        return None
    try:
        parts = urlsplit(value)
        valid = (
            parts.scheme == "https"
            and parts.hostname == "pbs.twimg.com"
            and parts.username is None
            and parts.password is None
            and parts.port in {None, 443}
            and parts.path.startswith("/media/")
        )
    except ValueError:
        return None
    return value if valid else None


def _media(status: dict) -> tuple[list[str], bool]:
    media = status.get("media")
    if not isinstance(media, dict):
        return [], False
    photos: dict[str, None] = {}
    has_video = False
    for key in ("photos", "videos", "all"):
        entries = media.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("type")
            if key == "videos" or kind in ("video", "gif", "animated_gif"):
                has_video = True
            if (
                key == "photos" and kind not in ("video", "gif", "animated_gif")
            ) or kind == "photo":
                url = _photo_url(entry.get("url"))
                if url:
                    photos.setdefault(url, None)
    return list(photos), has_video


def _parse_status(status: object, post_id: str) -> Post:
    if not isinstance(status, dict):
        raise PostError(_INVALID_RESPONSE)
    if status.get("type") == "tombstone":
        raise PostError(_UNAVAILABLE)
    if _string(status.get("id")) != post_id or not isinstance(status.get("text"), str):
        raise PostError(_INVALID_RESPONSE)
    author = status.get("author")
    if not isinstance(author, dict):
        raise PostError(_INVALID_RESPONSE)
    handle = _string(author.get("screen_name"))
    if not _HANDLE.fullmatch(handle):
        handle = ""
    photos, has_video = _media(status)
    quote_text = ""
    quote = status.get("quote")
    if isinstance(quote, dict) and quote.get("type") != "tombstone":
        text = _string(quote.get("text"))
        quoted_author = quote.get("author")
        if text:
            name = (
                _string(quoted_author.get("name"), "Автор X")
                if isinstance(quoted_author, dict)
                else "Автор X"
            )
            quote_text = f"{name}:\n{text}"
        quote_photos, quote_video = _media(quote)
        photos = list(dict.fromkeys([*photos, *quote_photos]))
        has_video = has_video or quote_video
    return Post(
        author=_string(author.get("name")) or handle or "Автор X",
        handle=handle,
        text=status["text"],
        url=f"https://x.com/{handle or 'i'}/status/{post_id}",
        photos=photos,
        has_video=has_video,
        quote_text=quote_text,
    )


class PostClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def fetch(self, post_id: str) -> Post:
        if not isinstance(post_id, str) or not _POST_ID.fullmatch(post_id):
            raise PostError("Некорректная ссылка на публикацию X.")
        try:
            response = await self.client.get(
                f"https://api.fxtwitter.com/2/status/{post_id}",
                timeout=20.0,
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise PostError("Сервис X не ответил вовремя. Попробуйте позже.") from None
        except httpx.RequestError:
            raise PostError("Не удалось связаться с сервисом X. Попробуйте позже.") from None
        if response.status_code == 429:
            raise PostError(_RATE_LIMIT)
        if response.status_code == 403:
            raise PostError("Сервис чтения X отклонил запрос. Попробуйте позже.")
        if response.status_code in {401, 404, 410}:
            raise PostError(_UNAVAILABLE)
        if not response.is_success:
            raise PostError(_INVALID_RESPONSE)
        try:
            payload = response.json()
        except ValueError:
            raise PostError(_INVALID_RESPONSE) from None
        if not isinstance(payload, dict):
            raise PostError(_INVALID_RESPONSE)
        code = payload.get("code")
        if code == 429:
            raise PostError(_RATE_LIMIT)
        if code in (401, 403, 404, 410):
            raise PostError(_UNAVAILABLE)
        if code != 200:
            raise PostError(_INVALID_RESPONSE)
        return _parse_status(payload.get("status"), post_id)
