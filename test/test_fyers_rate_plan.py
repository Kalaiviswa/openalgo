"""Fyers paces each API category for the trader's plan (Standard or Prime).

Fyers' Plans & Limits page (14 Oct 2026): market data 5/s, 50/min on
Standard and 10/s, 500/min on Prime; transactions and user info 10/s,
200/min on Standard and 10/s, 500/min on Prime. Going over a per-minute limit
more than three times in a day blocks the account for the day, so the
limiter must never let a minute's worth through early.
"""

import pytest

import broker.fyers.api.rate_limiter as rl


class Clock:
    """A clock the limiter reads and sleeps on, moved by hand."""

    def __init__(self):
        self.now = 1_000_000.0
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(rl.time, "time", c.time)
    monkeypatch.setattr(rl.time, "sleep", c.sleep)
    monkeypatch.setattr(rl, "_last_call_time", 0.0)
    monkeypatch.setattr(rl, "_last_by_category", dict.fromkeys(rl._last_by_category, 0.0))
    monkeypatch.setattr(rl, "_minute", {k: rl.deque() for k in rl._minute})
    monkeypatch.setattr(rl, "check_queue_wait", lambda wait, kind: None)
    return c


def _plan(monkeypatch, plan):
    monkeypatch.setattr(rl, "current_plan", lambda: plan)


def _send(n, path):
    for _ in range(n):
        rl.apply_rate_limit(path=path)


def test_categories_follow_the_endpoint():
    assert rl.category_of("/data/quotes?symbols=NSE:SBIN-EQ") == rl.DATA
    assert rl.category_of("/data/history?symbol=x") == rl.DATA
    assert rl.category_of("/api/v3/funds") == rl.USER_INFO
    assert rl.category_of("/api/v3/orders/sync") == rl.TRANSACTION
    assert rl.category_of("/api/v3/gtt/orders/sync") == rl.TRANSACTION
    assert rl.category_of(None, "order") == rl.TRANSACTION
    assert rl.category_of(None) == rl.DATA


def test_standard_data_never_sends_more_than_50_in_a_minute(clock, monkeypatch):
    _plan(monkeypatch, "standard")
    sent = []
    for _ in range(120):
        rl.apply_rate_limit(path="/data/depth")
        sent.append(clock.now)

    for i in range(len(sent)):
        in_window = [t for t in sent if sent[i] - 60 < t <= sent[i]]
        assert len(in_window) <= 50
    # Paced at 4 a second inside the minute, under the 5 a second limit.
    assert min(b - a for a, b in zip(sent, sent[1:], strict=False)) >= 0.25 - 1e-9
    # 120 requests need at least two full minutes on Standard.
    assert sent[-1] - sent[0] >= 120


def test_prime_data_allows_500_a_minute(clock, monkeypatch):
    _plan(monkeypatch, "prime")
    start = clock.now
    _send(400, "/data/depth")

    # Only the 8 a second process spacing applies: 400 in about 50 seconds.
    assert clock.now - start < 51


def test_orders_do_not_queue_behind_market_data(clock, monkeypatch):
    _plan(monkeypatch, "standard")
    _send(50, "/data/quotes")  # the data minute is now full
    before = clock.now

    rl.apply_rate_limit("order", path="/api/v3/orders/sync")

    # The order waits only for the process spacing, not for the data minute.
    assert clock.now - before <= rl.MIN_INTERVAL + 1e-9


def test_without_a_saved_plan_standard_applies(monkeypatch):
    import database.broker_keys_db as keys_db

    monkeypatch.setattr(keys_db, "get_rate_plan", lambda broker: None)
    rl.forget_plan()
    try:
        assert rl.current_plan() == "standard"
        monkeypatch.setattr(keys_db, "get_rate_plan", lambda broker: "something-else")
        rl.forget_plan()
        assert rl.current_plan() == "standard"
        monkeypatch.setattr(keys_db, "get_rate_plan", lambda broker: "prime")
        # Cached for PLAN_TTL: a change reaches another process within it.
        assert rl.current_plan() == "standard"
        rl.forget_plan()
        assert rl.current_plan() == "prime"
    finally:
        rl.forget_plan()
