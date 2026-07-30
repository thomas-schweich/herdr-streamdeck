"""Instance-lock tests.

The property that matters is that running the command twice works -- the second
invocation gets the deck and the first goes away. Everything else here defends
the edges of that: a crashed holder, a truncated lock file, a daemon too old to
hold a lock at all.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from herdr_streamdeck.instance import (
    AlreadyRunning,
    SingleInstance,
    lock_path,
    runtime_dir,
    stop_running,
)

# --------------------------------------------------------------------- paths


def test_the_lock_is_keyed_by_serial() -> None:
    """Two decks, two daemons, neither displacing the other."""
    assert lock_path("AB123") != lock_path("CD456")
    assert lock_path() != lock_path("AB123")


def test_the_lock_lives_somewhere_that_gets_cleared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert runtime_dir() == tmp_path


def test_a_missing_runtime_dir_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS has no XDG_RUNTIME_DIR."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/nonexistent/nope")
    monkeypatch.delenv("TMPDIR", raising=False)
    assert runtime_dir() == Path("/tmp")


# ---------------------------------------------------------------- acquisition


def test_an_uncontended_lock_is_taken_cleanly(tmp_path: Path) -> None:
    instance = SingleInstance(tmp_path / "d.lock")
    assert instance.acquire() is None
    assert instance.path.read_text().splitlines()[0] == str(os.getpid())
    instance.release()


def test_releasing_lets_the_next_one_in(tmp_path: Path) -> None:
    path = tmp_path / "d.lock"
    first = SingleInstance(path)
    first.acquire()
    first.release()
    second = SingleInstance(path)
    assert second.acquire() is None
    second.release()


def test_a_lock_left_by_a_dead_process_is_not_stale(tmp_path: Path) -> None:
    """The kernel drops an flock when the holder exits, however it exits. A
    bare pidfile cannot promise that, which is why this is not a pidfile."""
    path = tmp_path / "d.lock"
    script = textwrap.dedent(f"""
        import fcntl, os
        handle = open({str(path)!r}, "a+")
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(f"{{os.getpid()}}\\n")
        handle.flush()
        os._exit(1)          # crash, without unlocking
    """)
    subprocess.run([sys.executable, "-c", script], check=False, timeout=20)

    instance = SingleInstance(path)
    assert instance.acquire(timeout=1.0) is None, "a dead holder should block nothing"
    instance.release()


def test_a_truncated_lock_file_is_survivable(tmp_path: Path) -> None:
    """An older version may have written nothing, or something else."""
    path = tmp_path / "d.lock"
    path.write_text("not a pid\nwhatever\n")
    instance = SingleInstance(path)
    assert instance.acquire(timeout=1.0) is None
    instance.release()


# ------------------------------------------------------------------ takeover


HOLDER = """
import fcntl, os, signal, sys, time
path = sys.argv[1]
handle = open(path, "a+")
fcntl.flock(handle, fcntl.LOCK_EX)
handle.seek(0); handle.truncate()
handle.write(f"{os.getpid()}\\n")
handle.flush()
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
print("held", flush=True)
time.sleep(60)
"""


def start_holder(path: Path) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_a_second_invocation_takes_the_deck_over(tmp_path: Path) -> None:
    """The whole point: restarting is just running it again."""
    path = tmp_path / "d.lock"
    holder = start_holder(path)
    try:
        instance = SingleInstance(path)
        displaced = instance.acquire(timeout=10.0)
        assert displaced == holder.pid
        assert holder.wait(timeout=5) == 0, "the old daemon exited cleanly"
        instance.release()
    finally:
        holder.kill()


def test_no_takeover_refuses_instead(tmp_path: Path) -> None:
    path = tmp_path / "d.lock"
    holder = start_holder(path)
    try:
        with pytest.raises(AlreadyRunning, match=str(holder.pid)):
            SingleInstance(path).acquire(takeover=False)
        assert holder.poll() is None, "it must not have been killed"
    finally:
        holder.kill()
        holder.wait(timeout=5)


def test_a_wedged_holder_is_forced(tmp_path: Path) -> None:
    """SIGTERM is a request. A daemon that ignores it still has to let go."""
    path = tmp_path / "d.lock"
    deaf = textwrap.dedent(f"""
        import fcntl, os, signal, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        handle = open({str(path)!r}, "a+")
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0); handle.truncate()
        handle.write(f"{{os.getpid()}}\\n"); handle.flush()
        print("held", flush=True)
        time.sleep(60)
    """)
    holder = subprocess.Popen([sys.executable, "-c", deaf], stdout=subprocess.PIPE, text=True)
    assert holder.stdout is not None
    holder.stdout.readline()
    try:
        started = time.monotonic()
        displaced = SingleInstance(path).acquire(timeout=1.0)
        assert displaced == holder.pid
        assert time.monotonic() - started < 8.0, "it should escalate, not hang"
    finally:
        holder.kill()
        holder.wait(timeout=5)


def point_lock_at(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    def fixed(serial: str | None = None) -> Path:
        return path

    monkeypatch.setattr("herdr_streamdeck.instance.lock_path", fixed)


def test_stop_running_stops_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "d.lock"
    holder = start_holder(path)
    try:
        point_lock_at(monkeypatch, path)
        assert stop_running() == holder.pid
        assert holder.wait(timeout=5) == 0
    finally:
        holder.kill()


def test_stop_running_says_so_when_nothing_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "d.lock"
    path.write_text("")
    point_lock_at(monkeypatch, path)
    assert stop_running() is None
