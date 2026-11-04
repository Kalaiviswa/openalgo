# services/flow_scheduler_service.py
"""
Flow Workflow Scheduler Service
Handles scheduled workflow execution using APScheduler (Flask/sync version)
"""

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Optional

from apscheduler.events import EVENT_JOB_MISSED
from apscheduler.jobstores.base import JobLookupError
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import STATE_STOPPED
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from database.apscheduler_jobstore_db import (
    FLOW_JOBSTORE_TABLE,
    ensure_jobstore_table,
    get_database_url,
)
from database.engine_factory import create_db_engine
from utils.env_config import env_int
from utils.logging import get_logger

logger = get_logger(__name__)


#: Seconds past a boundary that an aligned interval job first fires at.
#:
#: Not zero: firing exactly on the minute races the bar that is closing, and the
#: feed may or may not have opened the next one, which are answers a whole candle
#: apart. Kept small so a one-minute strategy still acts on the candle that just
#: closed. Tunable because how long a feed takes to settle is a property of the
#: broker and the instrument, not of this scheduler.
INTERVAL_ALIGN_OFFSET_SECONDS = env_int("FLOW_INTERVAL_ALIGN_OFFSET", 2, minimum=0)


#: Seconds between the watchdog's checks on the scheduler thread (#2326).
WATCHDOG_INTERVAL_SECONDS = env_int("FLOW_SCHEDULER_WATCHDOG_INTERVAL", 30, minimum=5)

#: How far past its due time a job may sit before the watchdog nudges the
#: scheduler awake. Well inside the 60s misfire grace, so a nudged job still runs.
OVERDUE_WAKE_SECONDS = 30

#: How long a job may stay overdue, despite the nudges, before the watchdog
#: reports the scheduler as stuck.
STUCK_AFTER_SECONDS = 300

MISFIRE_GRACE_SECONDS = 60

#: Set while the scheduler's own loop is processing jobs, so the job store can
#: tell its bookkeeping writes apart from writes made on behalf of an API call.
_in_scheduler_loop = threading.local()


def _sort_by_next_run(jobs: list) -> list:
    """Due jobs first, paused jobs (no next run time) last, as APScheduler sorts."""
    return sorted(
        jobs,
        key=lambda job: (job.next_run_time is None, job.next_run_time or datetime.min),
    )


class _ResilientJobStore(SQLAlchemyJobStore):
    """A job store whose failed bookkeeping write cannot stop the scheduler.

    After each run APScheduler saves the job's next run time with
    ``update_job``. APScheduler 3.x calls that outside any error handling, so a
    single failure there (a write lock outlasting the database layer's retry
    budget, or a dropped connection) escaped the loop and killed the scheduler
    thread for good. Nothing was logged, the scheduler still reported itself
    running, and every job stayed in the store with its next run time in the
    past until the process restarted (issue #2326).

    A failed write from the loop is kept here and retried before the next
    lookup, and in the meantime every read sees the unsaved next run time.
    Without that, the job would look due again at the run time that was just
    executed and fire a second time within the misfire grace, which for a flow
    with order nodes is a duplicate order. Writes made for an API call (pause,
    resume, replace) still raise, so the caller learns it did not take.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._unsaved = {}
        self._unsaved_lock = threading.Lock()

    def _forget(self, job_id: str) -> None:
        with self._unsaved_lock:
            self._unsaved.pop(job_id, None)

    def _overlay(self, jobs: list) -> list:
        """Replace each job with its unsaved version, where there is one."""
        with self._unsaved_lock:
            unsaved = dict(self._unsaved)
        if not unsaved:
            return jobs
        return _sort_by_next_run([unsaved.get(job.id, job) for job in jobs])

    def _save_unsaved(self) -> None:
        """Try again to write the next run times that failed to save."""
        with self._unsaved_lock:
            pending = list(self._unsaved.values())
        for job in pending:
            try:
                super().update_job(job)
            except JobLookupError:
                # Removed meanwhile; nothing left to save.
                pass
            except Exception:
                # Still failing. Leave the rest for the next pass.
                return
            with self._unsaved_lock:
                if self._unsaved.get(job.id) is job:
                    del self._unsaved[job.id]
            logger.info(f"Saved the delayed next run time for scheduler job {job.id}")

    def update_job(self, job):
        try:
            super().update_job(job)
        except JobLookupError:
            self._forget(job.id)
            raise
        except Exception:
            if not getattr(_in_scheduler_loop, "active", False):
                raise
            with self._unsaved_lock:
                self._unsaved[job.id] = job
            logger.exception(
                f"Could not save the next run time ({job.next_run_time}) for scheduler "
                f"job {job.id}; keeping it in memory and retrying"
            )
        else:
            self._forget(job.id)

    def get_due_jobs(self, now):
        self._save_unsaved()
        return [
            job
            for job in self._overlay(super().get_due_jobs(now))
            if job.next_run_time is not None and job.next_run_time <= now
        ]

    def get_next_run_time(self):
        with self._unsaved_lock:
            has_unsaved = bool(self._unsaved)
        if not has_unsaved:
            return super().get_next_run_time()
        times = [job.next_run_time for job in self.get_all_jobs() if job.next_run_time]
        return min(times) if times else None

    def lookup_job(self, job_id):
        job = super().lookup_job(job_id)
        if job is None:
            return None
        return self._overlay([job])[0]

    def get_all_jobs(self):
        return self._overlay(super().get_all_jobs())

    def add_job(self, job):
        super().add_job(job)
        self._forget(job.id)

    def remove_job(self, job_id):
        super().remove_job(job_id)
        self._forget(job_id)

    def remove_all_jobs(self):
        super().remove_all_jobs()
        with self._unsaved_lock:
            self._unsaved.clear()


class _ResilientBackgroundScheduler(BackgroundScheduler):
    """A BackgroundScheduler whose loop survives an error in one pass.

    APScheduler 3.x runs ``_process_jobs`` in a bare ``while`` loop, so any
    exception it raises ends the scheduler thread silently (issue #2326). Here a
    failed pass is logged and retried after ``jobstore_retry_interval`` seconds,
    the same back-off APScheduler already uses when ``get_due_jobs`` fails.
    """

    def _process_jobs(self):
        _in_scheduler_loop.active = True
        try:
            return super()._process_jobs()
        except Exception:
            logger.exception(
                f"Flow scheduler pass failed; retrying in {self.jobstore_retry_interval:g}s"
            )
            return self.jobstore_retry_interval
        finally:
            _in_scheduler_loop.active = False


def _log_missed_run(event) -> None:
    """Report a scheduled run that was skipped for starting too late.

    APScheduler drops a run that misses the grace window and logs only a
    generic line from its own logger, so a lost run never showed up as a flow
    event.
    """
    if not str(event.job_id).startswith("flow_workflow_"):
        return
    logger.warning(
        f"Skipped the scheduled run of {event.job_id} due at "
        f"{event.scheduled_run_time:%Y-%m-%d %H:%M:%S %Z}: it could not start within "
        f"{MISFIRE_GRACE_SECONDS}s of its due time"
    )


def _next_aligned_start(value: int, unit: str) -> datetime:
    """The next clock boundary for an interval schedule, plus a small offset.

    A 5-minute job lands on :00, :05, :10 rather than five minutes after
    whenever it was switched on. Sub-minute intervals are left alone: there is
    no meaningful boundary to align a 10-second job to, and the offset would
    cost more than the alignment is worth.
    """
    now = datetime.now()
    if unit == "seconds":
        return now + timedelta(seconds=1)

    step = timedelta(hours=value) if unit == "hours" else timedelta(minutes=value)
    anchor = now.replace(minute=0, second=0, microsecond=0)
    if unit != "hours":
        anchor = now.replace(second=0, microsecond=0)
        # Back up to the last boundary this interval divides into the hour on.
        anchor -= timedelta(minutes=anchor.minute % value)

    start = anchor + timedelta(seconds=INTERVAL_ALIGN_OFFSET_SECONDS)
    while start <= now:
        start += step
    return start


class FlowScheduler:
    """Singleton scheduler for Flow workflows"""

    _instance: Optional["FlowScheduler"] = None
    _scheduler: BackgroundScheduler | None = None
    _jobstore: _ResilientJobStore | None = None
    _db_url: str | None = None
    _lock = threading.Lock()
    _initialized = False
    _api_key: str | None = None

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def init(self, db_url: str = None, api_key: str = None):
        """Initialize the scheduler with database URL for job persistence"""
        if self._initialized:
            return

        with self._lock:
            if self._initialized:
                return

            if db_url is None:
                db_url = get_database_url()

            self._api_key = api_key
            self._db_url = db_url

            try:
                # Create the job store table before APScheduler would. Its own
                # DDL in start() is one-shot: an install that loses the boot
                # write-lock race never initializes the scheduler, and every
                # scheduled workflow then silently never runs for the life of
                # the process (issue #1750). app.py normally creates this during
                # the serialized database-init phase, which leaves the call
                # below a read-only no-op; it retries here for any caller that
                # starts the scheduler outside that path.
                ensure_jobstore_table(FLOW_JOBSTORE_TABLE, database_url=db_url)

                self._scheduler, self._jobstore = self._build_scheduler(db_url)
                self._scheduler.start()
                self._initialized = True
                self._start_watchdog()
                logger.debug("Flow Scheduler initialized and started")
            except Exception as e:
                logger.exception(f"Failed to initialize Flow Scheduler: {e}")
                raise

    @staticmethod
    def _build_scheduler(db_url: str, unsaved: dict | None = None):
        """A new scheduler and its job store, not yet started.

        ``unsaved`` carries next run times the previous job store could not
        write, so a restarted scheduler does not run those jobs a second time.
        """
        # engine= rather than url= so the job store uses the project NullPool
        # policy instead of SQLAlchemy's default QueuePool, which would hold
        # connections open for the life of the process. See
        # database/engine_factory.py.
        jobstore = _ResilientJobStore(
            engine=create_db_engine(db_url), tablename=FLOW_JOBSTORE_TABLE
        )
        if unsaved:
            jobstore._unsaved.update(unsaved)
        scheduler = _ResilientBackgroundScheduler(
            jobstores={"default": jobstore},
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": MISFIRE_GRACE_SECONDS,
            },
        )
        scheduler.add_listener(_log_missed_run, EVENT_JOB_MISSED)
        return scheduler, jobstore

    def _start_watchdog(self) -> None:
        """Start the thread that keeps the scheduler thread alive and awake."""
        self._watchdog_stop = threading.Event()
        self._overdue_since = None
        self._stuck_reported = False
        threading.Thread(
            target=self._watchdog_loop,
            args=(self._watchdog_stop,),
            name="FlowSchedulerWatchdog",
            daemon=True,
        ).start()

    def _watchdog_loop(self, stop: threading.Event) -> None:
        while not stop.wait(WATCHDOG_INTERVAL_SECONDS):
            try:
                self._check_scheduler()
            except Exception:
                logger.exception("Flow scheduler watchdog check failed")

    def _check_scheduler(self) -> None:
        """Restart a dead scheduler thread and wake one that is behind (#2326).

        Two ways a job can sit past its due time with nothing firing it:

        * The scheduler thread has died. The scheduler still reports itself
          running, so nothing else would ever notice. It is rebuilt here.
        * The thread is alive but asleep past the due time, as happens on
          Windows after the machine sleeps: the wait does not count the time
          spent asleep, so a 10-minute wait could end ten minutes late and
          skip the run. A wake-up makes it look at the clock again.

        A job still behind after repeated wake-ups means the thread is stuck
        in a call that never returns. That cannot be undone from here without
        risking two threads running the same jobs, so it is reported instead.
        """
        scheduler = self._scheduler
        if scheduler is None or scheduler.state == STATE_STOPPED:
            return

        thread = getattr(scheduler, "_thread", None)
        if thread is not None and not thread.is_alive():
            logger.error("The Flow scheduler thread stopped unexpectedly; restarting it")
            self._restart_scheduler()
            return

        # Read the job store directly rather than through the scheduler, whose
        # lock a stuck thread would be holding.
        next_run = self._jobstore.get_next_run_time()
        now = datetime.now(UTC)
        if next_run is None or (now - next_run).total_seconds() <= OVERDUE_WAKE_SECONDS:
            if self._stuck_reported:
                logger.info("Flow scheduler is running scheduled jobs again")
            self._overdue_since = None
            self._stuck_reported = False
            return

        if self._overdue_since is None:
            self._overdue_since = now
        scheduler.wakeup()

        stuck_for = (now - self._overdue_since).total_seconds()
        if stuck_for >= STUCK_AFTER_SECONDS and not self._stuck_reported:
            self._stuck_reported = True
            logger.error(
                f"Flow scheduler has not run a job due at {next_run:%Y-%m-%d %H:%M:%S %Z} "
                f"for {stuck_for / 60:.0f} minutes. Scheduled flows will not run until "
                "OpenAlgo is restarted."
            )

    def _restart_scheduler(self) -> None:
        """Replace a scheduler whose thread has died with a new one."""
        with self._lock:
            old_scheduler, old_store = self._scheduler, self._jobstore
            with old_store._unsaved_lock:
                unsaved = dict(old_store._unsaved)
            try:
                old_scheduler.shutdown(wait=False)
            except Exception:
                logger.exception("Could not shut down the stopped Flow scheduler cleanly")
            self._scheduler, self._jobstore = self._build_scheduler(self._db_url, unsaved)
            self._scheduler.start()
            self._overdue_since = None
            self._stuck_reported = False
            logger.info("Flow scheduler restarted")

    def set_api_key(self, api_key: str):
        """Set API key for workflow execution"""
        self._api_key = api_key

    @property
    def scheduler(self) -> BackgroundScheduler:
        """Get the scheduler instance"""
        if self._scheduler is None:
            raise RuntimeError("Scheduler not initialized. Call init() first.")
        return self._scheduler

    @property
    def api_key(self) -> str | None:
        """Get the API key for workflow execution"""
        return self._api_key

    def add_workflow_job(
        self,
        workflow_id: int,
        schedule_type: str,
        time_str: str = "09:15",
        days: list | None = None,
        execute_at: str | None = None,
        interval_value: int | None = None,
        interval_unit: str | None = None,
        func: Callable = None,
        market_hours_only: bool = False,
    ) -> str:
        """Add a workflow job to the scheduler

        Args:
            workflow_id: ID of the workflow
            schedule_type: 'once', 'daily', 'weekly', or 'interval'
            time_str: Time string in HH:MM format (for daily/weekly/once)
            days: List of days for weekly schedule (0=Mon, 6=Sun)
            execute_at: ISO datetime string for one-time execution
            interval_value: Interval value (e.g., 1, 5, 10)
            interval_unit: Interval unit ('seconds', 'minutes', 'hours')
            func: Function to execute (defaults to execute_workflow_scheduled)
        """
        job_id = f"flow_workflow_{workflow_id}"

        # Clear any previous job for this workflow. A brand-new workflow has
        # none, which remove_job treats as a normal no-op.
        self.remove_job(job_id)

        # Use default function if not provided
        if func is None:
            func = execute_workflow_scheduled

        trigger = None

        if schedule_type == "interval":
            value = interval_value or 1
            unit = interval_unit or "minutes"

            # Anchored to the clock, not to whenever the workflow was activated.
            # APScheduler counts from `start_date`, which defaults to "now", so
            # "every 1 minute" fired at 11:34:37, 11:35:37, ... and the phase
            # changed on every reactivation and restart. Anything reading bars
            # cares: a strategy comparing the last two closed candles needs the
            # new one to exist before it looks, and an arbitrary phase decides
            # that by luck.
            #
            # The offset puts the run just inside the new bar rather than on its
            # boundary. Firing exactly on the minute races the bar that is
            # closing: the feed may or may not have opened the next one yet, and
            # the two answers differ by a whole candle.
            start = _next_aligned_start(value, unit)

            if unit == "seconds":
                trigger = IntervalTrigger(seconds=value, start_date=start)
            elif unit == "hours":
                trigger = IntervalTrigger(hours=value, start_date=start)
            else:
                trigger = IntervalTrigger(minutes=value, start_date=start)

            logger.info(
                f"Creating interval trigger: every {value} {unit}, first run {start:%H:%M:%S}"
            )

        elif schedule_type == "once" and execute_at:
            try:
                execute_datetime = datetime.fromisoformat(execute_at.replace("Z", "+00:00"))
                trigger = DateTrigger(run_date=execute_datetime)
                logger.info(f"Creating one-time trigger: {execute_datetime}")
            except ValueError as e:
                logger.error(f"Invalid execute_at datetime: {execute_at} - {e}")
                raise ValueError(f"Invalid datetime format: {execute_at}") from e

        elif schedule_type == "daily":
            try:
                hour, minute = map(int, time_str.split(":"))
                trigger = CronTrigger(hour=hour, minute=minute)
                logger.info(f"Creating daily trigger: {time_str}")
            except ValueError as e:
                logger.error(f"Invalid time format: {time_str} - {e}")
                raise ValueError(f"Invalid time format: {time_str}") from e

        elif schedule_type == "weekly" and days:
            try:
                hour, minute = map(int, time_str.split(":"))
                day_names = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}
                day_of_week = ",".join(day_names[d] for d in days if d in day_names)
                trigger = CronTrigger(day_of_week=day_of_week, hour=hour, minute=minute)
                logger.info(f"Creating weekly trigger: {day_of_week} at {time_str}")
            except (ValueError, KeyError) as e:
                logger.error(f"Invalid weekly schedule config: {e}")
                raise ValueError("Invalid weekly schedule configuration") from e

        else:
            raise ValueError(f"Invalid schedule configuration: type={schedule_type}")

        self.scheduler.add_job(
            func,
            trigger=trigger,
            id=job_id,
            # The API key is deliberately NOT stored here. APScheduler pickles
            # these args into flow_apscheduler_jobs.job_state, which lives in the
            # same database that encrypts flow_workflows.api_key -- persisting it
            # here defeated that encryption and froze the key at activation time,
            # so regenerating it silently broke every scheduled workflow. The
            # default executor resolves the current key at run time instead.
            # Only the default executor takes the market-hours flag. A custom
            # callback still receives the documented (workflow_id, api_key) pair,
            # which passing a third positional argument would break.
            # No branch stores the key. The custom-callback path used to pass
            # self._api_key, which lands in the same pickled job_state, so the
            # leak survived for any caller supplying its own func. A custom
            # callback receives None and resolves the current key itself, the
            # same way the default executor does.
            args=(
                [workflow_id, None, market_hours_only]
                if func is execute_workflow_scheduled
                else [workflow_id, None]
            ),
            replace_existing=True,
            name=f"Workflow {workflow_id}",
        )

        logger.info(f"Added job {job_id}")
        return job_id

    def remove_job(self, job_id: str, strict: bool = False) -> bool:
        """Remove a job from the scheduler. Returns False if there was none.

        A job that does not exist is not an error for any caller: activating a
        workflow clears any prior job first (a new workflow has none), and
        deactivating may find it already gone after a restart. Logging that as
        an ERROR with a traceback made a perfectly normal activation look
        broken. A real jobstore failure is still logged with its traceback.

        `strict` separates those two cases for callers that must not proceed on
        a failed removal. Deactivation uses it: swallowing a jobstore error let
        the workflow be marked inactive while its job stayed live and kept
        trading, with the stored job id already cleared so nothing could find
        it again. A missing job still returns False rather than raising, because
        that genuinely is the desired end state.
        """
        from apscheduler.jobstores.base import JobLookupError

        try:
            self.scheduler.remove_job(job_id)
            logger.info(f"Removed job {job_id}")
            return True
        except JobLookupError:
            logger.debug(f"No scheduler job {job_id} to remove")
            return False
        except Exception:
            logger.exception(f"Failed to remove job {job_id}")
            if strict:
                raise
            return False

    def remove_workflow_job(self, workflow_id: int, strict: bool = False) -> bool:
        """Remove a workflow job. A job that is already gone is not a failure."""
        job_id = f"flow_workflow_{workflow_id}"
        return self.remove_job(job_id, strict=strict)

    def get_job(self, job_id: str):
        """Get a job by ID"""
        return self.scheduler.get_job(job_id)

    def get_workflow_job(self, workflow_id: int):
        """Get a workflow job"""
        job_id = f"flow_workflow_{workflow_id}"
        return self.get_job(job_id)

    def get_next_run_time(self, job_id: str) -> datetime | None:
        """Get the next run time for a job"""
        job = self.get_job(job_id)
        if job:
            return job.next_run_time
        return None

    def get_all_jobs(self) -> list:
        """Get all scheduled jobs"""
        return self.scheduler.get_jobs()

    def pause_job(self, job_id: str) -> bool:
        """Pause a job"""
        try:
            self.scheduler.pause_job(job_id)
            logger.info(f"Paused job {job_id}")
            return True
        except Exception as e:
            logger.exception(f"Failed to pause job {job_id}: {e}")
            return False

    def resume_job(self, job_id: str) -> bool:
        """Resume a paused job"""
        try:
            self.scheduler.resume_job(job_id)
            logger.info(f"Resumed job {job_id}")
            return True
        except Exception as e:
            logger.exception(f"Failed to resume job {job_id}: {e}")
            return False

    def shutdown(self):
        """Shutdown the scheduler"""
        stop = getattr(self, "_watchdog_stop", None)
        if stop is not None:
            stop.set()
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._initialized = False
            logger.info("Flow Scheduler shutdown")


#: Exchange whose calendar is consulted when the trigger node names none. This
#: selects a calendar, not a time - the hours themselves always come from the
#: market-calendar tables.
DEFAULT_MARKET_HOURS_EXCHANGE = "NSE"


def _parse_hhmm(value) -> int | None:
    """"HH:MM" as minutes past midnight, or None if it is not a valid time.

    Returning None rather than a fallback time matters: the caller then falls
    through to the exchange calendar instead of silently trading to a time
    nobody configured.
    """
    if value is None:
        return None
    try:
        hours, minutes = (int(part) for part in str(value).strip().split(":", 1))
    except (ValueError, TypeError):
        return None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return hours * 60 + minutes


def is_within_market_hours(
    now: datetime | None = None,
    *,
    start: str | None = None,
    end: str | None = None,
    exchange: str | None = None,
) -> bool:
    """Whether ``now`` falls inside the workflow's trading window (IST).

    The editor offers a "market hours only" switch on the schedule trigger and
    defaults it on, but nothing read it, so an interval schedule kept firing
    overnight and at weekends.

    No trading time is hardcoded here. The day is resolved through
    ``get_effective_session_window``, the same calendar the rest of the
    platform uses, so weekends, trading holidays, muhurat and other special
    sessions, and per-exchange hours are all inherited rather than restated.
    That calendar knows MCX runs to 23:55 and CRYPTO never closes, which a
    fixed 09:15-15:30 window got wrong for every non-equity workflow.

    Args:
        now: IST-aware instant to test. Defaults to the current time.
        start: Optional "HH:MM" override from the workflow's trigger node.
        end: Optional "HH:MM" override from the workflow's trigger node.
        exchange: Calendar to consult. Defaults to NSE.

    Returns:
        False whenever the exchange is shut that day, regardless of any
        override - an override narrows or extends the clock, it does not
        reopen a holiday.
    """
    import pytz

    from database.market_calendar_db import get_effective_session_window

    ist = pytz.timezone("Asia/Kolkata")
    now = now or datetime.now(ist)

    exch = (exchange or DEFAULT_MARKET_HOURS_EXCHANGE).upper()
    window = get_effective_session_window(now.date(), exch)
    if window is None:
        # Shut that day: weekend, or a trading holiday for this exchange.
        return False

    start_minutes = _parse_hhmm(start)
    end_minutes = _parse_hhmm(end)

    if start_minutes is None or end_minutes is None:
        session_start = datetime.fromtimestamp(window["start_ms"] / 1000, ist)
        session_end = datetime.fromtimestamp(window["end_ms"] / 1000, ist)
        if start_minutes is None:
            start_minutes = session_start.hour * 60 + session_start.minute
        if end_minutes is None:
            end_minutes = session_end.hour * 60 + session_end.minute

    minutes = now.hour * 60 + now.minute
    return start_minutes <= minutes <= end_minutes


def reconcile_scheduler_jobs() -> dict:
    """Bring the persistent jobstore and the database back into agreement.

    Activation writes two things -- a row flag and a scheduler job -- and a
    crash, a failed write or a hand-edited database can leave those disagreeing.
    The jobstore is persistent, so a stale job is restored at every boot and
    keeps trading a workflow the user believes is off, while a missing job
    leaves a workflow that reports Active and never fires. Neither state can
    correct itself: deactivate short-circuits on `already_inactive`, and
    activate refuses an `already_active` workflow.

    Run once at startup, after the Flow database and the scheduler are up.

    Returns counts of what it changed.
    """
    from database.flow_db import get_active_workflows, get_workflow, set_schedule_job_id

    scheduler = get_flow_scheduler()
    removed = 0
    restored = 0

    # Orphans: a job whose workflow is gone or no longer active.
    for job in scheduler.get_all_jobs():
        job_id = str(getattr(job, "id", ""))
        if not job_id.startswith("flow_workflow_"):
            continue
        try:
            workflow_id = int(job_id.rsplit("_", 1)[1])
        except (IndexError, ValueError):
            continue

        workflow = get_workflow(workflow_id)
        if workflow is None or not workflow.is_active:
            reason = "no longer exists" if workflow is None else "is not active"
            logger.warning(
                f"Removing orphaned scheduler job {job_id}: the workflow {reason}."
            )
            if scheduler.remove_job(job_id):
                removed += 1

    # The mirror case: active, scheduled, but nothing registered to fire it.
    for workflow in get_active_workflows():
        trigger = next(
            (n for n in (workflow.nodes or []) if n.get("type") == "start"), None
        )
        if not trigger:
            continue
        data = trigger.get("data", {}) or {}
        schedule_type = data.get("scheduleType")
        if not schedule_type or schedule_type == "manual":
            continue
        if scheduler.get_workflow_job(workflow.id) is not None:
            continue

        try:
            job_id = scheduler.add_workflow_job(
                workflow_id=workflow.id,
                schedule_type=schedule_type,
                time_str=data.get("time", "09:15"),
                days=data.get("days"),
                execute_at=data.get("executeAt"),
                interval_value=data.get("intervalValue"),
                interval_unit=data.get("intervalUnit"),
                market_hours_only=bool(data.get("marketHoursOnly", False)),
            )
            set_schedule_job_id(workflow.id, job_id)
            restored += 1
            logger.warning(
                f"Restored missing scheduler job for active workflow {workflow.id}."
            )
        except Exception:
            # A one-shot schedule whose time has passed cannot be rebuilt, and
            # that is not a failure worth blocking startup for.
            logger.exception(
                f"Could not restore the scheduler job for active workflow "
                f"{workflow.id}; it will not fire until it is reactivated"
            )

    if removed or restored:
        logger.info(
            f"Scheduler reconciliation: removed {removed} orphaned job(s), "
            f"restored {restored} missing job(s)"
        )
    return {"removed": removed, "restored": restored}


def get_market_hours_config(workflow) -> dict:
    """Market-hours settings from a workflow's trigger node.

    Read from the graph on every run rather than baked into the scheduler job,
    so editing the times in the flow JSON takes effect on the next run - the
    same way node edits already do - instead of needing a deactivate and
    reactivate cycle.
    """
    for node in getattr(workflow, "nodes", None) or []:
        if node.get("type") == "start":
            data = node.get("data") or {}
            return {
                "enabled": bool(data.get("marketHoursOnly", False)),
                "start": data.get("marketHoursStart") or None,
                "end": data.get("marketHoursEnd") or None,
                "exchange": data.get("marketHoursExchange") or None,
            }
    return {"enabled": False, "start": None, "end": None, "exchange": None}


def execute_workflow_scheduled(
    workflow_id: int, api_key: str = None, market_hours_only: bool = False
):
    """Execute a workflow from scheduler (synchronous).

    `api_key` is accepted only so jobs pickled by an older build, which stored
    the key in the jobstore, still run. New jobs pass None and the current key
    is decrypted from the workflow row on every run.
    """
    from services.flow_executor_service import execute_workflow

    logger.info(f"Scheduled execution of workflow {workflow_id}")

    # The window is read from the workflow's trigger node on every run, so
    # changing the times in the flow JSON applies from the next run. The
    # market_hours_only argument is what older jobs stored in the jobstore and
    # is honoured only when the workflow itself cannot be read.
    config = {"enabled": market_hours_only, "start": None, "end": None, "exchange": None}
    try:
        from database.flow_db import get_workflow, get_workflow_api_key

        workflow = get_workflow(workflow_id)
        if workflow is None:
            logger.warning(
                f"Skipping scheduled workflow {workflow_id}: it no longer exists"
            )
            return

        # Fail closed. A job can outlive the deactivation that should have
        # removed it -- a failed jobstore write, a crash between the two steps,
        # or an orphan restored from the jobstore at boot -- and without this
        # check it keeps placing live orders against a workflow the user
        # believes is switched off.
        if not workflow.is_active:
            logger.warning(
                f"Skipping scheduled workflow {workflow_id}: it is not active"
            )
            return

        if not api_key:
            api_key = get_workflow_api_key(workflow)

        # The graph is the source of truth whenever it can be read, including
        # when the user switches market-hours gating off. Treating a disabled
        # switch as "nothing to say" made the setting one-way: it could be
        # turned on from the editor but never off.
        config = get_market_hours_config(workflow)
    except Exception:
        # A lookup failure must not silently drop the gate and let a workflow
        # trade at 3am, so the stored flag stands.
        logger.exception(
            f"Could not read scheduling config for workflow {workflow_id}; "
            "using the schedule's stored setting"
        )

    if not api_key:
        logger.error(f"No API key available for workflow {workflow_id}")
        return

    if config["enabled"] and not is_within_market_hours(
        start=config["start"], end=config["end"], exchange=config["exchange"]
    ):
        logger.debug(
            f"Skipping scheduled workflow {workflow_id}: outside market hours"
        )
        return

    try:
        result = execute_workflow(workflow_id, api_key=api_key)
        logger.info(
            f"Scheduled execution result for workflow {workflow_id}: {result.get('status')}"
        )
    except Exception as e:
        logger.exception(f"Scheduled execution failed for workflow {workflow_id}: {e}")
    finally:
        # APScheduler runs this on its own worker thread with no Flask app
        # context, so teardown_appcontext never fires and the sessions this run
        # touched would stay bound to that thread.
        from utils.db_sessions import remove_all_scoped_sessions

        remove_all_scoped_sessions()


# Global scheduler instance
flow_scheduler = FlowScheduler()


def get_flow_scheduler() -> FlowScheduler:
    """Get the global flow scheduler instance"""
    return flow_scheduler


def init_flow_scheduler(db_url: str = None, api_key: str = None):
    """Initialize the flow scheduler"""
    flow_scheduler.init(db_url=db_url, api_key=api_key)
    return flow_scheduler
