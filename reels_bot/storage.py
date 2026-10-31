"""Private download directories with POSIX locks and crash recovery."""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import stat
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class DownloadWorkspace:
    def __init__(self, root: Path | None = None) -> None:
        chosen = (
            root if root is not None else Path(tempfile.gettempdir()) / f"reels-sav-{os.getuid()}"
        )
        if chosen.is_symlink():
            raise ValueError("Download workspace root must not be a symlink")
        chosen.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = chosen.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError(
                "Download workspace root must be a private directory owned by this user"
            )
        self.root = chosen.resolve()

    @contextmanager
    def _root_lock(self) -> Iterator[None]:
        if self.root.is_symlink():
            raise ValueError("Download workspace root must not be a symlink")
        descriptor = self._open_lock(self.root / ".root.lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    @staticmethod
    def _open_lock(path: Path) -> int:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            os.close(descriptor)
            raise ValueError("Workspace lock must be a regular file owned by this user")
        return descriptor

    def cleanup_abandoned(self) -> int:
        """Remove abandoned jobs, leaving actively locked jobs untouched."""
        with self._root_lock():
            return self._cleanup_locked()

    def _cleanup_locked(self) -> int:
        removed = 0
        for job in self.root.iterdir():
            if not re.fullmatch(r"job-[0-9a-f]{32}", job.name) or job.is_symlink():
                continue
            try:
                info = job.stat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                    continue
                descriptor = self._open_lock(job / ".active")
            except (OSError, ValueError):
                # In particular, never follow a symlink used as a lock file.
                continue
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                shutil.rmtree(job)
                removed += 1
            finally:
                os.close(descriptor)
        return removed

    @contextmanager
    def request(self) -> Iterator[Path]:
        """Hold a job lock until download/upload ends; always remove its files."""
        with self._root_lock():
            self._cleanup_locked()
            job = self.root / f"job-{uuid.uuid4().hex}"
            job.mkdir(mode=0o700)
            try:
                descriptor = self._open_lock(job / ".active")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                except BaseException:
                    os.close(descriptor)
                    raise
            except BaseException:
                shutil.rmtree(job)
                raise
        try:
            yield job
        finally:
            try:
                with self._root_lock():
                    if not job.is_symlink() and job.is_dir():
                        shutil.rmtree(job)
            finally:
                os.close(descriptor)
