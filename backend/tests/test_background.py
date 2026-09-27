import asyncio
import uuid

from permetheus.models import Job, JobState


def test_media_runner_completes_and_failure_does_not_poison_queue(client, monkeypatch):
    from permetheus import background
    app = client.app
    sm = app.state.sessionmaker
    with sm() as db:
        job = Job(kind='document.extract', payload={'document_id': str(uuid.uuid4())}, idempotency_key='background-document-test')
        db.add(job)
        db.commit()
        job_id = job.id
    async def successful(*args, job_guard):
        assert job_guard()
    monkeypatch.setattr(background.documents, 'process_document', successful)
    assert asyncio.run(background.process_media(app))
    with sm() as db:
        job = db.get(Job, job_id)
        assert job.state == JobState.succeeded and job.lease_until is None
        job.state = JobState.queued
        db.commit()
    async def failing(*args, job_guard):
        raise ValueError('invalid stored document')
    monkeypatch.setattr(background.documents, 'process_document', failing)
    assert asyncio.run(background.process_media(app))
    with sm() as db:
        job = db.get(Job, job_id)
        assert job.state == JobState.failed and job.last_error
    assert not asyncio.run(background.process_media(app))
