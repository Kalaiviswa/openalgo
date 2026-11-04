"""The Flow scheduler must survive a failed job-store write (issue #2326).

After each run APScheduler 3.x saves the job's next run time with
``update_job``, outside any error handling, in a bare ``while`` loop. One
failure there (a write lock outlasting the database layer's retry budget, a
dropped connection) killed the scheduler thread for good: the scheduler still
reported itself running, nothing was logged, and every job stayed in the store
with its next run time in the past until the process was restarted.

The first test asserts that defect on plain APScheduler, so the others cannot
pass without it existing.
"""

import os
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta

import pytest
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.engine_factory import create_db_engine  # noqa: E402
from services import flow_scheduler_service as fss  # noqa: E402

#: Monotonic start time of every run of ``_record_run``.
RUNS: list[float] = []


def _record_run():
    RUNS.append(time.monotonic())


@pytest.fixture
def db_url(tmp_path):
    RUNS.clear()
    return f"sqlite:///{tmp_path / 'jobs.db'}"


@pytest.fixture
def fail_next_update(monkeypatch):
    """Make the next ``count`` job-store writes fail as a held write lock does."""
    original = SQLAlchemyJobStore.update_job
    remaining = {"count": 0}

    def flaky(self, job):
        if remaining["count"] > 0:
            remaining["count"] -= 1
            raise sqlite3.OperationalError("database is locked")
        return original(self, job)

    monkeypatch.setattr(SQLAlchemyJobStore, "update_job", flaky)

    def arm(count=1):
        remaining["count"] = count

    return arm


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _bare_flow_scheduler(db_url):
    """A FlowScheduler that is not the process-wide singleton."""
    flow = object.__new__(fss.FlowScheduler)
    flow._db_url = db_url
    flow._scheduler, flow._jobstore = fss.FlowScheduler._build_scheduler(db_url)
    flow._overdue_since = None
    flow._stuck_reported = False
    return flow


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_plain_apscheduler_thread_dies_on_one_failed_write(db_url, fail_next_update):
    """The defect: one failed write ends the scheduler thread, silently."""
    store = SQLAlchemyJobStore(engine=create_db_engine(db_url), tablename="jobs")
    scheduler = BackgroundScheduler(jobstores={"default": store})
    scheduler.start()
    try:
        fail_next_update()
        scheduler.add_job(_record_run, "interval", seconds=1, id="flow_workflow_1")
        assert _wait_for(lambda: not scheduler._thread.is_alive())
        time.sleep(2.5)

        assert len(RUNS) == 1
        assert scheduler.running, "the scheduler still reports itself running"
        assert scheduler.get_job("flow_workflow_1").next_run_time < datetime.now(UTC)
    finally:
        scheduler.shutdown(wait=False)


def test_failed_write_neither_stops_the_scheduler_nor_reruns_the_job(db_url, fail_next_update):
    scheduler, _ = fss.FlowScheduler._build_scheduler(db_url)
    scheduler.start()
    try:
        fail_next_update()
        scheduler.add_job(_record_run, "interval", seconds=1, id="flow_workflow_1")
        assert _wait_for(lambda: len(RUNS) >= 4, timeout=8)

        assert scheduler._thread.is_alive()
        gaps = [later - earlier for earlier, later in zip(RUNS, RUNS[1:], strict=False)]
        # A run whose next run time failed to save must not fire again at the
        # same run time; that would be a duplicate order in a flow that trades.
        assert min(gaps) > 0.5, f"job ran twice for one run time: {gaps}"
    finally:
        scheduler.shutdown(wait=False)


def test_unsaved_next_run_time_is_written_once_the_database_recovers(db_url, fail_next_update):
    scheduler, store = fss.FlowScheduler._build_scheduler(db_url)
    scheduler.start()
    try:
        fail_next_update()
        scheduler.add_job(_record_run, "interval", seconds=1, id="flow_workflow_1")
        assert _wait_for(lambda: len(RUNS) >= 3, timeout=6)

        assert not store._unsaved
        saved = SQLAlchemyJobStore.lookup_job(store, "flow_workflow_1").next_run_time
        assert saved > datetime.now(UTC) - timedelta(seconds=2)
    finally:
        scheduler.shutdown(wait=False)


def test_write_for_an_api_call_still_raises(db_url, fail_next_update):
    """Pausing must report a failed write, not appear to succeed."""
    scheduler, store = fss.FlowScheduler._build_scheduler(db_url)
    scheduler.start()
    try:
        scheduler.add_job(_record_run, "interval", hours=1, id="flow_workflow_1")
        fail_next_update()
        with pytest.raises(sqlite3.OperationalError):
            scheduler.pause_job("flow_workflow_1")
        assert not store._unsaved
    finally:
        scheduler.shutdown(wait=False)


class _ThreadKiller(BaseException):
    """Not an Exception, so the resilient loop lets it end the thread."""


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_watchdog_restarts_a_dead_scheduler_thread(db_url):
    flow = _bare_flow_scheduler(db_url)
    flow._scheduler.start()
    try:
        flow._scheduler.add_job(_record_run, "interval", hours=1, id="flow_workflow_1")
        old = flow._scheduler

        def die():
            raise _ThreadKiller

        old._process_jobs = die
        old.wakeup()
        assert _wait_for(lambda: not old._thread.is_alive())

        flow._check_scheduler()

        assert flow._scheduler is not old
        assert flow._scheduler._thread.is_alive()
        assert flow._scheduler.get_job("flow_workflow_1") is not None
        flow._scheduler.add_job(_record_run, "date", run_date=datetime.now(UTC), id="probe")
        assert _wait_for(lambda: len(RUNS) >= 1)
    finally:
        flow._scheduler.shutdown(wait=False)


def test_watchdog_wakes_an_overdue_scheduler_and_reports_a_stuck_one(db_url):
    flow = _bare_flow_scheduler(db_url)
    # Paused: the thread is alive but processes nothing, as one asleep past
    # its due time is.
    flow._scheduler.start(paused=True)
    try:
        flow._scheduler.add_job(
            _record_run,
            "interval",
            hours=1,
            id="flow_workflow_1",
            next_run_time=datetime.now(UTC) - timedelta(minutes=2),
        )
        wakeups = []
        flow._scheduler.wakeup = lambda: wakeups.append(1)

        flow._check_scheduler()
        assert wakeups == [1]
        assert not flow._stuck_reported

        flow._overdue_since = datetime.now(UTC) - timedelta(seconds=fss.STUCK_AFTER_SECONDS + 1)
        flow._check_scheduler()
        assert flow._stuck_reported
    finally:
        flow._scheduler.shutdown(wait=False)


def test_watchdog_leaves_an_on_time_scheduler_alone(db_url):
    flow = _bare_flow_scheduler(db_url)
    flow._scheduler.start()
    try:
        flow._scheduler.add_job(_record_run, "interval", hours=1, id="flow_workflow_1")
        old = flow._scheduler
        wakeups = []
        old.wakeup = lambda: wakeups.append(1)

        flow._check_scheduler()

        assert flow._scheduler is old
        assert wakeups == []
    finally:
        flow._scheduler.shutdown(wait=False)
