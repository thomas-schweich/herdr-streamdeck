"""Reading the macOS screen lock. No Mac required."""

from __future__ import annotations

import logging
import platform

import pytest

from herdr_streamdeck import lock as lock_module
from herdr_streamdeck.lock import LOCKED, NoLock, ScreenLock, lock_watcher

# ioreg's real shape: one dict per console session, the key present only while
# the screen is locked. Reproduced with its actual spacing -- no spaces around
# the inner `=`, which is why the pattern cannot assume any.
IOREG_LOCKED = """+-o Root  <class IORegistryEntry, id 0x100000100, retain 41>
    {
      "IOConsoleUsers" = ({"kCGSSessionAuditIDKey"=100006,"kCGSSessionUserNameKey"="tas",\
"CGSSessionScreenIsLocked"=Yes,"kCGSSessionOnConsoleKey"=Yes,"kCGSSessionIDKey"=257})
    }
"""

IOREG_UNLOCKED = """+-o Root  <class IORegistryEntry, id 0x100000100, retain 41>
    {
      "IOConsoleUsers" = ({"kCGSSessionAuditIDKey"=100006,"kCGSSessionUserNameKey"="tas",\
"kCGSSessionOnConsoleKey"=Yes,"kCGSSessionIDKey"=257})
    }
"""

IOREG_EXPLICIT_NO = IOREG_LOCKED.replace('IsLocked"=Yes', 'IsLocked" = No')


def test_the_pattern_reads_both_shapes_of_output() -> None:
    assert LOCKED.search(IOREG_LOCKED)
    assert not LOCKED.search(IOREG_UNLOCKED), "absent key means unlocked"
    assert not LOCKED.search(IOREG_EXPLICIT_NO), "some versions say No outright"


def test_spacing_around_the_equals_does_not_matter() -> None:
    assert LOCKED.search('"CGSSessionScreenIsLocked"=Yes')
    assert LOCKED.search('"CGSSessionScreenIsLocked" = Yes')


def test_it_follows_the_session() -> None:
    state = [IOREG_UNLOCKED]
    watcher = ScreenLock(reader=lambda: state[0])
    assert watcher.available
    assert not watcher.locked()

    state[0] = IOREG_LOCKED
    assert watcher.locked()

    state[0] = IOREG_UNLOCKED
    assert not watcher.locked()


def test_a_probe_that_fails_makes_the_watcher_unavailable() -> None:
    def broken() -> str:
        raise OSError("ioreg: not found")

    assert not ScreenLock(reader=broken).available


def test_an_unavailable_watcher_does_not_claim_locked() -> None:
    """It never worked, so it has no business blanking the deck forever."""

    def broken() -> str:
        raise OSError("ioreg: not found")

    assert not ScreenLock(reader=broken).locked()


def test_losing_a_reader_that_worked_reads_as_locked() -> None:
    """The safe reading of "I cannot tell" is the one that hides the summaries."""
    working = [True]

    def sometimes() -> str:
        if not working[0]:
            raise OSError("ioreg died")
        return IOREG_UNLOCKED

    watcher = ScreenLock(reader=sometimes)
    assert not watcher.locked()
    working[0] = False
    assert watcher.locked()


def test_an_outage_is_reported_once_not_twice_a_second(
    caplog: pytest.LogCaptureFixture,
) -> None:
    working = [True]

    def sometimes() -> str:
        if not working[0]:
            raise OSError("ioreg died")
        return IOREG_UNLOCKED

    watcher = ScreenLock(reader=sometimes)
    with caplog.at_level(logging.WARNING, logger="herdr_streamdeck.lock"):
        working[0] = False
        assert watcher.locked()
        assert watcher.locked()
        assert watcher.locked()
    assert len(caplog.records) == 1, "polling twice a second must not spew"

    # Having recovered, a later outage is worth reporting afresh.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="herdr_streamdeck.lock"):
        working[0] = True
        assert not watcher.locked()
        working[0] = False
        assert watcher.locked()
    assert [r.levelno for r in caplog.records] == [logging.INFO, logging.WARNING]


def test_a_disabled_watcher_is_the_null_one() -> None:
    assert isinstance(lock_watcher(enabled=False), NoLock)


def test_platforms_without_a_lock_state_get_the_null_watcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    assert isinstance(lock_watcher(), NoLock)


def test_a_mac_that_cannot_run_ioreg_falls_back_rather_than_blanking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deck stays usable, and the log says why it will not blank."""

    def broken() -> str:
        raise OSError("ioreg: not found")

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(lock_module, "read_ioreg", broken)
    assert isinstance(lock_watcher(), NoLock)


def test_a_mac_gets_the_real_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(lock_module, "read_ioreg", lambda: IOREG_LOCKED)
    watcher = lock_watcher()
    assert isinstance(watcher, ScreenLock)
    assert watcher.locked()


def test_the_null_watcher_never_reports_locked() -> None:
    assert not NoLock().locked()


@pytest.mark.skipif(platform.system() != "Darwin", reason="ioreg is macOS only")
def test_ioreg_really_runs_on_a_mac() -> None:
    """The one thing the fixtures above cannot prove: that the command works.

    Everything else here tests the parsing against captured output. This tests
    the invocation -- that ``ioreg`` is on PATH, accepts these flags and exits
    zero -- which is what decides whether the watcher is available at all.

    It deliberately asserts nothing about the *content*. A CI runner has no
    console session and never locks, so the only machine that can confirm a
    real transition is a Mac someone is sitting at.
    """
    lock_module.read_ioreg()
    assert ScreenLock().available


def test_the_filter_key_is_one_root_actually_has() -> None:
    """Guards the trap this command already fell into once.

    ``-k`` matches an object's own properties. Asking for the lock flag itself
    matches nothing, prints a bare Root line, and reads as permanently
    unlocked -- silently, which is the worst way for this to fail.
    """
    assert "IOConsoleUsers" in lock_module.IOREG
    assert "CGSSessionScreenIsLocked" not in lock_module.IOREG
