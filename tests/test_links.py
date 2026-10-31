import pytest

from reels_bot.links import LinkError, extract_links, parse_link


@pytest.mark.parametrize(
    ("value", "url", "platform", "post_id"),
    [
        (
            "http://twitter.com/alice/status/123?s=20",
            "https://x.com/alice/status/123",
            "twitter",
            "123",
        ),
        (
            "https://www.x.com/alice/status/123/photo/1",
            "https://x.com/alice/status/123",
            "twitter",
            "123",
        ),
        (
            "https://fxtwitter.com/alice/status/123/",
            "https://x.com/alice/status/123",
            "twitter",
            "123",
        ),
        (
            "https://fixupx.com/i/web/status/123#fragment",
            "https://x.com/i/status/123",
            "twitter",
            "123",
        ),
        ("https://x.com/i/status/123", "https://x.com/i/status/123", "twitter", "123"),
        (
            "INSTAGRAM.COM/reels/Ab-C_1/?igsh=tracking",
            "https://www.instagram.com/reel/Ab-C_1/",
            "instagram",
            "Ab-C_1",
        ),
        (
            "https://www.instagram.com/p/AbC/?img_index=2",
            "https://www.instagram.com/p/AbC/",
            "instagram",
            "AbC",
        ),
        (
            "https://m.tiktok.com/@some.user/video/12345?_t=abc",
            "https://www.tiktok.com/@some.user/video/12345",
            "tiktok",
            "12345",
        ),
        (
            "https://vm.tiktok.com/AbC12/?is_from_webapp=1",
            "https://vm.tiktok.com/AbC12/",
            "tiktok",
            None,
        ),
        ("https://vt.tiktok.com/AbC12", "https://vt.tiktok.com/AbC12/", "tiktok", None),
        ("https://www.tiktok.com/t/AbC12/", "https://www.tiktok.com/t/AbC12/", "tiktok", None),
        ("https://x.com:443/alice/status/123", "https://x.com/alice/status/123", "twitter", "123"),
    ],
)
def test_supported_links(value, url, platform, post_id):
    link = parse_link(value)
    assert (link.url, link.platform, link.post_id) == (url, platform, post_id)


@pytest.mark.parametrize(
    "value",
    [
        "https://x.com/alice",
        "https://instagram.com/alice/",
        "https://tiktok.com/@alice",
        "https://evil.com/alice/status/123",
        "https://x.com.evil.com/alice/status/123",
        "https://evil.x.com/alice/status/123",
        "https://x.com@evil.com/alice/status/123",
        "https://alice@x.com/alice/status/123",
        "https://x.com:8443/alice/status/123",
        "ftp://x.com/alice/status/123",
        "file:///x.com/alice/status/123",
        "https://127.0.0.1/alice/status/123",
        "https://[::1]/alice/status/123",
        "https://х.com/alice/status/123",
        "https://x.com./alice/status/123",
        "https://x.com/alice/status/abc",
        "https://x.com/alice/status/123/extra",
        "https://x.com/alice/status/%31%32%33",
        "https://x.com\\@evil.com/alice/status/123",
        "https://x.com/\nalice/status/123",
        "https://x.com:bad/alice/status/123",
        "https://x.com/alice/status/123 https://x.com/bob/status/456",
        "",
        "https://www.tiktok.com/redirect?url=https://evil.com",
        "https://vm.tiktok.com/",
    ],
)
def test_rejects_unsupported_and_unsafe_urls(value):
    with pytest.raises(LinkError):
        parse_link(value)


def test_forwarded_caption_and_deduplication():
    links = extract_links(
        "Переслано от друга: (https://twitter.com/alice/status/123?s=20).\n"
        "Тот же пост https://x.com/alice/status/123/video/1!\n"
        "И ролик instagram.com/reel/AbC/; https://example.com/ignore"
    )
    assert [link.url for link in links] == [
        "https://x.com/alice/status/123",
        "https://www.instagram.com/reel/AbC/",
    ]


@pytest.mark.parametrize(
    "value",
    [
        "Без ссылки",
        "evil.x.com/alice/status/123",
        "evil-x.com/alice/status/123",
        "https://evil.com/https://x.com/alice/status/123",
        "https://x.com.evil.com/alice/status/123",
        "ftp://x.com/alice/status/123",
    ],
)
def test_extract_does_not_rescue_invalid_links(value):
    with pytest.raises(LinkError):
        extract_links(value)
