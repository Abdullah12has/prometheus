import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import SQLAlchemyError

from . import auth, companies, contacts, errors, provenance, workspace
from .config import Settings
from .db import init_db, make_engine, make_sessionmaker

log = logging.getLogger("permetheus")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            init_db(engine)
        except SQLAlchemyError as exc:
            # Boot anyway; /api/health reports the database as unavailable.
            log.error("database bootstrap failed: %s", exc)
        yield
        engine.dispose()

    app = FastAPI(title="Permetheus API", version="0.1.0", lifespan=lifespan,
                  openapi_url="/api/openapi.json", docs_url="/api/docs", redoc_url=None)
    app.state.settings = settings
    app.state.sessionmaker = make_sessionmaker(engine)
    app.state.login_failures = {}
    app.add_middleware(CORSMiddleware, allow_origins=sorted(settings.allowed_origins), allow_credentials=True,
                       allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["Content-Type", "X-CSRF-Token"])
    errors.install(app)
    for module in (auth, workspace, companies, contacts, provenance):
        app.include_router(module.router)
    app.include_router(workspace.public)
    return app
