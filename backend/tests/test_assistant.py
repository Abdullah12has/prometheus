import json
from contextlib import contextmanager

from fastapi.testclient import TestClient

from conftest import make_client
from permetheus import assistant
from permetheus.llm import ModelUnavailable


class FakeLLM:
    configured = True

    def __init__(self, calls):
        self.calls = list(calls)
        self.seen = []

    async def complete(self, messages, *, tools=None, **options):
        self.seen.append(messages)
        answer = self.calls.pop(0) if self.calls else {"content": "Okay."}
        if isinstance(answer, Exception):
            raise answer
        return answer


@contextmanager
def run(calls):
    client = make_client()
    client.app.state.llm = FakeLLM(calls)
    with client:
        login = client.post("/api/auth/login", json={"password": "correct horse"})
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        yield client


def tool(name, args):
    return {"tool_calls": [{"id": "call-1", "type": "function", "function": {"name": name, "arguments": args}}]}


def test_company_creation_uses_domain_api_and_persists_history():
    with run([tool("create_company_api_companies_post", '{"body":{"name":"Assistant Co"}}'), {"content": "Created Assistant Co."}]) as client:
        response = client.post("/api/assistant", json={"message": "Add Assistant Co"})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["actions"][0]["status"] == 201
        assert result["actions"][0]["result"]["company"]["name"] == "Assistant Co"
        assert client.get("/api/companies", params={"q": "Assistant Co"}).json()["total"] == 1
        history = client.get(f"/api/assistant/conversations/{result['conversation_id']}").json()
        assert [m["role"] for m in history["messages"]] == ["user", "assistant"]
        assert history["messages"][1]["actions"][0]["status"] == 201


def test_forbidden_delete_tool_is_rejected():
    with run([tool("delete_company", '{"path_params":{"company_id":"x"}}'), {"content": "No deletion was performed."}]) as client:
        company = client.post("/api/companies", json={"name": "Keep"}).json()["company"]
        response = client.post("/api/assistant", json={"message": "Delete it", "conversation_id": None})
        assert response.status_code == 200
        assert response.json()["actions"][0]["kind"] == "error"
        assert response.json()["actions"][0]["status"] == 400
        assert client.get(f"/api/companies/{company['id']}").status_code == 200


def test_browser_actions_open_the_requested_voice_workspace():
    for kind, target in [("open_recorder", "/voice-notes?tab=notes"), ("open_voice", "/voice-notes")]:
        with run([tool("browser_action", json.dumps({"kind": kind})), {"content": "Open the workspace."}]) as client:
            response = client.post("/api/assistant", json={"message": kind})
            assert response.status_code == 200
            assert response.json()["actions"][0]["link"] == target


def test_invalid_domain_body_is_rejected_by_api_schema():
    with run([tool("create_company_api_companies_post", '{"body":{}}'), {"content": "The company data was invalid."}]) as client:
        response = client.post("/api/assistant", json={"message": "Create a company"})
        assert response.status_code == 200
        assert response.json()["actions"][0]["status"] == 422
        assert client.get("/api/companies").json()["total"] == 0


def test_path_parameters_cannot_escape_allowlisted_route():
    with run([tool("get_company_api_companies__company_id__get", '{"path_params":{"company_id":"../../jobs"}}'), {"content": "Invalid company ID."}]) as client:
        response = client.post("/api/assistant", json={"message": "Look up this company"})
        assert response.status_code == 200
        action = response.json()["actions"][0]
        assert action["status"] == 400
        assert action["link"] == "/companies"
        assert client.get("/api/jobs").status_code == 200


def test_tool_schemas_have_valid_outer_required_names_and_no_strict_mode():
    with run([]) as client:
        tools, routes = assistant._tools(client.app)
        for tool_spec in tools:
            fn = tool_spec["function"]
            schema = fn["parameters"]
            assert set(schema.get("required", [])) <= set(schema["properties"])
            assert "strict" not in fn
        get_company = next(name for name, route in routes.items() if route[:2] == ("GET", "/api/companies/{company_id}"))
        schema = next(t["function"]["parameters"] for t in tools if t["function"]["name"] == get_company)
        assert schema["required"] == ["path_params"]
        assert schema["properties"]["path_params"]["required"] == ["company_id"]


def test_history_keeps_latest_40_and_survives_logout():
    with run([]) as client:
        first = client.post("/api/assistant", json={"message": "Message 0"}).json()
        cid = first["conversation_id"]
        for index in range(1, 22):
            response = client.post("/api/assistant", json={"message": f"Message {index}", "conversation_id": cid})
            assert response.status_code == 200
        assert len(client.app.state.llm.seen[-1]) == 41
        assert all(message["content"] != "Message 0" for message in client.app.state.llm.seen[-1])
        client.post("/api/auth/logout")
        login = client.post("/api/auth/login", json={"password": "correct horse"})
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        assert client.get(f"/api/assistant/conversations/{cid}").status_code == 200
        assert len(client.get("/api/assistant/conversations").json()) == 1


def test_eight_tool_call_turn_budget():
    calls = [{"id": str(i), "type": "function", "function": {"name": "not_allowed", "arguments": "{}"}} for i in range(10)]
    with run([{"tool_calls": calls}]) as client:
        response = client.post("/api/assistant", json={"message": "Do many things"})
        assert response.status_code == 200
        assert len(response.json()["actions"]) == 8
        assert "action limit" in response.json()["reply"]


def test_assistant_cannot_set_company_or_mandate_verification():
    assert assistant._guard_write("/api/companies", "POST", {"name": "X", "business_id": "123"})
    assert assistant._guard_write("/api/companies/{company_id}", "PATCH", {"status": "confirmed"})
    assert assistant._guard_write("/api/mandates", "POST", {"evidence_level": "buyer_confirmed"})
    assert assistant._guard_write("/api/mandates", "POST", {"evidence_level": "public_strategy", "identity_verified": True})
    assert assistant._guard_write("/api/mandates/{mandate_id}", "PATCH", {"financing_status": "evidenced"})


def test_mail_and_deal_allowlist_excludes_dispatch_and_approval_routes():
    expected = {
        ("POST", "/api/outreach/drafts"), ("GET", "/api/outreach/drafts"),
        ("PATCH", "/api/outreach/drafts/{draft_id}"),
        ("GET", "/api/outreach/conversations/{conversation_id}"),
        ("POST", "/api/outreach/conversations/{conversation_id}/followup"),
        ("GET", "/api/outreach/controls"),
        ("GET", "/api/outreach/sequences"), ("POST", "/api/outreach/sequences"),
        ("PATCH", "/api/outreach/sequences/{sequence_id}"),
        ("POST", "/api/outreach/sequences/{sequence_id}/pause"),
        ("GET", "/api/outreach/enrollments"),
        ("POST", "/api/outreach/enrollments/{enrollment_id}/{action}"),
        ("GET", "/api/historical-deals"), ("POST", "/api/historical-deals"),
        ("GET", "/api/deals/analytics"), ("POST", "/api/simulations/replay"),
    }
    assert expected <= assistant.ALLOWED
    forbidden = {
        ("POST", "/api/outreach/drafts/{draft_id}/approve"),
        ("POST", "/api/outreach/drafts/{draft_id}/send"),
        ("POST", "/api/outreach/conversations/{conversation_id}/classify"),
        ("POST", "/api/outreach/sequences/{sequence_id}/resume"),
        ("POST", "/api/outreach/sequences/{sequence_id}/template-approvals"),
        ("POST", "/api/outreach/enrollments/{enrollment_id}/resume"),
        ("POST", "/api/preferences/{profile_id}/confirm"),
        ("POST", "/api/opportunities/{opportunity_id}/drafts"),
    }
    assert not forbidden & assistant.ALLOWED


def test_mail_guards_require_per_draft_review_and_stop_only():
    assert assistant._guard_write("/api/outreach/drafts", "POST", {"disclosure": {"buyer": "Acquirer"}})
    assert assistant._guard_write("/api/outreach/drafts/{draft_id}", "PATCH", {"disclosure": {"ai": True}})
    assert assistant._guard_write("/api/outreach/sequences", "POST", {"steps": [{"approval": "template"}]})
    assert assistant._guard_write("/api/outreach/sequences", "POST", {"steps": [{"kind": "ask_interest"}]}) is None
    assert assistant._guard_write("/api/outreach/enrollments/{enrollment_id}/{action}", "POST", None, {"action": "resume"})
    assert assistant._guard_write("/api/outreach/enrollments/{enrollment_id}/{action}", "POST", None, {"action": "stop"}) is None


def test_malformed_path_and_query_objects_are_rejected_without_server_error():
    with run([tool("get_company_api_companies__company_id__get", '{"path_params":"oops"}'), {"content": "Invalid arguments."}]) as client:
        response = client.post("/api/assistant", json={"message": "Look up a company"})
        assert response.status_code == 200
        assert response.json()["actions"][0]["status"] == 400


def test_provider_failure_returns_persisted_conversation_and_completed_actions():
    with run([tool("create_company_api_companies_post", '{"body":{"name":"Saved before error"}}'), ModelUnavailable("offline")]) as client:
        response = client.post("/api/assistant", json={"message": "Create a company"})
        assert response.status_code == 503
        error = response.json()["error"]["details"]
        assert error["conversation_id"]
        assert error["actions"][0]["status"] == 201
        history = client.get(f"/api/assistant/conversations/{error['conversation_id']}").json()
        assert history["messages"][-1]["actions"][0]["status"] == 201


def test_deep_schemas_keep_scalar_enum_and_required_values():
    from permetheus.app import create_app
    from permetheus.assistant import _tools
    from permetheus.config import Settings
    tools, _ = _tools(create_app(Settings(_env_file=None, database_url="sqlite://")))
    def check(value):
        if isinstance(value, dict):
            if "enum" in value:
                assert all(not isinstance(x, (dict, list)) for x in value["enum"])
            if "required" in value:
                assert all(isinstance(x, str) for x in value["required"])
            for child in value.values(): check(child)
        elif isinstance(value, list):
            for child in value: check(child)
    check(tools)


def test_company_lookup_by_name_then_action_and_followup_context():
    with run([]) as client:
        company = client.post("/api/companies", json={"name": "North Star Oy"}).json()["company"]
        client.app.state.llm.calls = [
            tool("list_companies_api_companies_get", json.dumps({"query": {"q": "North Star"}})),
            tool("get_company_api_companies__company_id__get", json.dumps({"path_params": {"company_id": company["id"]}})),
            {"content": "Found North Star Oy."},
        ]
        result = client.post("/api/assistant", json={"message": "Tell me about North Star"}).json()
        assert result["actions"][0]["result"]["items"][0]["id"] == company["id"]
        client.post("/api/assistant", json={"message": "Research that company", "conversation_id": result["conversation_id"]})
        assert company["id"] in json.dumps(client.app.state.llm.seen[-1])


def test_large_results_preserve_records_instead_of_discarding_everything():
    with run([tool("list_companies_api_companies_get", "{}"), {"content": "Companies found."}]) as client:
        for i in range(20):
            client.post("/api/companies", json={"name": f"Company {i}", "description": "Evidence " * 200})
        result = client.post("/api/assistant", json={"message": "List companies"}).json()
        assert result["actions"][0]["result"].get("items")


def test_grounded_search_returns_clickable_sources_and_handles_outage(monkeypatch):
    from types import SimpleNamespace
    from permetheus import acquisition
    monkeypatch.setattr(acquisition, "search_web", lambda *a, **k: SimpleNamespace(
        results=[SimpleNamespace(title="Official report", url="https://example.org/report", content="Revenue was 10 million.")],
        degraded=False, fetched_at="2026-09-27"))
    with run([tool("search_sources_api_assistant_search_get", json.dumps({"query": {"q": "market report"}})), {"content": "Revenue was 10 million [1]."}]) as client:
        result = client.post("/api/assistant", json={"message": "Find the market report"}).json()
        assert result["actions"][0]["status"] == 200
        assert result["actions"][0]["result"]["sources"][0]["url"] == "https://example.org/report"
        client.app.state.settings.searxng_url = None
        assert client.get("/api/assistant/search", params={"q": "market"}).status_code == 503
