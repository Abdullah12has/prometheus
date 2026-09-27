import asyncio
import threading
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event, func, select

from permetheus import background, registry, research, worker
from permetheus.config import Settings
from permetheus.db import init_db, make_sessionmaker
from permetheus.models import Company, Job, JobState, utcnow
from permetheus.registry_sources import Batch, Entity


@pytest.fixture
def Session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'registry.sqlite'}")
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    init_db(engine)
    registry.init(engine)
    yield make_sessionmaker(engine)
    engine.dispose()


def _new_import(Session, *, source, checkpoint=None):
    with Session() as db:
        imp = registry.RegistryImport(source=source, country="CH", checkpoint=checkpoint or {},
                                      skip_reasons={}, errors=[])
        db.add(imp)
        db.flush()
        registry._queue_job(db, imp)
        db.commit()
        return imp.id, imp.job_id


def test_shutdown_requeues_owned_import_and_stale_thread_cannot_commit(Session, tmp_path, monkeypatch):
    asyncio.run(_test_shutdown_requeues_owned_import_and_stale_thread_cannot_commit(Session, tmp_path, monkeypatch))


async def _test_shutdown_requeues_owned_import_and_stale_thread_cannot_commit(Session, tmp_path, monkeypatch):
    registry.STOP.clear()
    import_id, job_id = _new_import(Session, source="zefix_lindas", checkpoint={"page": 4})
    other_import_id, other_job_id = _new_import(Session, source="gleif_de")
    other_lease = utcnow() + timedelta(minutes=5)
    with Session() as db:
        db.get(registry.RegistryImport, other_import_id).status = registry.ImportStatus.running
        other = db.get(Job, other_job_id)
        other.state, other.attempts, other.lease_until = JobState.running, 7, other_lease
        other.payload = {**other.payload, "retry_base": 3}
        db.commit()

    request_started, release_request, process_done = threading.Event(), threading.Event(), threading.Event()

    def pending_adapter(checkpoint, beat, errors, data_dir):
        yield Batch([Entity("CH", "business_id", "CHE-100.000.001", "Committed AG",
                            "https://example.test/committed", {})], {"page": 5}, seen=1)
        request_started.set()
        if not release_request.wait(3):
            raise TimeoutError("test source request was not released")
        yield Batch([Entity("CH", "business_id", "CHE-100.000.002", "Late AG",
                            "https://example.test/late", {})], {"page": 99}, seen=1)

    monkeypatch.setitem(registry.sources.ADAPTERS, "zefix_lindas", pending_adapter)
    settings = Settings(_env_file=None, database_url="sqlite://", root_dir=tmp_path)

    def process():
        try:
            return registry.process_one(Session, settings)
        finally:
            process_done.set()

    pending = asyncio.create_task(asyncio.to_thread(process))
    assert await asyncio.wait_for(asyncio.to_thread(request_started.wait, 2), 3)
    try:
        await background.stop([pending])

        with Session() as db:
            imp, job = db.get(registry.RegistryImport, import_id), db.get(Job, job_id)
            assert (imp.status, imp.checkpoint, imp.processed, job.state, job.payload["retry_base"]) == (
                registry.ImportStatus.queued, {"page": 5}, 1, JobState.queued, 1)
            assert worker.attempts_used(job) == 0
            external = db.get(registry.RegistryImport, other_import_id)
            external_job = db.get(Job, other_job_id)
            assert (external.status, external_job.state, external_job.attempts) == (
                registry.ImportStatus.running, JobState.running, 7)
            assert external_job.lease_until.replace(tzinfo=other_lease.tzinfo) == other_lease

            claimed = worker.claim_job(db, kinds=(registry.JOB_KIND_REGISTRY_IMPORT,))
            assert claimed is not None
            _, new_fencing = claimed
            assert new_fencing == 2
            with pytest.raises(research.LeaseLost):
                research._fence(db, job_id, 1, renew=True)

        release_request.set()
        assert await asyncio.wait_for(asyncio.to_thread(process_done.wait, 2), 3)
        with Session() as db:
            imp, job = db.get(registry.RegistryImport, import_id), db.get(Job, job_id)
            assert (imp.status, imp.checkpoint, imp.processed, job.state, job.attempts) == (
                registry.ImportStatus.queued, {"page": 5}, 1, JobState.running, 2)
            assert db.scalar(select(func.count()).select_from(Company)) == 1
    finally:
        release_request.set()
        registry.STOP.clear()


def test_shutdown_requeues_claimed_import_before_handler_marks_it_running(Session, tmp_path, monkeypatch):
    asyncio.run(_test_shutdown_requeues_claimed_import_before_handler_marks_it_running(
        Session, tmp_path, monkeypatch))


async def _test_shutdown_requeues_claimed_import_before_handler_marks_it_running(Session, tmp_path, monkeypatch):
    registry.STOP.clear()
    import_id, job_id = _new_import(Session, source="zefix_lindas")
    handler_started, release_handler, process_done = threading.Event(), threading.Event(), threading.Event()

    def pending_handler(Session, job, fencing, settings):
        handler_started.set()
        if not release_handler.wait(3):
            raise TimeoutError("test handler was not released")

    monkeypatch.setattr(registry, "run_import", pending_handler)
    settings = Settings(_env_file=None, database_url="sqlite://", root_dir=tmp_path)

    def process():
        try:
            return registry.process_one(Session, settings)
        finally:
            process_done.set()

    pending = asyncio.create_task(asyncio.to_thread(process))
    assert await asyncio.wait_for(asyncio.to_thread(handler_started.wait, 2), 3)
    try:
        with Session() as db:
            assert db.get(registry.RegistryImport, import_id).status == registry.ImportStatus.queued
            assert db.get(Job, job_id).state == JobState.running

        await background.stop([pending])
        with Session() as db:
            imp, job = db.get(registry.RegistryImport, import_id), db.get(Job, job_id)
            assert (imp.status, job.state, job.payload["retry_base"]) == (
                registry.ImportStatus.queued, JobState.queued, 1)

        release_handler.set()
        assert await asyncio.wait_for(asyncio.to_thread(process_done.wait, 2), 3)
        with Session() as db:
            imp, job = db.get(registry.RegistryImport, import_id), db.get(Job, job_id)
            assert (imp.status, job.state, job.attempts) == (
                registry.ImportStatus.queued, JobState.queued, 1)
    finally:
        release_handler.set()
        registry.STOP.clear()


@pytest.mark.parametrize("status,job_state", [
    (registry.ImportStatus.paused, JobState.cancelled),
    (registry.ImportStatus.completed, JobState.succeeded),
    (registry.ImportStatus.failed, JobState.failed),
])
def test_shutdown_requeue_preserves_nonrunning_import_state(Session, status, job_state):
    import_id, job_id = _new_import(Session, source="zefix_lindas")
    with Session() as db:
        db.get(registry.RegistryImport, import_id).status = status
        job = db.get(Job, job_id)
        job.state, job.attempts = job_state, 2
        job.lease_until = None
        db.commit()
    registry._OWNED_IMPORTS[job_id] = (2, Session, {"import_id": str(import_id), "retry_base": 1})
    try:
        registry.requeue_owned_jobs()
    finally:
        registry._OWNED_IMPORTS.pop(job_id, None)
    with Session() as db:
        imp, job = db.get(registry.RegistryImport, import_id), db.get(Job, job_id)
        assert imp.status == status and job.state == job_state and job.attempts == 2
