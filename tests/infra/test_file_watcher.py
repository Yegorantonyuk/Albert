"""Tests for the file-mtime poller behind the JSON config watchers."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from ductor_bot.infra.file_watcher import FileWatcher

INTERVAL = 0.01
# Long enough for several poll cycles to elapse at the interval above.
SETTLE = 0.15


class _Recorder:
    """Async callback that counts how often the watcher fired."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1


def _touch(path: Path, *, offset: float) -> None:
    """Move *path*'s mtime by *offset* seconds, independent of clock resolution."""
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + offset))


class TestFileWatcher:
    async def test_pre_existing_file_does_not_fire_on_first_poll(self, tmp_path: Path) -> None:
        """Regression: a file present before start() is not a change.

        Without an mtime snapshot in start(), the first poll compared the
        file's real mtime against the 0.0 initial value and always reported
        a change. For the restart-marker watcher that meant the bot restarted
        itself on every boot, then restarted again -- an endless loop.
        """
        marker = tmp_path / "restart-requested"
        marker.write_text("")

        recorder = _Recorder()
        watcher = FileWatcher(marker, recorder, interval=INTERVAL)
        await watcher.start()
        try:
            await asyncio.sleep(SETTLE)
        finally:
            await watcher.stop()

        assert recorder.calls == 0

    async def test_touch_after_start_fires(self, tmp_path: Path) -> None:
        target = tmp_path / "config.json"
        target.write_text("{}")

        recorder = _Recorder()
        watcher = FileWatcher(target, recorder, interval=INTERVAL)
        await watcher.start()
        try:
            _touch(target, offset=10.0)
            await asyncio.sleep(SETTLE)
        finally:
            await watcher.stop()

        assert recorder.calls == 1

    async def test_creation_after_start_fires(self, tmp_path: Path) -> None:
        """A file that does not exist yet at start() still triggers on arrival."""
        target = tmp_path / "created-later.json"

        recorder = _Recorder()
        watcher = FileWatcher(target, recorder, interval=INTERVAL)
        await watcher.start()
        try:
            await asyncio.sleep(SETTLE)
            assert recorder.calls == 0
            target.write_text("{}")
            await asyncio.sleep(SETTLE)
        finally:
            await watcher.stop()

        assert recorder.calls == 1

    async def test_stop_halts_polling(self, tmp_path: Path) -> None:
        target = tmp_path / "config.json"
        target.write_text("{}")

        recorder = _Recorder()
        watcher = FileWatcher(target, recorder, interval=INTERVAL)
        await watcher.start()
        await watcher.stop()

        _touch(target, offset=10.0)
        await asyncio.sleep(SETTLE)

        assert recorder.calls == 0

    async def test_update_mtime_absorbs_a_self_inflicted_write(self, tmp_path: Path) -> None:
        """Callers that write the watched file suppress their own callback."""
        target = tmp_path / "cron_jobs.json"
        target.write_text("{}")

        recorder = _Recorder()
        watcher = FileWatcher(target, recorder, interval=INTERVAL)
        await watcher.start()
        try:
            _touch(target, offset=10.0)
            await watcher.update_mtime()
            await asyncio.sleep(SETTLE)
        finally:
            await watcher.stop()

        assert recorder.calls == 0
