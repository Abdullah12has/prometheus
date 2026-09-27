from conftest import PASSWORD, make_client


def test_health_is_public_and_api_requires_login(anon):
    assert anon.get("/api/health").json() == {"status": "ok", "database": "ok"}
    r = anon.get("/api/companies")
    assert r.status_code == 401
    assert r.json() == {"error": {"code": "unauthenticated", "message": "Sign in required"}}


def test_login_me_logout(anon):
    assert anon.post("/api/auth/login", json={"password": "wrong"}).json()["error"]["code"] == "invalid_credentials"
    r = anon.post("/api/auth/login", json={"password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    csrf = r.json()["csrf_token"]
    assert anon.get("/api/auth/me").json()["csrf_token"] == csrf
    assert anon.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert anon.get("/api/auth/me").status_code == 401


def test_unsafe_requests_need_csrf_token_and_allowed_origin(client):
    token = client.headers.pop("X-CSRF-Token")
    r = client.post("/api/companies", json={"name": "Acme"})
    assert (r.status_code, r.json()["error"]["code"]) == (403, "csrf_failed")
    r = client.post("/api/companies", json={"name": "Acme"}, headers={"X-CSRF-Token": token, "Origin": "https://evil.test"})
    assert (r.status_code, r.json()["error"]["code"]) == (403, "origin_rejected")
    r = client.post("/api/companies", json={"name": "Acme"}, headers={"X-CSRF-Token": token, "Origin": "http://localhost:4310"})
    assert r.status_code == 201


def test_login_rejects_foreign_origin_and_rate_limits(anon):
    r = anon.post("/api/auth/login", json={"password": PASSWORD}, headers={"Origin": "https://evil.test"})
    assert r.status_code == 403
    for _ in range(5):
        anon.post("/api/auth/login", json={"password": "wrong"})
    r = anon.post("/api/auth/login", json={"password": PASSWORD})
    assert (r.status_code, r.json()["error"]["code"]) == (429, "rate_limited")


def test_missing_optional_config_does_not_prevent_boot():
    with make_client(admin_password=None, database_url="postgresql+psycopg://nobody@127.0.0.1:1/none") as c:
        assert c.get("/api/health").json() == {"status": "degraded", "database": "unavailable"}
        r = c.post("/api/auth/login", json={"password": "x"})
        assert (r.status_code, r.json()["error"]["code"]) == (503, "auth_not_configured")


def test_settings_status_reports_presence_not_values():
    with make_client(google_client_id="id-value-123", google_client_secret="secret-value-456") as c:
        c.headers["X-CSRF-Token"] = c.post("/api/auth/login", json={"password": PASSWORD}).json()["csrf_token"]
        r = c.get("/api/settings/status")
        body = r.json()
        assert "secret-value-456" not in r.text and "id-value-123" not in r.text and PASSWORD not in r.text
        connectors = {x["id"]: x for x in body["connectors"]}
        assert connectors["gmail"]["configured"] and connectors["gmail"]["implemented"]
        assert connectors["phone"]["missing"] == ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "PUBLIC_BASE_URL"]
        assert body["outbound_dispatch"] == {"email": "disconnected", "phone": "disabled"}
