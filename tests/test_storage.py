import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from reels_bot.storage import DownloadWorkspace


def test_request_removes_downloads_and_metadata_on_success(tmp_path):
    workspace = DownloadWorkspace(tmp_path / "downloads")
    with workspace.request() as job:
        (job / "video.mp4").write_bytes(b"video")
        (job / "_cookies.txt").write_text("private cookie copy")
        assert job.is_dir()
        assert job.stat().st_mode & 0o777 == 0o700
    assert not job.exists()


def test_request_cleans_after_error(tmp_path):
    workspace = DownloadWorkspace(tmp_path / "downloads")
    with pytest.raises(RuntimeError, match="upload failed"), workspace.request() as job:
        (job / "video.mp4.part").write_bytes(b"partial")
        raise RuntimeError("upload failed")
    assert not job.exists()


@pytest.mark.asyncio
async def test_request_cleans_after_cancellation(tmp_path):
    workspace = DownloadWorkspace(tmp_path / "downloads")
    started = asyncio.Event()
    paths = []

    async def upload():
        with workspace.request() as job:
            paths.append(job)
            (job / "video.mp4").write_bytes(b"video")
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(upload())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not paths[0].exists()


def test_cleanup_and_other_requests_skip_active_jobs(tmp_path):
    workspace = DownloadWorkspace(tmp_path / "downloads")
    other = DownloadWorkspace(workspace.root)
    with workspace.request() as first:
        (first / "video.mp4").write_bytes(b"video")
        assert other.cleanup_abandoned() == 0
        with other.request() as second:
            assert first.exists()
            assert second != first
            assert workspace.cleanup_abandoned() == 0
        assert first.exists()
    assert not first.exists()


_CHILD_SCRIPT = """
import os
import sys
from pathlib import Path
from reels_bot.storage import DownloadWorkspace
with DownloadWorkspace(Path(sys.argv[1])).request() as job:
    (job / 'video.mp4.part').write_bytes(b'partial download')
    print(job, flush=True)
    if sys.argv[2] == 'hold':
        sys.stdin.readline()
    os._exit(0)
"""


def crash_job(root: Path) -> Path:
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT, str(root), "crash"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    job = Path(result.stdout.strip())
    assert job.is_dir()
    return job


def test_startup_cleanup_removes_real_crashed_process_job(tmp_path):
    root = tmp_path / "downloads"
    abandoned = crash_job(root)
    workspace = DownloadWorkspace(root)
    assert workspace.cleanup_abandoned() == 1
    assert not abandoned.exists()
    assert workspace.cleanup_abandoned() == 0


def test_next_request_cleans_jobs_abandoned_since_startup(tmp_path):
    workspace = DownloadWorkspace(tmp_path / "downloads")
    assert workspace.cleanup_abandoned() == 0
    abandoned = crash_job(workspace.root)
    with workspace.request() as current:
        assert not abandoned.exists()
        assert current.is_dir()


def test_cleanup_skips_job_locked_by_another_process(tmp_path):
    workspace = DownloadWorkspace(tmp_path / "downloads")
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD_SCRIPT, str(workspace.root), "hold"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        active = Path(child.stdout.readline().strip())
        assert workspace.cleanup_abandoned() == 0
        assert active.is_dir()
        child.communicate("stop\n", timeout=10)
        assert child.returncode == 0
        assert workspace.cleanup_abandoned() == 1
        assert not active.exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_cleanup_preserves_unrelated_entries_and_symlink_targets(tmp_path):
    workspace = DownloadWorkspace(tmp_path / "downloads")
    unrelated = workspace.root / "another-app"
    unrelated.mkdir()
    (unrelated / "important.txt").write_text("keep")
    ordinary = workspace.root / "notes.txt"
    ordinary.write_text("keep")
    target = tmp_path / "outside"
    target.mkdir()
    (target / "important.txt").write_text("keep")
    link = workspace.root / ("job-" + "a" * 32)
    link.symlink_to(target, target_is_directory=True)
    assert workspace.cleanup_abandoned() == 0
    assert ordinary.read_text() == "keep"
    assert (unrelated / "important.txt").read_text() == "keep"
    assert (target / "important.txt").read_text() == "keep"
    assert link.is_symlink()


def test_cleanup_never_follows_symlinks_inside_abandoned_job(tmp_path):
    workspace = DownloadWorkspace(tmp_path / "downloads")
    abandoned = crash_job(workspace.root)
    outside = tmp_path / "important.txt"
    outside.write_text("keep")
    (abandoned / "external").symlink_to(outside)
    assert workspace.cleanup_abandoned() == 1
    assert outside.read_text() == "keep"


def test_rejects_symlink_or_public_root(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        DownloadWorkspace(link)
    os.chmod(target, 0o755)
    with pytest.raises(ValueError, match="private directory"):
        DownloadWorkspace(target)


def test_replaced_root_symlink_is_not_traversed(tmp_path):
    workspace = DownloadWorkspace(tmp_path / "downloads")
    outside = tmp_path / "outside"
    outside.mkdir()
    important = outside / ("job-" + "b" * 32)
    important.mkdir()
    (important / "video.mp4").write_bytes(b"keep")
    workspace.root.rmdir()
    workspace.root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        workspace.cleanup_abandoned()
    assert (important / "video.mp4").read_bytes() == b"keep"
