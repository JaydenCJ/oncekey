"""Shared fixtures: a fake clock and a ledger on a temp file.

Everything time-dependent (leases, TTLs, purge ages) is driven by
:class:`FakeClock`, so no test ever sleeps and lease-expiry behavior is
tested deterministically.
"""

from __future__ import annotations

import pytest

from oncekey import Ledger


class FakeClock:
    """A manually-advanced clock, injectable as ``Ledger(clock=...)``."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path) -> str:
    return str(tmp_path / "ledger.db")


@pytest.fixture
def ledger(db_path, clock):
    led = Ledger(db_path, clock=clock, lease_seconds=60.0)
    yield led
    led.close()
