from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event

from permetheus import research, worker
from permetheus.config import Settings
from permetheus.db import init_db, make_sessionmaker
from permetheus.models import Job, JobState
from permetheus.research_models import JOB_KIND_DISCOVERY, DiscoveryRun, DiscoveryRunStatus


@pytest.fixture
def Session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    init_db(engine)
    yield make_sessionmaker(engine)
    engine.dispose()


def add_discovery_job(Session):
    with Session() as db:
        run = DiscoveryRun(params={"max_pages": 1})
        db.add(run)
        db.flush()
        job = Job(kind=JOB_KIND_DISCOVERY, idempotency_key=f"d:{run.id}", payload={"discovery_run_id": str(run.id)})
        db.add(job)
        db.commit()
        return job.id, run.id


def make_due(Session, job_id):
    with Session() as db:
        db.get(Job, job_id).available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()


def test_interactive_job_precedes_older_bulk_campaign(Session):
    with Session() as db:
        bulk = Job(kind='company.enrich', idempotency_key='bulk', payload={'campaign_id': 'campaign'},
                   available_at=datetime.now(timezone.utc) - timedelta(hours=1))
        manual = Job(kind='company.enrich', idempotency_key='manual', payload={})
        db.add_all([bulk, manual]); db.commit()
        claimed, _ = worker.claim_job(db)
        assert claimed.id == manual.id


def test_claim_job_defaults_to_research_kinds_and_accepts_explicit_kinds(Session):
    with Session() as db:
        db.add(Job(kind="note.transcribe", idempotency_key="note.transcribe:n1", payload={}))
        db.commit()
        assert worker.claim_job(db) is None
        job, fencing = worker.claim_job(db, kinds=("note.transcribe",))
        assert (job.kind, fencing, job.state) == ("note.transcribe", 1, JobState.running)


def test_failures_back_off_then_fail_after_max_attempts(Session, monkeypatch):
    job_id, run_id = add_discovery_job(Session)

    def boom(db, *, job, fencing):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(research, "run_discovery", boom)
    settings = Settings(_env_file=None, database_url="sqlite://")
    for attempt in range(1, worker.MAX_ATTEMPTS + 1):
        before = datetime.now(timezone.utc)
        assert worker.process_one(Session, llm=None, settings=settings)
        with Session() as db:
            job = db.get(Job, job_id)
            assert job.attempts == attempt and job.last_error == "upstream down"
            if attempt < worker.MAX_ATTEMPTS:
                assert job.state == JobState.queued
                delay = (job.available_at.replace(tzinfo=timezone.utc) - before).total_seconds()
                assert worker._backoff_seconds(attempt) - 1 <= delay <= worker._backoff_seconds(attempt) + 1
                assert not worker.process_one(Session, llm=None, settings=settings)  # not due yet
        make_due(Session, job_id)
    with Session() as db:
        assert db.get(Job, job_id).state == JobState.failed
        assert db.get(DiscoveryRun, run_id).status == DiscoveryRunStatus.failed


def test_expired_lease_on_final_attempt_fails_instead_of_looping(Session):
    job_id, run_id = add_discovery_job(Session)
    with Session() as db:
        job = db.get(Job, job_id)
        job.state, job.attempts = JobState.running, worker.MAX_ATTEMPTS
        job.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        assert worker.claim_job(db) is None
    with Session() as db:
        assert db.get(Job, job_id).state == JobState.failed
        assert db.get(DiscoveryRun, run_id).status == DiscoveryRunStatus.failed


def test_manual_retry_refreshes_budget_but_keeps_fencing_tokens_unique(Session):
    job_id, _ = add_discovery_job(Session)
    with Session() as db:
        stale_job, stale_fencing = worker.claim_job(db)
        db.get(Job, job_id).state = JobState.failed
        db.get(Job, job_id).attempts = worker.MAX_ATTEMPTS
        db.commit()
        research.retry_job(job_id, db)
        job, fencing = worker.claim_job(db)
        assert fencing == worker.MAX_ATTEMPTS + 1 and worker.attempts_used(job) == 1
        # the pre-retry attempt can never reuse a token the new owner holds
        with pytest.raises(research.LeaseLost):
            research._fence(db, job_id, stale_fencing, finish=JobState.succeeded)
        assert not worker._release(db, job_id, stale_fencing, state=JobState.succeeded)
        assert db.get(Job, job_id, populate_existing=True).state == JobState.running
