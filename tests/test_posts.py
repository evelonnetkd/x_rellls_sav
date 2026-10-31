import httpx
import pytest

from reels_bot.posts import PostClient, PostError


def status(**overrides):
    return {
        "type": "status",
        "id": "123",
        "text": "Текст <поста> & ссылка",
        "url": "https://evil.example/tracking",
        "author": {"name": "Алиса", "screen_name": "alice"},
        **overrides,
    }


async def fetch_payload(payload, code=200):
    def handler(request):
        assert str(request.url) == "https://api.fxtwitter.com/2/status/123"
        return httpx.Response(code, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await PostClient(client).fetch("123")


@pytest.mark.asyncio
async def test_text_post_and_canonical_origin():
    post = await fetch_payload({"code": 200, "status": status()})
    assert post.text == "Текст <поста> & ссылка"
    assert post.author == "Алиса"
    assert post.handle == "alice"
    assert post.url == "https://x.com/alice/status/123"
    assert post.photos == []
    assert not post.has_video


@pytest.mark.asyncio
async def test_photo_post_deduplicates_media():
    photo = {"type": "photo", "url": "https://pbs.twimg.com/media/abc.jpg?name=orig"}
    post = await fetch_payload(
        {"code": 200, "status": status(media={"photos": [photo], "all": [photo]})}
    )
    assert post.photos == [photo["url"]]
    assert not post.has_video


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "media",
    [
        {"videos": [{"type": "video", "url": "https://video.twimg.com/abc.mp4"}]},
        {"all": [{"type": "gif", "url": "https://video.twimg.com/abc.mp4"}]},
        {"photos": [{"type": "gif", "url": "https://pbs.twimg.com/media/abc.gif"}]},
    ],
)
async def test_video_and_gif(media):
    post = await fetch_payload({"code": 200, "status": status(media=media)})
    assert post.has_video
    assert post.photos == []


@pytest.mark.asyncio
async def test_quote_includes_author_text_and_media():
    quote = status(
        id="456",
        text="Цитируемый текст",
        author={"name": "Боб", "screen_name": "bob"},
        media={
            "photos": [{"url": "https://pbs.twimg.com/media/quoted.jpg"}],
            "videos": [{"type": "video"}],
        },
    )
    post = await fetch_payload({"code": 200, "status": status(quote=quote)})
    assert post.quote_text == "Боб:\nЦитируемый текст"
    assert post.photos == ["https://pbs.twimg.com/media/quoted.jpg"]
    assert post.has_video


@pytest.mark.asyncio
async def test_unavailable_quote_does_not_hide_parent():
    post = await fetch_payload(
        {"code": 200, "status": status(quote={"type": "tombstone", "reason": "private"})}
    )
    assert post.text
    assert not post.quote_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://pbs.twimg.com/media/abc.jpg",
        "https://pbs.twimg.com.evil.com/media/abc.jpg",
        "https://evil.com/media/abc.jpg",
        "https://user@pbs.twimg.com/media/abc.jpg",
        "https://pbs.twimg.com:8080/media/abc.jpg",
        "https://pbs.twimg.com/profile_images/abc.jpg",
        "https://pbs.twimg.com\\@evil.com/media/abc.jpg",
        "https://127.0.0.1/media/abc.jpg",
        "https://pbs.twimg.com/media/abc.jpg\n",
        "https://pbs.twimg.com:bad/media/abc.jpg",
    ],
)
async def test_unsafe_photo_urls_are_ignored(url):
    post = await fetch_payload({"code": 200, "status": status(media={"photos": [{"url": url}]})})
    assert post.photos == []


@pytest.mark.asyncio
async def test_unsafe_handle_falls_back_to_id_url():
    post = await fetch_payload(
        {"code": 200, "status": status(author={"name": "Автор", "screen_name": "../../evil"})}
    )
    assert post.url == "https://x.com/i/status/123"
    assert post.handle == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "code", "message"),
    [
        ({"code": 429}, 200, "ограничил"),
        ({}, 429, "ограничил"),
        ({"code": 404}, 200, "недоступна"),
        ({}, 403, "отклонил"),
        ({"code": 200, "status": {"type": "tombstone", "reason": "deleted"}}, 200, "недоступна"),
        ({"code": 200, "status": status(id="456")}, 200, "Не удалось"),
        ({"code": 200, "status": status(text=None)}, 200, "Не удалось"),
        ({"code": 200, "status": []}, 200, "Не удалось"),
        ({"code": {}}, 200, "Не удалось"),
        ([], 200, "Не удалось"),
        ({"secret": "internal trace"}, 500, "Не удалось"),
    ],
)
async def test_safe_upstream_errors(payload, code, message):
    with pytest.raises(PostError, match=message) as error:
        await fetch_payload(payload, code)
    assert "internal trace" not in str(error.value)


@pytest.mark.asyncio
async def test_malformed_json():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="not json"))
    ) as client:
        with pytest.raises(PostError, match="Не удалось"):
            await PostClient(client).fetch("123")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception", "message"),
    [
        (httpx.ReadTimeout("internal details"), "не ответил вовремя"),
        (httpx.ConnectError("internal details"), "Не удалось связаться"),
    ],
)
async def test_safe_network_errors(exception, message):
    def handler(request):
        raise exception

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PostError, match=message) as error:
            await PostClient(client).fetch("123")
    assert "internal details" not in str(error.value)


@pytest.mark.asyncio
async def test_no_redirects_to_provider_urls():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example/"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(PostError):
            await PostClient(client).fetch("123")
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_bad_post_id_makes_no_request():
    def handler(request):
        pytest.fail("Invalid IDs must never reach the network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PostError):
            await PostClient(client).fetch("123/../../other")
