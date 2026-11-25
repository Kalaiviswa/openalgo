"""
Shared rate limiting and 429-retry helpers for all Fyers API calls.

Fyers publishes its limits per API category and per plan (Plans & Limits,
https://fyers.in/api-docs/v3/getting-started/plans-and-limits/, last updated
14 Oct 2026; fyers-api-docs/FYERS_CHANGES_OCT_NOV_2026.md):

| Category | Standard (free) | Prime (subscription) |
| --- | --- | --- |
| Data (quotes, depth, history, option chain) | 5/s, 50/min, 5,000/day | 10/s, 500/min, 5,00,000/day |
| Transaction and user info | 10/s, 200/min, 1,00,000/day | 10/s, 500/min, 5,00,000/day |

Every window applies at once, and going over the per-minute limit more than
three times in a day blocks the account for the rest of the day. So each
category has its own budget (a burst of quotes never queues an order behind
it), paced per second and over a rolling minute, and every call also keeps
the old process-wide spacing of 0.125 s as a ceiling.

The trader picks the plan on Broker Configuration
(``database.broker_keys_db.get_rate_plan``); until they do, the Standard
limits apply, because pacing a Prime account as Standard is slow but pacing a
Standard account as Prime gets it blocked. The plan is re-read at most every
``PLAN_TTL`` seconds, so a change made in the app reaches the websocket proxy
process too. The day's count is kept in memory only: it is not enforced here
(Fyers' own answer stays authoritative), it is logged once when crossed.
OpenAlgo Desktop does the same in ``src-tauri/src/brokers/fyers/ratelimit.rs``.

Services create a fresh BrokerData(auth_token) per request (see
services/option_chain_service.py, services/oi_tracker_service.py, etc.), so
pacing state MUST live here at module level, not on an instance.
"""

import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from utils.broker_backpressure import BrokerBusyError, cap_server_delay, check_queue_wait
from utils.logging import get_logger

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

STANDARD = "standard"
PRIME = "prime"

DATA = "data"
TRANSACTION = "transaction"
USER_INFO = "user_info"

#: (per second, per minute, per day) for each plan and category.
LIMITS = {
    STANDARD: {
        DATA: (5, 50, 5_000),
        TRANSACTION: (10, 200, 100_000),
        USER_INFO: (10, 200, 100_000),
    },
    PRIME: {
        DATA: (10, 500, 500_000),
        TRANSACTION: (10, 500, 500_000),
        USER_INFO: (10, 500, 500_000),
    },
}

# Process-wide spacing every call keeps, whatever its category (about 8 a
# second, under the 10 a second every category allows, leaving headroom for
# clock jitter).
MIN_INTERVAL = 0.125
# Pace each category's per-second limit with the same 25% headroom.
_SECOND_HEADROOM = 1.25
# A rolling minute one second longer than Fyers', so a request at the edge of
# our window can never land inside Fyers' previous one.
_MINUTE = 61.0

PLAN_TTL = 30.0

MAX_RETRIES = 3
BASE_BACKOFF = 1.0  # seconds; exponential fallback when no Retry-After header: 1, 2, 4

_USER_INFO_PATHS = ("/api/v3/profile", "/api/v3/funds", "/api/v3/holdings")

_lock = threading.Lock()
_last_call_time = 0.0
_last_by_category = {DATA: 0.0, TRANSACTION: 0.0, USER_INFO: 0.0}
_minute = {DATA: deque(), TRANSACTION: deque(), USER_INFO: deque()}
_day = {"date": None, DATA: 0, TRANSACTION: 0, USER_INFO: 0}
_day_warned = set()
_plan_cache = {"plan": None, "read_at": 0.0}


def category_of(path, kind="data"):
    """The Fyers API category of a call.

    ``/data/...`` is market data; GET profile, funds and holdings are user
    info; every other ``/api/v3`` call (orders, positions, trades, GTT,
    margin) is a transaction. Without a path, ``kind="order"`` means a
    transaction and anything else market data.
    """
    if not path:
        return TRANSACTION if kind == "order" else DATA
    path = path.split("?", 1)[0]
    if path.startswith("/data/"):
        return DATA
    if path in _USER_INFO_PATHS:
        return USER_INFO
    return TRANSACTION


def current_plan():
    """The plan the trader chose for Fyers, ``standard`` until they choose."""
    now = time.monotonic()
    cached = _plan_cache["plan"]
    if cached is not None and now - _plan_cache["read_at"] < PLAN_TTL:
        return cached
    plan = STANDARD
    try:
        from database.broker_keys_db import get_rate_plan

        plan = get_rate_plan("fyers") or STANDARD
    except Exception:
        # The stricter plan is the safe answer when the choice cannot be read.
        logger.debug("Fyers API plan could not be read; pacing for Standard", exc_info=True)
    if plan not in LIMITS:
        plan = STANDARD
    _plan_cache["plan"], _plan_cache["read_at"] = plan, now
    return plan


def forget_plan():
    """Drop the cached plan, so the next call reads the saved one."""
    _plan_cache["plan"] = None


def reset():
    """Forget every booked slot and today's counts (tests and a fresh start)."""
    global _last_call_time
    with _lock:
        _last_call_time = 0.0
        for category in _last_by_category:
            _last_by_category[category] = 0.0
            _minute[category].clear()
        _day.update({"date": None, DATA: 0, TRANSACTION: 0, USER_INFO: 0})
        _day_warned.clear()


def _count_day(category, plan, per_day):
    """Count the call against today's (IST) total; log once when it is crossed."""
    today = datetime.now(IST).date()
    if _day["date"] != today:
        _day.update({"date": today, DATA: 0, TRANSACTION: 0, USER_INFO: 0})
        _day_warned.clear()
    _day[category] += 1
    if _day[category] > per_day and category not in _day_warned:
        _day_warned.add(category)
        logger.warning(
            f"Fyers {category} requests today passed the {plan} plan's daily limit "
            f"of {per_day}; Fyers may refuse further requests until tomorrow"
        )


def apply_rate_limit(kind="data", path=None):
    """Block the calling thread until it is safe to make another Fyers API call.

    Every caller books its slot under one lock: no sooner than the process
    spacing, its category's per-second spacing, and the moment its category's
    rolling minute has room. A burst therefore queues. Under the gthread
    worker each queued caller holds a request thread, so one whose slot is
    further away than ``utils.broker_backpressure.max_queue_wait(kind)`` is
    refused before it books anything. Under eventlet and the dev server there
    is no bound.

    Args:
        kind: ``"data"``, or ``"order"`` for order placement and changes.
        path: The endpoint path, which decides the Fyers category.

    Raises:
        BrokerBusyError: Under gthread, when the slot is too far away.
    """
    global _last_call_time
    category = category_of(path, kind)
    plan = current_plan()  # may read the database: outside the lock
    per_second, per_minute, per_day = LIMITS[plan][category]
    spacing = _SECOND_HEADROOM / per_second

    with _lock:
        now = time.time()
        window = _minute[category]
        while window and window[0] <= now - _MINUTE:
            window.popleft()
        slot = max(now, _last_call_time + MIN_INTERVAL, _last_by_category[category] + spacing)
        if len(window) >= per_minute:
            # The booked slots are in order: the one per_minute places back
            # must leave the rolling minute before this one may go.
            slot = max(slot, window[-per_minute] + _MINUTE)
        sleep_time = slot - now
        check_queue_wait(sleep_time, kind)
        window.append(slot)
        _last_call_time = max(_last_call_time, slot)
        _last_by_category[category] = slot
        _count_day(category, plan, per_day)

    if sleep_time > 0:
        time.sleep(sleep_time)


def retry_delay_from_headers(headers, attempt, kind="data"):
    """Compute how long to wait before retrying a 429.

    Fyers documents both `Retry-After` (seconds) and `X-Retry-After-Ms`
    (milliseconds) response headers on rate-limited requests -- prefer those
    over a blind exponential guess when present.

    The broker's delay is honoured as asked, except under the gthread worker
    when it is longer than ``max_queue_wait(kind)``: sleeping it there would
    hold a request thread for as long as the broker likes (three retries of a
    60 second Retry-After is three minutes), so the retry is abandoned instead.

    Raises:
        BrokerBusyError: Under gthread, when the broker asks for a longer wait
            than the bound.
    """
    delay = _requested_delay(headers, attempt)
    if cap_server_delay(delay, kind) is None:
        raise BrokerBusyError(
            "Fyers asked OpenAlgo to slow down and wait longer than a request "
            "can be held. This request was not retried. Try again in a minute.",
            retry_after=delay,
        )
    return delay


def _requested_delay(headers, attempt):
    retry_after_ms = headers.get("X-Retry-After-Ms") or headers.get("x-retry-after-ms")
    if retry_after_ms:
        try:
            return max(float(retry_after_ms) / 1000.0, 0.05)
        except ValueError:
            pass

    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    if retry_after:
        try:
            return max(float(retry_after), 0.05)
        except ValueError:
            pass

    return BASE_BACKOFF * (2**attempt)
