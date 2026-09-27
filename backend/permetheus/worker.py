"""Durable job worker: claims ``Job`` rows with SQL ``SKIP LOCKED``, executes
them under a time-boxed lease, applies bounded exponential-backoff retry, and
only ever applies a handler's results if this worker still holds the lease
(fencing via ``Job.attempts``).

Run standalone:

    python -m permetheus.worker

The main loop also polls (once per cycle, cheaply) for a configured daily
discovery schedule and enqueues at most one ``discovery.run`` job per day
when an operator has enabled it -- see ``research.maybe_enqueue_scheduled_discovery``.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from . import research
from .config import Settings
from .db import make_engine, make_sessionmaker
from .llm import LanguageModel
from .models import Job, JobState, utcnow
from .research_models import JOB_KIND_DISCOVERY, JOB_KIND_ENRICH

log = logging.getLogger("permetheus.worker")

LEASE_SECONDS = research.LEASE_SECONDS
MAX_ATTEMPTS = 5
POLL_INTERVAL_SECONDS = 2.0
SCHEDULE_POLL_EVERY = 60  # check the discovery schedule once a minute, not every poll tick
BACKOFF_BASE_SECONDS = 10
BACKOFF_CAP_SECONDS = 600

# Only research kinds. Media jobs (note.transcribe, document.extract) are run
# by the root runtime, which calls claim_job(db, kinds=...) with its own kinds.
HANDLED_KINDS = (JOB_KIND_ENRICH, JOB_KIND_DISCOVERY)


def _backoff_seconds(attempts: int) -> int:
    return min(BACKOFF_BASE_SECONDS * (2 ** max(attempts - 1, 0)), BACKOFF_CAP_SECONDS)


def attempts_used(job: Job, attempts: int | None = None) -> int:
    """Attempts spent since the last manual retry. ``Job.attempts`` itself
    never resets, so fencing tokens stay unique across retries."""
    return (job.attempts if attempts is None else attempts) - int((job.payload or {}).get("retry_base", 0))


def claim_job(db: Session, *, kinds: tuple[str, ...] = HANDLED_KINDS, now=None) -> tuple[Job, int] | None:
    """Claim one queued job of ``kinds``, or reclaim one whose lease expired
    (its owner crashed or was killed without renewing it). Returns
    ``(job, fencing)`` where ``fencing`` is the job's post-claim ``attempts``
    value -- the caller must present it back to ``_release``/``research._fence``
    before writing results, so a worker whose lease was reclaimed out from
    under it can never overwrite a newer owner's work.

    An expired lease on a job that already used ``MAX_ATTEMPTS`` is failed
    instead of reclaimed, so a job that crashes its worker every time cannot
    loop forever.

    Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` on backends that support row
    locking (Postgres, the production target), so concurrent workers each
    grab a different candidate row. SQLite (tests only) is single-writer and
    cannot compile ``SKIP LOCKED``, so it uses a plain SELECT; the
    compare-and-set UPDATE below still makes the claim safe.
    """
    now = now or utcnow()
    query = (
        select(Job)
        .where(
            Job.kind.in_(kinds),
            (
                ((Job.state == JobState.queued) & (Job.available_at <= now))
                | ((Job.state == JobState.running) & Job.lease_until.is_not(None) & (Job.lease_until < now))
            ),
        )
        .order_by(Job.available_at)
        .limit(1)
    )
    if db.get_bind().dialect.name != "sqlite":
        query = query.with_for_update(skip_locked=True)
    job = db.scalar(query)
    if job is None:
        db.rollback()
        return None

    prior_attempts, prior_state = job.attempts, job.state
    guard = (Job.id == job.id, Job.attempts == prior_attempts, Job.state == prior_state)
    if prior_state == JobState.running and attempts_used(job) >= MAX_ATTEMPTS:
        db.execute(update(Job).where(*guard).values(
            state=JobState.failed, lease_until=None, updated_at=now,
            last_error="lease_expired: worker stopped renewing on the final attempt"))
        db.commit()
        if job.kind == JOB_KIND_DISCOVERY:
            research.mark_discovery_run_abandoned(db, job)
        elif job.kind == "registry.import":
            from . import registry  # registry imports worker; resolve lazily
            registry.mark_abandoned(db, job)
        return None

    new_attempts = prior_attempts + 1
    result = db.execute(
        update(Job).where(*guard)
        .values(state=JobState.running, lease_until=now + timedelta(seconds=LEASE_SECONDS),
                attempts=new_attempts, updated_at=now)
    )
    if result.rowcount != 1:
        db.rollback()  # claimed by someone else between our SELECT and UPDATE
        return None
    db.commit()
    db.refresh(job)
    return job, new_attempts


def _release(db: Session, job_id, fencing: int, *, state: JobState, **fields) -> bool:
    """Write the job's final state iff this worker still owns the lease.
    Returns False (and writes nothing) if another attempt owns the job, or
    the handler already committed the terminal state with its results."""
    result = db.execute(
        update(Job).where(Job.id == job_id, Job.attempts == fencing, Job.state == JobState.running)
        .values(state=state, lease_until=None, updated_at=utcnow(), **fields)
    )
    db.commit()
    return result.rowcount == 1


def process_one(Session: sessionmaker, llm: LanguageModel, settings: Settings) -> bool:
    """Claim and process at most one research job. Returns True if a job was
    claimed (regardless of outcome), False if none were available to claim."""
    with Session() as db:
        claimed = claim_job(db)
    if claimed is None:
        return False
    job, fencing = claimed
    job_id = job.id

    try:
        with Session() as db:
            if job.kind == JOB_KIND_ENRICH:
                research.run_enrich(db, job=job, fencing=fencing, llm=llm, settings=settings)
            else:
                research.run_discovery(db, job=job, fencing=fencing)
    except research.JobCancelled:
        with Session() as db:
            _release(db, job_id, fencing, state=JobState.cancelled)
    except research.LeaseLost:
        log.info("job %s: no longer owned by attempt %s; results discarded", job_id, fencing)
    except Exception as exc:
        used = attempts_used(job, fencing)
        log.warning("job %s failed (attempt %s): %s", job_id, used, exc)
        with Session() as db:
            if used >= MAX_ATTEMPTS:
                if _release(db, job_id, fencing, state=JobState.failed, last_error=str(exc)[:2000]) \
                        and job.kind == JOB_KIND_DISCOVERY:
                    research.mark_discovery_run_abandoned(db, job)
            else:
                db.execute(
                    update(Job).where(Job.id == job_id, Job.attempts == fencing, Job.state == JobState.running)
                    .values(state=JobState.queued, lease_until=None,
                            available_at=utcnow() + timedelta(seconds=_backoff_seconds(used)),
                            last_error=str(exc)[:2000], updated_at=utcnow())
                )
                db.commit()
    else:
        # Handlers commit the terminal state atomically with their results;
        # this only covers a handler that returned early without doing so.
        with Session() as db:
            _release(db, job_id, fencing, state=JobState.succeeded)
    return True


def run_worker_loop(settings: Settings | None = None, *, iterations: int | None = None) -> None:
    """Poll for claimable jobs forever (or ``iterations`` times, for tests).
    Sleeps ``POLL_INTERVAL_SECONDS`` after a cycle that claimed nothing."""
    settings = settings or Settings()
    engine = make_engine(settings.database_url)
    Session = make_sessionmaker(engine)
    llm = LanguageModel(
        settings.litellm_base_url,
        settings.litellm_api_key.get_secret_value() if settings.litellm_api_key else None,
        settings.litellm_model,
    )

    count = 0
    ticks_since_schedule_check = SCHEDULE_POLL_EVERY  # check immediately on startup
    while iterations is None or count < iterations:
        if ticks_since_schedule_check >= SCHEDULE_POLL_EVERY:
            with Session() as db:
                research.maybe_enqueue_scheduled_discovery(db)
            ticks_since_schedule_check = 0

        claimed = process_one(Session, llm, settings)
        count += 1
        ticks_since_schedule_check += 1
        if not claimed:
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_worker_loop()
