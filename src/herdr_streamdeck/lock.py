"""Whether the computer's screen is locked.

The keys carry the agents' own words, which is exactly the content a lock
screen exists to hide. Left alone, the deck goes on showing them to the room
after the Mac is locked, and the only remedy is unplugging it.

macOS publishes lock state in the IORegistry, so reading it costs a subprocess
and no dependency. Everywhere else this reports unlocked: Linux and Windows
have no single equivalent to read, and guessing would be worse than declining.
"""

from __future__ import annotations

import logging
import platform
import re
import subprocess
from collections.abc import Callable
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

POLL_SECONDS = 0.5
"""How often to ask. The interval is the exposure window after locking, so it
wants to be short; ``ioreg`` measured well under the budget at twice a second."""

IOREG = ("ioreg", "-n", "Root", "-d1", "-k", "IOConsoleUsers")
"""Root's record of the console sessions, dumped whole.

Filtered on ``IOConsoleUsers`` and not on ``CGSSessionScreenIsLocked``, which
is the obvious choice and the wrong one: ``-k`` matches an object's *own*
properties, and the lock flag is not one -- it lives inside a dict inside the
``IOConsoleUsers`` array. Asking for it directly matches nothing and prints a
bare ``+-o Root`` line with no properties at all, which parses as "never
locked" forever. ``-d1`` then prints the array in full, flag and all."""

LOCKED = re.compile(r'"CGSSessionScreenIsLocked"\s*=\s*Yes')
"""The key is present and Yes only while the screen is locked, and absent
otherwise -- so this is a search for a positive, not a boolean parse."""

TIMEOUT = 2.0


@runtime_checkable
class LockWatcher(Protocol):
    """Reports whether the screen is currently locked."""

    def locked(self) -> bool: ...


class NoLock:
    """Every platform whose lock state we cannot read."""

    def locked(self) -> bool:
        return False


def read_ioreg() -> str:
    result = subprocess.run(IOREG, capture_output=True, text=True, timeout=TIMEOUT, check=True)
    return result.stdout


class ScreenLock:
    """macOS lock state.

    Two different failures, deliberately handled two different ways. If the
    very first read fails, ``available`` is False and the caller drops back to
    ``NoLock`` with a warning -- better to say plainly that the deck will not
    blank than to blank it forever for a reason nobody can see. If a read fails
    *after* one has succeeded, the screen is reported locked: at that point
    something real has broken, and the safe reading of "I cannot tell" is the
    one that hides the summaries.
    """

    def __init__(self, reader: Callable[[], str] | None = None) -> None:
        self._read = reader or read_ioreg
        self._proven = False
        self._complained = False
        self.available = self._probe()

    def _probe(self) -> bool:
        try:
            self._read()
        except Exception:
            logger.debug("ioreg probe failed", exc_info=True)
            return False
        self._proven = True
        return True

    def locked(self) -> bool:
        try:
            output = self._read()
        except Exception:
            if not self._proven:
                return False
            if not self._complained:
                self._complained = True
                logger.warning(
                    "cannot read the screen lock state; blanking the deck until "
                    "it can be read again",
                    exc_info=True,
                )
            return True
        if self._complained:
            self._complained = False
            logger.info("screen lock state readable again")
        return LOCKED.search(output) is not None


def lock_watcher(*, enabled: bool = True) -> LockWatcher:
    """The best lock watcher this platform can offer."""
    if not enabled:
        return NoLock()
    if platform.system() != "Darwin":
        return NoLock()
    watcher = ScreenLock()
    if not watcher.available:
        logger.warning(
            "could not read %s from ioreg, so the deck will stay lit while the "
            "screen is locked",
            "CGSSessionScreenIsLocked",
        )
        return NoLock()
    logger.info("watching the screen lock; the deck blanks while it is locked")
    return watcher
