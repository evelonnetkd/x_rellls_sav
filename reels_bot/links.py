"""Recognize public post links without following user-controlled redirects."""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


class LinkError(ValueError):
    """A message suitable for showing to the bot user."""


@dataclass(frozen=True)
class Link:
    url: str
    platform: str
    post_id: str | None = None


_X_HOSTS = {
    "x.com",
    "www.x.com",
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
    "fxtwitter.com",
    "www.fxtwitter.com",
    "fixupx.com",
    "www.fixupx.com",
}
_INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
_TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}
_SHORT_TIKTOK_HOSTS = {"vm.tiktok.com", "vt.tiktok.com"}
_HOSTS = _X_HOSTS | _INSTAGRAM_HOSTS | _TIKTOK_HOSTS | _SHORT_TIKTOK_HOSTS
_STATUS = re.compile(
    r"/(?:([A-Za-z0-9_]{1,15})/status|i/web/status)/([0-9]{1,20})"
    r"(?:/(?:photo|video)/[1-9][0-9]*)?/?"
)
_INSTAGRAM = re.compile(r"/(reel|reels|p)/([A-Za-z0-9_-]{1,100})/?")
_TIKTOK_VIDEO = re.compile(r"/@([A-Za-z0-9_.]{1,50})/video/([0-9]{1,30})/?")
_TIKTOK_SHORT = re.compile(r"/([A-Za-z0-9_-]{1,100})/?")
_TIKTOK_T = re.compile(r"/t/([A-Za-z0-9_-]{1,100})/?")
_BARE_HOSTS = "|".join(re.escape(host) for host in sorted(_HOSTS, key=len, reverse=True))
_CANDIDATE = re.compile(
    r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"']+"
    r"|(?<![\w@./-])(?:" + _BARE_HOSTS + r")/[^\s<>\"']+",
    re.IGNORECASE,
)
_INVALID = "Пришлите ссылку на публикацию Instagram, TikTok или X."


def parse_link(value: str) -> Link:
    """Validate one URL and remove tracking parameters and media selectors."""
    if not isinstance(value, str):
        raise LinkError(_INVALID)
    value = value.strip()
    if not value or any(char.isspace() or ord(char) < 32 for char in value):
        raise LinkError(_INVALID)
    if "\\" in value:
        raise LinkError(_INVALID)
    if "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise LinkError(_INVALID) from None
    if (
        parts.scheme.lower() not in {"http", "https"}
        or host not in _HOSTS
        or parts.username is not None
        or parts.password is not None
        or port not in {None, 443}
    ):
        raise LinkError(_INVALID)

    if host in _X_HOSTS:
        match = _STATUS.fullmatch(parts.path)
        if match:
            handle, post_id = match.groups()
            handle = handle or "i"
            return Link(f"https://x.com/{handle}/status/{post_id}", "twitter", post_id)
    elif host in _INSTAGRAM_HOSTS:
        match = _INSTAGRAM.fullmatch(parts.path)
        if match:
            kind, post_id = match.groups()
            kind = "reel" if kind == "reels" else kind
            return Link(f"https://www.instagram.com/{kind}/{post_id}/", "instagram", post_id)
    elif host in _SHORT_TIKTOK_HOSTS:
        match = _TIKTOK_SHORT.fullmatch(parts.path)
        if match:
            return Link(f"https://{host}/{match[1]}/", "tiktok")
    elif host in _TIKTOK_HOSTS:
        match = _TIKTOK_VIDEO.fullmatch(parts.path)
        if match:
            handle, post_id = match.groups()
            return Link(f"https://www.tiktok.com/@{handle}/video/{post_id}", "tiktok", post_id)
        match = _TIKTOK_T.fullmatch(parts.path)
        if match:
            return Link(f"https://www.tiktok.com/t/{match[1]}/", "tiktok")
    raise LinkError(_INVALID)


def extract_links(text: str) -> list[Link]:
    """Find and de-duplicate supported links in a forwarded message or caption."""
    links: dict[str, Link] = {}
    for candidate in _CANDIDATE.finditer(text or ""):
        value = candidate[0].rstrip(".,;:!?)]}»”’")
        try:
            link = parse_link(value)
        except LinkError:
            continue
        links.setdefault(link.url, link)
    if not links:
        raise LinkError(_INVALID)
    return list(links.values())
