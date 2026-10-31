import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from reels_bot.download import _FORMAT as DOWNLOAD_FORMAT
from reels_bot.download import Downloader, DownloadError


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, *, hang=False):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stderr.feed_data(stderr)
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.returncode = None
        self.final_returncode = returncode
        self.pid = 987654321
        self.hang = hang
        self.stopped = asyncio.Event()
        self.waits = 0
        self.killed = False

    async def wait(self):
        self.waits += 1
        if self.hang:
            await self.stopped.wait()
        self.returncode = -9 if self.killed else self.final_returncode
        return self.returncode

    def kill(self):
        self.killed = True
        self.stopped.set()


def install_process(monkeypatch, process):
    called = {}

    async def create(*args, **kwargs):
        called["args"] = args
        called["kwargs"] = kwargs
        return process

    def killpg(pid, sig):
        assert pid == process.pid
        assert sig == signal.SIGKILL
        process.kill()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(os, "killpg", killpg, raising=False)
    return called


def output(*paths: Path) -> bytes:
    return "".join(json.dumps(str(path)) + "\n" for path in paths).encode()


def test_real_selector_accepts_twitter_progressive_formats_without_codec_metadata():
    from yt_dlp import YoutubeDL

    # Twitter direct HTTP variants can omit codec names. Unknown audio metadata
    # must not discard these otherwise-valid progressive MP4 formats.
    with YoutubeDL({"quiet": True, "format": DOWNLOAD_FORMAT}) as downloader:
        selected = downloader.process_ie_result(
            {
                "id": "fixture",
                "title": "fixture",
                "formats": [
                    {
                        "format_id": "http-720",
                        "url": "https://video.twimg.com/fixture.mp4",
                        "ext": "mp4",
                        "height": 720,
                        "protocol": "https",
                    }
                ],
            },
            download=False,
        )
    assert selected["format_id"] == "http-720"


@pytest.mark.parametrize(
    "audio, expected",
    [(None, "video"), ("audio-only", "video+audio"), ("progressive", "audio")],
)
def test_real_selector_preserves_available_sound_or_downloads_silent_source(audio, expected):
    from yt_dlp import YoutubeDL

    formats = [
        {
            "format_id": "video",
            "url": "https://example.test/silent.mp4",
            "ext": "mp4",
            "vcodec": "avc1",
            "acodec": "none",
            "height": 480,
        }
    ]
    if audio is not None:
        formats.append(
            {
                "format_id": "audio",
                "url": "https://example.test/source.webm",
                "ext": "webm",
                "vcodec": "none" if audio == "audio-only" else "vp9",
                "acodec": "opus",
                "height": None if audio == "audio-only" else 480,
            }
        )
    with YoutubeDL({"quiet": True, "format": DOWNLOAD_FORMAT}) as downloader:
        selected = downloader.process_ie_result(
            {"id": "fixture", "title": "fixture", "formats": formats}, download=False
        )
    assert selected["format_id"] == expected
    if audio is not None:
        assert selected["acodec"] != "none"


@pytest.mark.asyncio
async def test_download_multiple_post_videos_and_bounded_command(monkeypatch, tmp_path):
    paths = [tmp_path / "01.mp4", tmp_path / "02.mp4"]
    for path in paths:
        path.write_bytes(b"video")
    process = FakeProcess(output(*paths))
    called = install_process(monkeypatch, process)

    result = await Downloader().download("https://x.com/user/status/123", tmp_path)

    assert [video.path for video in result] == paths
    args = called["args"]
    assert args[:3] == (sys.executable, "-m", "yt_dlp")
    assert args[args.index("--playlist-items") + 1] == "1:4"
    assert args[args.index("--max-downloads") + 1] == "4"
    assert args[args.index("--max-filesize") + 1] == "49000000"
    assert "--no-playlist" not in args
    assert "--cookies" not in args
    assert args[-2:] == ("--", "https://x.com/user/status/123")
    assert called["kwargs"]["stdin"] == asyncio.subprocess.DEVNULL
    assert called["kwargs"]["start_new_session"] == (os.name == "posix")


@pytest.mark.asyncio
async def test_expected_max_downloads_exit_is_success(monkeypatch, tmp_path):
    paths = [tmp_path / f"{number:02d}.mp4" for number in range(1, 5)]
    for path in paths:
        path.write_bytes(b"video")
    process = FakeProcess(output(*paths), returncode=101)
    install_process(monkeypatch, process)

    assert len(await Downloader().download("https://x.com/u/status/123", tmp_path)) == 4


async def test_unmerged_tracks_are_not_sent_as_silent_video(monkeypatch, tmp_path):
    path = tmp_path / "01.mp4"
    path.write_bytes(b"video track without the source audio")
    process = FakeProcess(
        output(path),
        b"WARNING: You have requested merging of multiple formats but ffmpeg is not installed. "
        b"The formats won't be merged",
        returncode=0,
    )
    install_process(monkeypatch, process)
    with pytest.raises(DownloadError, match="FFmpeg"):
        await Downloader().download("https://x.com/u/status/123", tmp_path)


@pytest.mark.asyncio
async def test_explicit_cookies_only(monkeypatch, tmp_path):
    path = tmp_path / "01.mp4"
    path.write_bytes(b"video")
    called = install_process(monkeypatch, FakeProcess(output(path)))
    cookie_path = tmp_path / "source" / "cookies.txt"
    cookie_path.parent.mkdir()
    cookie_path.write_text("# Netscape HTTP Cookie File\n")

    await Downloader(cookies_file=cookie_path).download("https://x.com/u/status/1", tmp_path)

    args = called["args"]
    copy = Path(args[args.index("--cookies") + 1])
    assert copy != cookie_path
    assert copy.parent == tmp_path
    assert copy.stat().st_mode & 0o777 == 0o600
    assert copy.read_text() == cookie_path.read_text()
    copy.write_text("yt-dlp may update its own cookie jar")
    assert cookie_path.read_text() == "# Netscape HTTP Cookie File\n"


@pytest.mark.asyncio
async def test_oversize_completed_file_is_rejected(monkeypatch, tmp_path):
    path = tmp_path / "01.mp4"
    path.write_bytes(b"x" * 101)
    install_process(monkeypatch, FakeProcess(output(path)))

    with pytest.raises(DownloadError, match="слишком большое"):
        await Downloader(max_bytes=100).download("https://x.com/u/status/123", tmp_path)


@pytest.mark.asyncio
async def test_oversize_partial_file_stops_running_process(monkeypatch, tmp_path):
    (tmp_path / "01.mp4.part").write_bytes(b"x" * 101)
    process = FakeProcess(hang=True)
    install_process(monkeypatch, process)

    with pytest.raises(DownloadError, match="слишком большое"):
        await Downloader(max_bytes=100).download("https://x.com/u/status/123", tmp_path)

    assert process.killed
    assert process.returncode == -9
    assert process.waits >= 2


@pytest.mark.asyncio
async def test_timeout_kills_and_reaps_child(monkeypatch, tmp_path):
    process = FakeProcess(hang=True)
    install_process(monkeypatch, process)

    with pytest.raises(DownloadError, match="слишком много времени"):
        await Downloader(timeout=0.01).download("https://x.com/u/status/123", tmp_path)

    assert process.killed
    assert process.returncode == -9
    assert process.waits >= 2


@pytest.mark.asyncio
async def test_cancellation_kills_and_reaps_child(monkeypatch, tmp_path):
    process = FakeProcess(hang=True)
    install_process(monkeypatch, process)
    task = asyncio.create_task(Downloader().download("https://x.com/u/status/123", tmp_path))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.killed
    assert process.returncode == -9


@pytest.mark.asyncio
async def test_failure_does_not_return_partial_results_or_leak_stderr(monkeypatch, tmp_path):
    path = tmp_path / "01.mp4"
    path.write_bytes(b"video")
    secret = "https://example.test/?token=secret"
    process = FakeProcess(output(path), f"ERROR: Login required; cookies: {secret}".encode(), 1)
    install_process(monkeypatch, process)

    with pytest.raises(DownloadError, match="требует входа") as error:
        await Downloader().download("https://x.com/u/status/123", tmp_path)

    assert secret not in str(error.value)
    assert "cookies" not in str(error.value)
    assert path.exists()  # Only the owner of the temporary directory removes it.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stderr, message",
    [
        (b"File is larger than max-filesize", "слишком большое"),
        (b"ERROR: request timed out", "не ответил вовремя"),
        (b"ERROR: ffmpeg is not installed", "нужен FFmpeg"),
        (b"ERROR: no video formats found", "Не удалось скачать"),
    ],
)
async def test_safe_error_messages(monkeypatch, tmp_path, stderr, message):
    install_process(monkeypatch, FakeProcess(stderr=stderr, returncode=1))

    with pytest.raises(DownloadError, match=message):
        await Downloader().download("https://x.com/u/status/123", tmp_path)


@pytest.mark.asyncio
async def test_no_completed_file_is_not_success(monkeypatch, tmp_path):
    (tmp_path / "01.mp4.part").write_bytes(b"unfinished")
    install_process(monkeypatch, FakeProcess())

    with pytest.raises(DownloadError):
        await Downloader().download("https://x.com/u/status/123", tmp_path)


@pytest.mark.asyncio
async def test_rejects_paths_outside_request_directory(monkeypatch, tmp_path):
    destination = tmp_path / "request"
    destination.mkdir()
    outside = tmp_path / "01.mp4"
    outside.write_bytes(b"video")
    install_process(monkeypatch, FakeProcess(output(outside)))

    with pytest.raises(DownloadError):
        await Downloader().download("https://x.com/u/status/123", destination)


@pytest.mark.asyncio
async def test_launch_failure_is_safe(monkeypatch, tmp_path):
    async def fail(*args, **kwargs):
        raise OSError("internal path or secret")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail)
    with pytest.raises(DownloadError, match="временно недоступен") as error:
        await Downloader().download("https://x.com/u/status/123", tmp_path)
    assert "secret" not in str(error.value)
