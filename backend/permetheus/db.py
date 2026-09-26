import uuid
from collections.abc import Iterator
from typing import Any, TypeVar

from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .errors import ApiError
from .models import Activity, Base

T = TypeVar("T")


def make_engine(url: str) -> Engine:
    if not url.startswith("sqlite"):
        return create_engine(url, pool_pre_ping=True)
    # In-memory SQLite (tests) must share one connection across threads.
    engine = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    return engine


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    # ponytail: create_all bootstrap, add Alembic migrations once a deployed schema must evolve.
    Base.metadata.create_all(engine)


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.sessionmaker() as db:
        yield db


def get_or_404(db: Session, model: type[T], id: uuid.UUID) -> T:
    obj = db.get(model, id)
    if obj is None:
        raise ApiError(404, "not_found", f"{model.__name__} not found")
    return obj


def record_activity(db: Session, kind: str, summary: str, company_id: uuid.UUID | None, **payload: Any) -> None:
    db.add(Activity(kind=kind, summary=summary, company_id=company_id, payload=payload))
