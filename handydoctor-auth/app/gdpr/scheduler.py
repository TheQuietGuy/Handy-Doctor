"""
app/gdpr/scheduler.py — APScheduler wired into the FastAPI lifespan.

The scheduler runs as a background asyncio task inside the same process.
For high-volume production you'd replace this with a dedicated Celery beat
worker or a cloud scheduler (AWS EventBridge, GCP Cloud Scheduler) hitting
a protected internal endpoint — but for this scale asyncio is fine.
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.gdpr.worker import run_pending_deletions
from app.notifications.worker import run_followup_reminders, run_session_summaries

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def create_scheduler() -> AsyncIOScheduler:
    """
    Build the scheduler and register all background jobs.
    Call once inside the FastAPI lifespan startup block.
    """
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone="UTC")

    # ── GDPR deletion worker ─────────────────────────────────────────────
    # Runs every 60 seconds; checks for any deletions whose grace period
    # has expired and executes the wipe.
    _scheduler.add_job(
        run_pending_deletions,
        trigger=IntervalTrigger(seconds=60),
        id="gdpr_deletion_worker",
        name="GDPR Deletion Worker",
        replace_existing=True,
        misfire_grace_time=30,  # allow 30s slip before skipping a run
        coalesce=True,  # merge missed runs into one
        max_instances=1,  # never run two wipes concurrently
    )

    # ── Session summary notifications (every 5 minutes) ───────────────────
    _scheduler.add_job(
        run_session_summaries,
        trigger=IntervalTrigger(minutes=5),
        id="session_summary_worker",
        name="Session Summary Notifier",
        replace_existing=True,
        misfire_grace_time=60,
        coalesce=True,
        max_instances=1,
    )

    # ── Follow-up reminder notifications (every hour) ─────────────────────
    _scheduler.add_job(
        run_followup_reminders,
        trigger=IntervalTrigger(hours=1),
        id="followup_reminder_worker",
        name="Follow-up Reminder Notifier",
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
    )

    return _scheduler


def start_scheduler() -> None:
    if _scheduler and not _scheduler.running:
        _scheduler.start()
        logger.info("APScheduler started — GDPR deletion worker active (60s interval)")


def stop_scheduler() -> None:
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")
