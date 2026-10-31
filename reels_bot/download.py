"""Bounded yt-dlp subprocesses; callers own the per-request temporary directory."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import sys
from dataclasses import dataclass
from pathlib import Path


class DownloadError(Exception):
    """A safe, Russian message suitable for sending to the requesting user."""


@dataclass(frozen=True)
class DownloadedVideo:
    path: Path
    title: str = ""
    width: int | None = None
    height: int | None = None
    duration: float | None = None


_SIZE_ERROR = "Видео слишком большое для отправки. Попробуйте более короткий ролик."
_DOWNLOAD_ERROR = (
    "Не удалось скачать видео. Проверьте, что публикация доступна, и попробуйте позже."
)
_MAX_VIDEOS = 4
# Prefer progressive MP4, which needs no merging. Never choose a silent stream
# while an audio-only or complete progressive source is available. Joining two
# progressive formats is unsafe: yt-dlp can discard the second one's audio when
# it suppresses its extra video track, so remux that complete source instead.
_VIDEO = "(bestvideo[ext=mp4][height<=?720]/worstvideo[ext=mp4])"
_FORMAT = (
    "best[ext=mp4][vcodec^=avc1][acodec!=?none][height<=?720]/"
    "best[ext=mp4][acodec!=?none][height<=?720]/"
    "worst[ext=mp4][acodec!=?none]/"
    f"{_VIDEO}+bestaudio/best[acodec!=?none]/{_VIDEO}"
)


class Downloader:
    def __init__(
        self,
        max_bytes: int = 49_000_000,
        timeout: int = 180,
        cookies_file: Path | None = None,
    ) -> None:
        if not 0 < max_bytes <= 49_000_000:
            raise ValueError("max_bytes must be between 1 and 49000000")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.cookies_file = cookies_file

    async def download(self, url: str, destination: Path) -> list[DownloadedVideo]:
        """Download one already-validated post, returning at most four files.

        Do not reuse a destination between requests. Partial results are never
        returned after an extraction/download error; the caller removes them.
        """
        destination = destination.resolve()
        try:
            destination.mkdir(parents=True, exist_ok=True)
            request_cookies = None
            if self.cookies_file is not None:
                # yt-dlp saves its cookie jar on exit. Give each request a
                # private copy so concurrent jobs never rewrite the source.
                request_cookies = destination / "_cookies.txt"
                descriptor = os.open(request_cookies, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as target, self.cookies_file.open("rb") as source:
                    shutil.copyfileobj(source, target)
        except OSError:
            raise DownloadError("Сервис скачивания временно недоступен.") from None
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--no-progress",
            "--no-cache-dir",
            "--no-plugin-dirs",
            "--no-simulate",
            "--abort-on-error",
            "--abort-on-unavailable-fragments",
            "--playlist-items",
            "1:4",
            "--lazy-playlist",
            "--max-downloads",
            str(_MAX_VIDEOS),
            "--max-filesize",
            str(self.max_bytes),
            "--socket-timeout",
            "15",
            "--retries",
            "1",
            "--fragment-retries",
            "1",
            "--extractor-retries",
            "1",
            "--file-access-retries",
            "1",
            "--concurrent-fragments",
            "1",
            "--match-filters",
            "!is_live",
            "--format",
            _FORMAT,
            "--merge-output-format",
            "mp4",
            "--remux-video",
            "mp4",
            "--output",
            str(destination / "%(autonumber)02d.%(ext)s"),
            "--print",
            "after_move:%(filepath)j",
        ]
        if request_cookies is not None:
            command.extend(["--cookies", str(request_cookies)])
        command.extend(["--", url])
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
        except OSError:
            raise DownloadError("Сервис скачивания временно недоступен.") from None

        assert process.stdout is not None and process.stderr is not None
        tasks = [
            asyncio.create_task(process.wait()),
            asyncio.create_task(_read_output(process.stdout)),
            asyncio.create_task(_read_output(process.stderr, tail=True)),
            asyncio.create_task(self._watch_size(process, destination)),
        ]
        try:
            _, stdout, stderr, _ = await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=self.timeout,
            )
        except TimeoutError:
            await _kill_and_wait(process)
            raise DownloadError(
                "Скачивание заняло слишком много времени. Попробуйте позже."
            ) from None
        except BaseException:
            # Includes CancelledError: stopping a request must stop its child
            # processes before the parent removes the temporary directory.
            await _kill_and_wait(process)
            raise
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        # yt-dlp exits with 101 when --max-downloads is reached normally.
        if process.returncode not in (0, 101):
            raise _friendly_error(stderr)
        # yt-dlp can exit successfully with separate, unmerged tracks when
        # FFmpeg is missing. Never send only the video track in that case.
        if b"requested merging" in stderr.lower() and b"ffmpeg" in stderr.lower():
            if any(word in stderr.lower() for word in (b"not installed", b"not found")):
                raise _friendly_error(stderr)
        self._check_size(destination)
        videos = self._finished_videos(stdout, destination)
        if process.returncode == 101 and len(videos) != _MAX_VIDEOS:
            raise DownloadError(_DOWNLOAD_ERROR)
        if not videos:
            raise _friendly_error(stderr)
        return videos

    async def _watch_size(self, process: asyncio.subprocess.Process, destination: Path) -> None:
        # --max-filesize also aborts while downloading, but not every protocol
        # reports its size reliably. Watch partial files as a second guard.
        while process.returncode is None:
            self._check_size(destination)
            await asyncio.sleep(0.1)

    def _check_size(self, destination: Path) -> None:
        total = 0
        for path in destination.iterdir():
            try:
                if path.is_symlink():
                    raise DownloadError(_DOWNLOAD_ERROR)
                if path.name == "_cookies.txt":
                    continue
                if not path.is_file():
                    continue
                size = path.stat().st_size
            except FileNotFoundError:
                continue  # yt-dlp can rename .part files between these calls.
            if size > self.max_bytes:
                raise DownloadError(_SIZE_ERROR)
            total += size
        if total > self.max_bytes * _MAX_VIDEOS:
            raise DownloadError(_SIZE_ERROR)

    def _finished_videos(self, stdout: bytes, destination: Path) -> list[DownloadedVideo]:
        videos: list[DownloadedVideo] = []
        seen: set[Path] = set()
        try:
            for line in stdout.decode("utf-8").splitlines():
                if not line.strip():
                    continue
                raw_path = json.loads(line)
                if not isinstance(raw_path, str):
                    raise ValueError
                path = Path(raw_path)
                # Only accept the fixed output names inside the owned directory.
                if path.is_symlink() or not re.fullmatch(r"0[1-4]\.mp4", path.name):
                    raise ValueError
                path = path.resolve(strict=True)
                if path.parent != destination or not path.is_file():
                    raise ValueError
                if not 0 < path.stat().st_size <= self.max_bytes:
                    raise ValueError
                if path not in seen:
                    videos.append(DownloadedVideo(path))
                    seen.add(path)
                if len(videos) > _MAX_VIDEOS:
                    raise ValueError
        except (OSError, ValueError, UnicodeError):
            raise DownloadError(_DOWNLOAD_ERROR) from None
        return videos


async def _read_output(stream: asyncio.StreamReader, *, tail: bool = False) -> bytes:
    data = bytearray()
    while chunk := await stream.read(8192):
        data.extend(chunk)
        if len(data) > 65_536:
            if not tail:
                raise DownloadError(_DOWNLOAD_ERROR)
            del data[:-65_536]
    return bytes(data)


async def _kill_and_wait(process: asyncio.subprocess.Process) -> None:
    # A separate POSIX session lets us stop an ffmpeg child as well as yt-dlp.
    with contextlib.suppress(ProcessLookupError):
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
    await process.wait()


def _friendly_error(stderr: bytes) -> DownloadError:
    error = stderr.decode("utf-8", errors="replace").lower()
    if "ffmpeg" in error and any(
        word in error for word in ("not found", "not installed", "unable", "error", "failed")
    ):
        return DownloadError(
            "Для обработки этого видео на сервере нужен FFmpeg. Обратитесь к владельцу бота."
        )
    if any(
        word in error for word in ("max-filesize", "larger than", "file is larger", "too large")
    ):
        return DownloadError(_SIZE_ERROR)
    if any(
        word in error
        for word in ("login", "sign in", "log in", "cookies", "private", "not authorized")
    ):
        return DownloadError(
            "Публикация требует входа или недоступна. Попробуйте открытую публикацию."
        )
    if "timed out" in error or "timeout" in error:
        return DownloadError("Источник не ответил вовремя. Попробуйте позже.")
    return DownloadError(_DOWNLOAD_ERROR)
