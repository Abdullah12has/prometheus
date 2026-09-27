from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from permetheus.models import Base


def test_schema_compiles_for_postgresql():
    ddl = "\n".join(str(CreateTable(t).compile(dialect=postgresql.dialect())) for t in Base.metadata.sorted_tables)
    assert "UUID" in ddl and "JSONB" in ddl and "NUMERIC(24, 4)" in ddl and "TIMESTAMP WITH TIME ZONE" in ddl


def test_openapi_lists_contract_routes(anon):
    paths = anon.get("/api/openapi.json").json()["paths"]
    for route in ("/api/health", "/api/auth/login", "/api/auth/me", "/api/auth/logout", "/api/companies",
                  "/api/companies/{company_id}", "/api/settings/status", "/api/dashboard"):
        assert route in paths
