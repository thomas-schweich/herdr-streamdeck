"""One daemon per deck, and a new one displaces the old.

The deck is an exclusive resource -- hidapi opens it for one process -- so two
daemons cannot share it and the second would simply fail. Since the whole point
of running standalone is that you can restart it whenever you like, "restart"
has to mean "just run it again" rather than "find and kill the old one first".

Takeover is built on ``flock`` rather than a socket or a signal protocol,
because it has to work between versions that know nothing about each other. A
lock is an OS primitive with no wire format to get wrong, and the kernel drops
it when the holder exits *however* it exits -- so a crashed daemon leaves no
stale state to clean up, which a bare pidfile cannot promise.

A daemon started before this module existed holds no lock, so the very first
upgrade needs one manual restart. That is a one-time cost paid once, and the
alternative -- hunting for processes by command line -- would kill an editor
that happened to have the project open. Not worth it for a single changeover.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import logging
import os
import signal
import time
from collections.abc import Iterable
from pathlib import Path
from typing import IO

logger = logging.getLogger(__name__)

TAKEOVER_TIMEOUT = 10.0
"""How long to wait for the previous daemon to let go before forcing it.

Generous: a clean shutdown resets the deck and closes the device, and being
slow to hand over is much better than two daemons briefly fighting over it.
"""

GRACE = 0.1
"""Poll interval while waiting for the lock."""


def runtime_dir() -> Path:
    """Where to keep the lock.

    ``XDG_RUNTIME_DIR`` when it exists, since the kernel clears it on logout and
    a lock outliving the session is meaningless. macOS has no such directory, so
    fall back to the temp dir.
    """
    for name in ("XDG_RUNTIME_DIR", "TMPDIR"):
        value = os.environ.get(name)
        if value and Path(value).is_dir():
            return Path(value)
    return Path("/tmp")


def lock_path(serial: str | None = None) -> Path:
    """The lock for a given deck.

    Keyed by serial when one is requested, so two decks can be driven by two
    daemons without either displacing the other.
    """
    suffix = f"-{serial}" if serial else ""
    return runtime_dir() / f"herdr-streamdeck{suffix}.lock"


def _read_pid(handle: IO[str]) -> int | None:
    """The PID recorded in the lock file, if it holds a plausible one.

    Deliberately forgiving about the rest of the file. A future version may
    write more, and an older one may have written less or nothing at all; the
    first line being a number is the only thing this contract can promise.
    """
    try:
        handle.seek(0)
        first = handle.readline().strip()
    except OSError:
        return None
    if not first.isdigit():
        return None
    pid = int(first)
    return pid if pid > 1 else None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Someone else's process. Alive, but not ours to signal.
        return True
    return True


class AlreadyRunning(RuntimeError):
    """Another daemon holds the deck and was not asked to give it up."""


class SingleInstance:
    """Holds the lock for as long as this daemon runs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: IO[str] | None = None

    def _try_lock(self, handle: IO[str]) -> bool:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                return False
            raise
        return True

    def acquire(
        self, *, takeover: bool = True, timeout: float = TAKEOVER_TIMEOUT
    ) -> int | None:
        """Take the lock, displacing the previous holder if asked to.

        Returns the PID that was displaced, or None if nothing was running.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")  # noqa: SIM115 -- held for the process lifetime
        self._handle = handle

        if self._try_lock(handle):
            self._record()
            return None

        previous = _read_pid(handle)
        if not takeover:
            raise AlreadyRunning(
                f"another herdr-streamdeck is running (pid {previous or 'unknown'}). "
                "Run it without --no-takeover to replace it, or --stop to end it"
            )

        if previous is not None:
            logger.info("asking the running daemon (pid %d) to hand over the deck", previous)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(previous, signal.SIGTERM)

        if self._wait_for_lock(handle, timeout):
            self._record()
            return previous

        if previous is not None:
            logger.warning("pid %d did not exit; forcing it", previous)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(previous, signal.SIGKILL)
            if self._wait_for_lock(handle, timeout=2.0):
                self._record()
                return previous

        raise AlreadyRunning(
            f"could not take the deck from pid {previous or 'unknown'} after {timeout:.0f}s"
        )

    def _wait_for_lock(self, handle: IO[str], timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._try_lock(handle):
                return True
            time.sleep(GRACE)
        return self._try_lock(handle)

    def _record(self) -> None:
        """Write our PID, first line, nothing clever.

        The format is the contract with every future version, so it stays as
        dull as possible: a decimal PID on line one. Anything after it is
        commentary that a reader is free to ignore.
        """
        handle = self._handle
        if handle is None:
            return
        with contextlib.suppress(OSError):
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\nherdr-streamdeck\n")
            handle.flush()

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(handle, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            handle.close()

    def __enter__(self) -> SingleInstance:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def terminate(pids: Iterable[int], timeout: float = 5.0) -> None:
    """Ask, then insist."""
    remaining = [pid for pid in pids if _alive(pid)]
    for pid in remaining:
        logger.info("stopping daemon pid %d", pid)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = [pid for pid in remaining if _alive(pid)]
        if not remaining:
            return
        time.sleep(GRACE)

    for pid in remaining:
        logger.warning("pid %d ignored SIGTERM; forcing it", pid)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


def stop_running(serial: str | None = None, timeout: float = TAKEOVER_TIMEOUT) -> int | None:
    """Stop a running daemon without starting one. Returns the PID stopped."""
    path = lock_path(serial)
    if not path.exists():
        return None
    with open(path, "a+") as handle:
        instance = SingleInstance(path)
        if instance._try_lock(handle):
            # Nobody was holding it.
            with contextlib.suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)
            return None
        pid = _read_pid(handle)
    if pid is None:
        return None
    terminate([pid], timeout=timeout)
    return pid
