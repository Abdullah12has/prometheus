import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import SQLAlchemyError

from . import auth, companies, contacts, errors, provenance, workspace, notes, deals, mail, voice, assistant, documents, research, background, registry
from .config import Settings
from .db import init_db, make_engine, make_sessionmaker
from .speech_runtime import SpeechRuntime
from .llm import LanguageModel

log = logging.getLogger("permetheus")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            init_db(engine)
            registry.init(engine)
        except SQLAlchemyError as exc:
            # Boot anyway; /api/health reports the database as unavailable.
            log.error("database bootstrap failed: %s", exc)
        tasks = background.start(app) if settings.worker_enabled else []
        app.state.background_tasks = tasks
        try:
            yield
        finally:
            await background.stop(tasks)
            await app.state.speech.close()
            if getattr(app.state, "gmail_http", None) is not None:
                app.state.gmail_http.close()
            engine.dispose()

    app = FastAPI(title="Permetheus API", version="0.1.0", lifespan=lifespan,
                  openapi_url="/api/openapi.json", docs_url="/api/docs", redoc_url=None)
    app.state.settings = settings
    app.state.sessionmaker = make_sessionmaker(engine)
    app.state.login_failures = {}
    app.state.speech = SpeechRuntime(settings.root_dir, settings.asr_binary, settings.asr_model_path or settings.data_dir / "unconfigured.gguf", settings.tts_python)
    app.state.llm = LanguageModel(settings.litellm_base_url, settings.litellm_api_key.get_secret_value() if settings.litellm_api_key else None, settings.litellm_model, settings.llm_reasoning_effort)
    app.add_middleware(CORSMiddleware, allow_origins=sorted(settings.allowed_origins), allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["Content-Type", "X-CSRF-Token"])
    errors.install(app)
    for module in (auth, workspace, companies, contacts, provenance, notes, deals, mail, voice, assistant, documents, research, registry):
        app.include_router(module.router)
    app.include_router(workspace.public)
    app.include_router(mail.oauth_router)
    return app
