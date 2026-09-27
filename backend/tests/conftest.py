import pytest
from fastapi.testclient import TestClient

from permetheus.app import create_app
from permetheus.config import Settings

PASSWORD = "correct horse"


def make_client(**overrides) -> TestClient:
    # _env_file=None: tests never read the real root .env.
    settings = Settings(_env_file=None, **{"database_url": "sqlite://", "admin_password": PASSWORD, **overrides})
    return TestClient(create_app(settings))


@pytest.fixture
def anon():
    with make_client() as c:
        yield c


@pytest.fixture
def client(anon):
    r = anon.post("/api/auth/login", json={"password": PASSWORD})
    assert r.status_code == 200
    anon.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return anon
