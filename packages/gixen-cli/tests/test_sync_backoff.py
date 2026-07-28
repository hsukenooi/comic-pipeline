"""BUI-562: _sync_loop's backoff schedule.

The schedule is asserted directly against _sync_backoff_delay and, for the
loop itself, by capturing what it passes to asyncio.sleep — never by actually
sleeping.

Background: the backoff was always exponential, but its base was SYNC_INTERVAL
and the exponent started at 1, so the first failure cost 1200s and the loop
could never back off shorter than 20 minutes. Since BUI-555 this loop is the
self-healing path for bids.max_bid, which _sniper_loop fires real money from.
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gixen_client import GixenConnectionError, GixenError  # noqa: E402
from server import main as smain  # noqa: E402

DELAY = smain._sync_backoff_delay
FIRST = smain._SYNC_BACKOFF_FIRST
UNEXPECTED = smain._SYNC_BACKOFF_FIRST_UNEXPECTED
MAX = smain._SYNC_BACKOFF_MAX


# --------------------------------------------------------------------------
# the pure schedule
# --------------------------------------------------------------------------

def test_zero_failures_is_the_healthy_cadence_not_the_retry_base():
    """A working loop must poll at SYNC_INTERVAL, never at the 30s retry base.

    This is the hot-spin guard: `first_delay * 2 ** (n - 1)` at n=0 would be
    half the base, so the zero-failure case has to short-circuit.
    """
    assert DELAY(0, first_delay=FIRST) == smain.SYNC_INTERVAL
    assert DELAY(-1, first_delay=FIRST) == smain.SYNC_INTERVAL


def test_first_failure_costs_the_base_not_double_it():
    """The headline fix: 30s after the first failure, not 1200s."""
    assert DELAY(1, first_delay=FIRST) == 30


def test_schedule_doubles_from_the_base_and_caps_at_one_hour():
    got = [DELAY(n, first_delay=FIRST) for n in range(1, 10)]
    assert got == [30, 60, 120, 240, 480, 960, 1920, 3600, 3600]
    assert all(d <= MAX for d in got)


def test_cap_holds_for_absurd_failure_counts():
    """consecutive_failures is unbounded — it has reached 177 in production."""
    for n in (33, 177, 10_000, 10**6):
        assert DELAY(n, first_delay=FIRST) == MAX


def test_schedule_is_monotonically_non_decreasing():
    delays = [DELAY(n, first_delay=FIRST) for n in range(1, 60)]
    assert delays == sorted(delays)


def test_time_to_reach_the_cap_stays_about_an_hour():
    """A short first retry must not turn a real outage into a retry storm.

    Summing the sub-cap steps bounds the extra load: the whole ramp fits in
    roughly one hour and costs ~7 attempts, after which it is hourly — the
    same steady state the old schedule reached (in 2 attempts).
    """
    ramp = [d for d in (DELAY(n, first_delay=FIRST) for n in range(1, 40)) if d < MAX]
    assert len(ramp) == 7
    assert 3000 < sum(ramp) < 4200


def test_unexpected_exception_base_reproduces_the_old_schedule_exactly():
    """The evidence is about Gixen connectivity; leave our own bugs alone."""
    for n in range(1, 12):
        assert DELAY(n, first_delay=UNEXPECTED) == min(
            smain.SYNC_INTERVAL * (2 ** n), MAX
        ), f"n={n} drifted from the pre-BUI-562 SYNC_INTERVAL * 2**n"
    assert DELAY(1, first_delay=UNEXPECTED) == 1200


def test_connectivity_retries_sooner_than_an_unexpected_exception():
    assert DELAY(1, first_delay=FIRST) < DELAY(1, first_delay=UNEXPECTED)


# --------------------------------------------------------------------------
# the loop's use of it
# --------------------------------------------------------------------------

class _StopLoop(Exception):
    """Breaks out of the otherwise-infinite _sync_loop."""


async def _drive(outcomes):
    """Run _sync_loop over `outcomes`, returning the delays it slept for.

    Each outcome is either None (a successful sync) or an exception instance
    to raise. asyncio.sleep is stubbed, so this never actually waits.
    """
    delays: list[int] = []

    async def fake_sleep(d):
        delays.append(d)
        if len(delays) >= len(outcomes):
            raise _StopLoop

    with patch.object(smain, "_sync_client", MagicMock()), \
            patch.object(smain, "_get_db", MagicMock(return_value=MagicMock())), \
            patch.object(smain, "_sync_gixen", AsyncMock(side_effect=list(outcomes))), \
            patch.object(asyncio, "sleep", fake_sleep):
        with pytest.raises(_StopLoop):
            await smain._sync_loop()
    return delays


def _conn_err():
    return GixenConnectionError("curl exit 52")


def test_loop_backs_off_from_30s_on_consecutive_connectivity_failures():
    delays = asyncio.run(_drive([_conn_err(), _conn_err(), _conn_err(), _conn_err()]))
    assert delays == [30, 60, 120, 240]


def test_loop_resets_to_the_healthy_interval_after_a_success():
    """A recovered sync must go straight back to SYNC_INTERVAL, not keep ramping."""
    delays = asyncio.run(_drive([_conn_err(), _conn_err(), None, _conn_err()]))
    assert delays == [30, 60, smain.SYNC_INTERVAL, 30]


def test_loop_never_sleeps_less_than_the_interval_while_healthy():
    delays = asyncio.run(_drive([None, None, None]))
    assert delays == [smain.SYNC_INTERVAL] * 3


def test_a_plain_gixen_error_gets_the_short_connectivity_backoff_too():
    """GixenConnectionError subclasses GixenError; both are the Gixen-side class."""
    delays = asyncio.run(_drive([GixenError("login cooldown active"), _conn_err()]))
    assert delays == [30, 60]


def test_unexpected_exception_keeps_the_slow_schedule():
    delays = asyncio.run(_drive([RuntimeError("bug"), RuntimeError("bug")]))
    assert delays == [1200, 2400]


def test_an_unexpected_exception_after_connectivity_failures_slows_back_down():
    """The counter tracks how long sync has been broken; the base tracks what
    broke it most recently. A bug surfacing mid-outage must not keep us
    probing on the fast schedule."""
    delays = asyncio.run(_drive([_conn_err(), _conn_err(), RuntimeError("bug")]))
    assert delays == [30, 60, MAX]  # 1200 * 2**2 = 4800, capped


def test_loop_still_logs_every_failure_with_count_and_delay(caplog):
    """The warning is the only operator-visible signal that sync is degraded;
    a faster retry must not cost us the ability to grep it."""
    import logging
    with caplog.at_level(logging.WARNING, logger="server.main"):
        asyncio.run(_drive([_conn_err(), _conn_err()]))
    msgs = [r.getMessage() for r in caplog.records if "_sync_loop" in r.getMessage()]
    assert len(msgs) == 2
    assert "1 consecutive failure(s)" in msgs[0] and "sleeping 30s" in msgs[0]
    assert "2 consecutive failure(s)" in msgs[1] and "sleeping 60s" in msgs[1]
    assert "GixenConnectionError" in msgs[0]


def test_no_warning_is_logged_while_healthy(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="server.main"):
        asyncio.run(_drive([None, None]))
    assert not [r for r in caplog.records if "_sync_loop:" in r.getMessage()]
