"""Local durable jobs; the API and recordings share one speech runtime."""
import asyncio
import contextlib
import logging
import uuid
from datetime import timedelta

from sqlalchemy import select, update
from starlette.requests import Request

from . import deals, documents, mail, notes, research, worker
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


def scheduled_cycle(app):
    deals.refresh_matches(app.state.sessionmaker)
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
    return [asyncio.create_task(loop(app)) for loop in (research_loop, media_loop, scheduled_loop)]


async def stop(tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
