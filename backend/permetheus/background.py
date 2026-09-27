"""Local durable jobs; the API and recordings share one speech runtime."""
import asyncio
import contextlib
import logging
import uuid
from datetime import timedelta

from sqlalchemy import select, update
from starlette.requests import Request

from . import deals, documents, mail, notes, registry, research, worker
from .models import Job, JobState, utcnow

log = logging.getLogger('permetheus.background')
MEDIA_KINDS = ('note.transcribe', 'document.extract')


def owns_job(sessionmaker, job_id, attempt):
    with sessionmaker() as db:
        return db.scalar(select(Job.id).where(Job.id == job_id, Job.attempts == attempt,
            Job.state == JobState.running, Job.lease_until > utcnow())) is not None


async def renew_lease(sessionmaker, job_id, attempt):
    while True:
        await asyncio.sleep(30)
        with sessionmaker() as db:
            changed = db.execute(update(Job).where(Job.id == job_id, Job.attempts == attempt,
                Job.state == JobState.running).values(lease_until=utcnow() + timedelta(seconds=worker.LEASE_SECONDS))).rowcount
            db.commit()
        if not changed:
            return


async def process_media(app):
    sm = app.state.sessionmaker
    with sm() as db:
        claimed = worker.claim_job(db, kinds=MEDIA_KINDS)
    if not claimed:
        return False
    job, attempt = claimed
    renewal = asyncio.create_task(renew_lease(sm, job.id, attempt))
    guard = lambda: owns_job(sm, job.id, attempt)
    try:
        if job.kind == 'note.transcribe':
            await notes.process_note(uuid.UUID(job.payload['note_id']), sm, app.state.speech, app.state.llm, job_guard=guard)
        else:
            await documents.process_document(uuid.UUID(job.payload['document_id']), sm, app.state.llm,
                app.state.settings.data_dir, job_guard=guard)
        with sm() as db:
            worker._release(db, job.id, attempt, state=JobState.succeeded)
    except asyncio.CancelledError:
        with sm() as db:
            worker._release(db, job.id, attempt, state=JobState.queued, available_at=utcnow())
        raise
    except Exception:
        log.exception('Local media job failed: %s', job.id)
        with sm() as db:
            worker._release(db, job.id, attempt, state=JobState.failed,
                last_error='Local processing failed. Review the recording or document and retry.')
    finally:
        renewal.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await renewal
    return True


async def media_loop(app):
    while True:
        try:
            if await process_media(app):
                continue
        except Exception:
            log.exception('Media queue unavailable')
        await asyncio.sleep(2)


async def research_loop(app):
    while True:
        try:
            claimed = await asyncio.to_thread(worker.process_one, app.state.sessionmaker, app.state.llm, app.state.settings)
            if claimed:
                continue
        except Exception:
            log.exception('Research queue unavailable')
        await asyncio.sleep(2)


async def registry_loop(app):
    # Own loop: an hours-long bulk import must not starve enrichment or media jobs.
    while True:
        try:
            if await asyncio.to_thread(registry.process_one, app.state.sessionmaker, app.state.settings):
                continue
        except Exception:
            log.exception('Registry import queue unavailable')
        await asyncio.sleep(5)


async def enrichment_feeder_loop(app):
    while True:
        try:
            await asyncio.to_thread(registry.feed_enrichment, app.state.sessionmaker)
        except Exception:
            log.exception('Enrichment feeder failed')
        await asyncio.sleep(15)


def scheduled_cycle(app):
    _, app.state.match_cursor = deals.refresh_match_batch(
        app.state.sessionmaker, getattr(app.state, 'match_cursor', None))
    with app.state.sessionmaker() as db:
        research.maybe_enqueue_scheduled_discovery(db)
    with app.state.sessionmaker() as db:
        account = db.scalar(select(mail.GmailAccount))
        if account is None or account.needs_reauth:
            return
    request = Request({'type': 'http', 'app': app})
    # Sync first: a received reply must stop follow-ups before due steps run.
    with app.state.sessionmaker() as db:
        mail.sync(request, db)
    with app.state.sessionmaker() as db:
        mail.reconcile(request, db)
    with app.state.sessionmaker() as db:
        mail.run_due(request, limit=50, db=db)


async def scheduled_loop(app):
    while True:
        try:
            await asyncio.to_thread(scheduled_cycle, app)
        except Exception:
            log.exception('Scheduled connector cycle failed; no subsequent sends attempted')
        await asyncio.sleep(60)


def start(app):
    registry.STOP.clear()
    # Two researchers share the local search/model budget; one import lane per country.
    loops = (research_loop, research_loop, media_loop, scheduled_loop,
             registry_loop, registry_loop, registry_loop, enrichment_feeder_loop)
    return [asyncio.create_task(loop(app)) for loop in loops]


async def stop(tasks):
    if tasks:
        registry.STOP.set()
        registry.requeue_owned_jobs()  # fenced even when a cancelled to_thread call is still in I/O
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
