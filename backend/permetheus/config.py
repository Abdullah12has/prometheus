from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]
ROOT_ENV = ROOT_DIR / ".env"


class Settings(BaseSettings):
    """Every field is optional so a partial .env never prevents boot."""

    model_config = SettingsConfigDict(env_file=ROOT_ENV, extra="ignore", env_ignore_empty=True)

    worker_enabled: bool = False
    database_url: str = "postgresql+psycopg://permetheus@localhost:5433/permetheus"
    admin_password: SecretStr | None = None
    session_secret: SecretStr | None = None
    root_dir: Path = ROOT_DIR
    asr_binary: Path = ROOT_DIR / "data/bin/native-asr-bridge"
    asr_model_path: Path | None = None
    tts_python: Path = ROOT_DIR / ".venv-speech/bin/python"
    google_redirect_uri: str = "http://localhost:4311/oauth/google/callback"
    web_origins: str = "http://localhost:4310,http://127.0.0.1:4310"

    # Connector presence only; values are never returned by the API.
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    litellm_base_url: str | None = Field(None, validation_alias=AliasChoices("LLM_BASE_URL", "LITELLM_BASE_URL", "litellm_base_url"))
    litellm_api_key: SecretStr | None = Field(None, validation_alias=AliasChoices("LLM_API_KEY", "LITELLM_API_KEY", "litellm_api_key"))
    llm_reasoning_effort: str | None = None
    litellm_model: str | None = Field(None, validation_alias=AliasChoices("LLM_MODEL", "LITELLM_MODEL", "litellm_model"))
    searxng_url: str | None = "http://localhost:8888"
    twilio_account_sid: str | None = None
    twilio_auth_token: SecretStr | None = None
    twilio_from_number: str | None = None
    public_base_url: str | None = None

    @property
    def allowed_origins(self) -> set[str]:
        return {o.strip().rstrip("/") for o in self.web_origins.split(",") if o.strip()}

    @property
    def cookie_secure(self) -> bool:
        return any(o.startswith("https://") for o in self.allowed_origins)

    @property
    def data_dir(self) -> Path:
        return self.root_dir / "data"
