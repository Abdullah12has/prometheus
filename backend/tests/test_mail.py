"""Gmail/outreach tests. Google is replaced by an httpx.MockTransport; nothing leaves the process."""

import base64
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, timedelta
from email import message_from_bytes, policy
from email.message import EmailMessage
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from permetheus import mail
from permetheus.app import create_app
from permetheus.config import Settings
from permetheus.models import (
    Base, Company, CompanyStatus, Contact, ContactRole, FinancialMetric, FinancialObservation, FinancialScope,
    FinancialStatus, IntentStatement, PersonRole, ReviewStatus, SellerIntent, Source, SourceKind, Verification, utcnow,
)

PASSWORD = "correct horse"
GMAIL = "/gmail/v1/users/me"


class MailSettings(Settings):
    # Fields the root settings gain when this module is registered.
    google_redirect_uri: str | None = None
    session_secret: SecretStr | None = None


class FakeGoogle:
    """Answers from a route table keyed by (method, path) and records every request."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.threads: dict[str, list[str]] = {}  # threadId -> message ids Gmail holds, as threads.get reports them
        self.routes = {
            ("POST", "/token"): lambda r: httpx.Response(200, json={
                "access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600,
                "scope": " ".join(mail.SCOPES)}),
            ("GET", f"{GMAIL}/profile"): lambda r: httpx.Response(200, json={"emailAddress": "Me@Example.com", "historyId": "100"}),
            ("POST", "/revoke"): lambda r: httpx.Response(200),
            ("POST", f"{GMAIL}/messages/send"): lambda r: self.accept("g1", "t1"),
        }

    def accept(self, mid: str, tid: str) -> httpx.Response:
        self.add(tid, mid)
        return httpx.Response(200, json={"id": mid, "threadId": tid})

    def add(self, tid: str, mid: str) -> None:
        if mid not in self.threads.setdefault(tid, []):
            self.threads[tid].append(mid)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        handler = self.routes.get((request.method, request.url.path))
        tid = request.url.path.removeprefix(f"{GMAIL}/threads/")
        if handler is None and request.method == "GET" and tid in self.threads:
            return httpx.Response(200, json={"id": tid, "messages": [{"id": m} for m in self.threads[tid]]})
        return handler(request) if handler else httpx.Response(404, json={"error": {"status": "NOT_FOUND"}})

    def called(self, path: str) -> list[httpx.Request]:
        return [c for c in self.calls if c.url.path == path]

    def sent_mime(self, i: int = -1) -> EmailMessage:
        raw = json.loads(self.called(f"{GMAIL}/messages/send")[i].content)["raw"]
        return message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)


@contextmanager
def make_env(database_url: str = "sqlite://"):
    settings = MailSettings(
        _env_file=None, database_url=database_url, admin_password=PASSWORD, google_client_id="client-id",
        google_client_secret="client-secret", google_redirect_uri="http://localhost:4311/oauth/google/callback",
        session_secret="s" * 40,
    )
    app = create_app(settings)
    app.include_router(mail.router)
    app.include_router(mail.oauth_router)
    google = FakeGoogle()
    app.state.gmail_http = httpx.Client(transport=httpx.MockTransport(google))
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"password": PASSWORD})
        c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        yield SimpleNamespace(c=c, app=app, google=google, db=app.state.sessionmaker)


@pytest.fixture
def env():
    with make_env() as e:
        yield e


def start_oauth(env) -> str:
    r = env.c.post("/api/gmail/connect")
    assert r.status_code == 200
    return parse_qs(urlsplit(r.json()["authorization_url"]).query)["state"][0]


def connect(env):
    state = start_oauth(env)
    r = env.c.get("/oauth/google/callback", params={"code": "abc", "state": state}, follow_redirects=False)
    assert r.headers["location"] == "http://localhost:4310/outreach?gmail=connected"


def seed(env, status=CompanyStatus.confirmed, verification=Verification.verified):
    with env.db() as db:
        co = Company(name="Acme Oy", name_normalized="acme", status=status)
        db.add(co)
        db.flush()
        ct = Contact(company_id=co.id, name="Aino Owner", person_role=PersonRole.owner, contact_role=ContactRole.seller,
                     email="aino@acme.fi", verification=verification)
        db.add(ct)
        db.commit()
        return str(co.id), str(ct.id)


def draft(env, co, ct, **extra):
    body = {"company_id": co, "contact_id": ct, "recipients": ["Aino@Acme.fi"], "subject": "Succession at Acme",
            "body": "Hello Aino, would you consider a conversation?", "disclosure": {"buyer_identity_disclosed": False}, **extra}
    r = env.c.post("/api/outreach/drafts", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def approve(env, d, **extra):
    return env.c.post(f"/api/outreach/drafts/{d['id']}/approve",
                      json={"version": d["version"], "content_hash": d["content_hash"], **extra})


def approved(env, co, ct, **extra):
    r = approve(env, draft(env, co, ct, **extra))
    assert r.status_code == 200, r.text
    return r.json()


def send(env, d):
    return env.c.post(f"/api/outreach/drafts/{d['id']}/send")


def reply_raw(text: str, sender="aino@acme.fi", msgid="<r1@acme.fi>") -> dict:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"], msg["Message-ID"] = sender, "me@example.com", "Re: Succession at Acme", msgid
    msg.set_content(text)
    later = int((utcnow() + timedelta(minutes=5)).timestamp() * 1000)  # replies arrive after our send
    return {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("="), "internalDate": str(later)}


def serve_message(env, mid, text, tid="t1", **kw):
    env.google.add(tid, mid)  # the reply exists in Gmail's thread whether or not it has been synced
    env.google.routes[("GET", f"{GMAIL}/messages/{mid}")] = lambda r: httpx.Response(200, json={"id": mid, "threadId": tid, **reply_raw(text, **kw)})


def serve_history(env, *pairs, history_id="120"):
    env.google.routes[("GET", f"{GMAIL}/history")] = lambda r: httpx.Response(200, json={
        "history": [{"messagesAdded": [{"message": {"id": m, "threadId": t}} for m, t in pairs]}], "historyId": history_id})


# ---------------------------------------------------------------- OAuth

def test_connect_url_requests_offline_send_and_readonly_with_pkce(env):
    r = env.c.post("/api/gmail/connect")
    q = parse_qs(urlsplit(r.json()["authorization_url"]).query)
    assert q["scope"] == [" ".join(mail.SCOPES)] and q["access_type"] == ["offline"]
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"] == ["http://localhost:4311/oauth/google/callback"]
    assert "httponly" in r.headers["set-cookie"].lower() and "path=/oauth/google" in r.headers["set-cookie"].lower()


def test_oauth_state_is_cookie_bound_one_time_and_expiring(env):
    state = start_oauth(env)
    bad = env.c.get("/oauth/google/callback", params={"code": "x", "state": "forged"}, follow_redirects=False)
    assert bad.headers["location"].endswith("reason=invalid_state")

    binding = env.c.cookies.get(mail.OAUTH_COOKIE)
    env.c.cookies.delete(mail.OAUTH_COOKIE)
    r = env.c.get("/oauth/google/callback", params={"code": "x", "state": state}, follow_redirects=False)
    assert r.headers["location"].endswith("reason=invalid_state")  # other browser: no binding cookie

    env.c.cookies.set(mail.OAUTH_COOKIE, binding, path="/oauth/google")
    ok = env.c.get("/oauth/google/callback", params={"code": "x", "state": state}, follow_redirects=False)
    assert ok.headers["location"].endswith("gmail=connected")
    token_call = env.google.called("/token")[0]
    assert b"code_verifier=" in token_call.content

    env.c.cookies.set(mail.OAUTH_COOKIE, binding, path="/oauth/google")
    replay = env.c.get("/oauth/google/callback", params={"code": "x", "state": state}, follow_redirects=False)
    assert replay.headers["location"].endswith("reason=invalid_state")

    state2 = start_oauth(env)
    with env.db() as db:
        for row in db.scalars(select(mail.OAuthState).where(mail.OAuthState.used_at.is_(None))):
            row.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    expired = env.c.get("/oauth/google/callback", params={"code": "x", "state": state2}, follow_redirects=False)
    assert expired.headers["location"].endswith("reason=invalid_state")


def test_tokens_encrypted_status_hides_secrets_and_disconnect_revokes(env):
    connect(env)
    with env.db() as db:
        account = db.scalar(select(mail.GmailAccount))
        assert account.email == "me@example.com" and account.history_id == "100"
        assert "refresh-1" not in account.token_ciphertext and "access-1" not in account.token_ciphertext
    status = env.c.get("/api/gmail/status")
    assert status.json()["connected"] and "refresh-1" not in status.text and "access-1" not in status.text

    r = env.c.post("/api/gmail/disconnect")
    assert r.json() == {"connected": False, "revoked": True}
    assert b"token=refresh-1" in env.google.called("/revoke")[0].content
    assert env.c.get("/api/gmail/status").json()["connected"] is False


def test_refresh_rejected_marks_reauth(env):
    env.google.routes[("POST", "/token")] = lambda r: httpx.Response(200, json={
        "access_token": "a", "refresh_token": "r", "expires_in": 0, "scope": " ".join(mail.SCOPES)})
    connect(env)
    env.google.routes[("POST", "/token")] = lambda r: httpx.Response(400, json={"error": "invalid_grant"})
    assert env.c.post("/api/gmail/sync").json()["error"]["code"] == "gmail_reauth_required"
    assert env.c.get("/api/gmail/status").json()["needs_reauth"] is True


def test_callback_rejects_missing_scope(env):
    env.google.routes[("POST", "/token")] = lambda r: httpx.Response(200, json={
        "access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": mail.SCOPES[0]})
    state = start_oauth(env)
    r = env.c.get("/oauth/google/callback", params={"code": "x", "state": state}, follow_redirects=False)
    assert r.headers["location"].endswith("reason=scope_missing")
    assert env.c.get("/api/gmail/status").json()["connected"] is False


# ---------------------------------------------------------------- approval and send gates

def test_send_uses_exact_approved_content_and_deterministic_message_id(env):
    connect(env)
    co, ct = seed(env)
    d = approved(env, co, ct)
    r = send(env, d)
    assert r.status_code == 200, r.text
    sent = r.json()
    assert sent["status"] == "sent" and sent["dispatch"]["gmail_message_id"] == "g1"
    mime = env.google.sent_mime()
    assert mime["To"] == "aino@acme.fi" and mime["Subject"] == "Succession at Acme"
    assert mime["Message-ID"] == f"<permetheus.{d['id'].replace('-', '')}.v1@example.com>"
    assert mime.get_content().strip() == "Hello Aino, would you consider a conversation?"

    again = send(env, d)
    assert again.status_code == 409 and len(env.google.called(f"{GMAIL}/messages/send")) == 1
    conv = env.c.get("/api/outreach/conversations").json()
    assert len(conv) == 1 and conv[0]["gmail_thread_id"] == "t1" and conv[0]["status"] == "awaiting_reply"


def test_edit_after_approval_invalidates_and_stale_preview_rejected(env):
    connect(env)
    co, ct = seed(env)
    d = approved(env, co, ct)
    edited = env.c.patch(f"/api/outreach/drafts/{d['id']}", json={"body": "Changed text"}).json()
    assert edited["status"] == "draft" and edited["version"] == 2 and edited["approval"] is None
    assert send(env, edited).json()["error"]["code"] == "not_approved"
    stale = approve(env, d)  # approving the version the operator saw before the edit
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_preview"
    assert approve(env, edited).status_code == 200
    assert not env.google.called(f"{GMAIL}/messages/send")


def test_expired_approval_blocks_send(env):
    connect(env)
    co, ct = seed(env)
    d = approved(env, co, ct)
    with env.db() as db:
        db.get(mail.OutreachDraft, uuid.UUID(d["id"])).approval_expires_at = utcnow() - timedelta(minutes=1)
        db.commit()
    assert send(env, d).json()["error"]["code"] == "approval_expired"
    assert not env.google.called(f"{GMAIL}/messages/send")


@pytest.mark.parametrize("block", ["email", "company", "channel", "global"])
def test_suppression_and_stop_switch_checked_at_send_time(env, block):
    connect(env)
    co, ct = seed(env)
    d = approved(env, co, ct)  # approved before the suppression exists
    if block == "global":
        env.c.post("/api/outreach/stop", json={"stopped": True})
    else:
        value = {"email": "AINO@acme.fi", "company": co, "channel": "email"}[block]
        assert env.c.post("/api/outreach/suppressions", json={"kind": block, "value": value, "reason": "test"}).status_code == 201
    code = send(env, d).json()["error"]["code"]
    assert code == ("outreach_stopped" if block == "global" else "suppressed")
    assert not env.google.called(f"{GMAIL}/messages/send")


def test_unverified_contact_and_unconfirmed_company_block(env):
    connect(env)
    co, ct = seed(env, verification=Verification.unverified)
    assert send(env, approved(env, co, ct)).json()["error"]["code"] == "contact_unverified"
    co2, ct2 = seed(env, status=CompanyStatus.provisional)
    assert send(env, approved(env, co2, ct2)).json()["error"]["code"] == "company_unconfirmed"
    co3, ct3 = seed(env)
    extra = approved(env, co3, ct3, recipients=["aino@acme.fi", "stranger@acme.fi"])
    assert send(env, extra).json()["error"]["code"] == "contact_unverified"


def test_timeout_after_possible_acceptance_is_unknown_never_retried_then_reconciled(env):
    connect(env)
    co, ct = seed(env)
    d = approved(env, co, ct)

    def timeout(request):
        raise httpx.ReadTimeout("read timed out", request=request)

    env.google.routes[("POST", f"{GMAIL}/messages/send")] = timeout
    r = send(env, d)
    assert r.status_code == 502 and r.json()["error"]["code"] == "delivery_unknown"
    assert env.c.get(f"/api/outreach/drafts/{d['id']}").json()["status"] == "delivery_unknown"
    assert send(env, d).status_code == 409
    assert env.c.patch(f"/api/outreach/drafts/{d['id']}", json={"body": "x"}).json()["error"]["code"] == "draft_locked"
    assert len(env.google.called(f"{GMAIL}/messages/send")) == 1

    msgid = env.google.sent_mime()["Message-ID"]
    env.google.routes[("GET", f"{GMAIL}/messages")] = lambda req: httpx.Response(
        200, json={"messages": [{"id": "g9", "threadId": "t9"}]} if req.url.params["q"] == f"rfc822msgid:{msgid.strip('<>')}" else {})
    rec = env.c.post("/api/outreach/reconcile").json()["reconciled"]
    assert rec[0]["state"] == "sent"
    after = env.c.get(f"/api/outreach/drafts/{d['id']}").json()
    assert after["status"] == "sent" and after["dispatch"]["gmail_thread_id"] == "t9"
    assert len(env.google.called(f"{GMAIL}/messages/send")) == 1


def test_definite_rejection_is_failed_and_connect_failure_never_unknown(env):
    connect(env)
    co, ct = seed(env)
    env.google.routes[("POST", f"{GMAIL}/messages/send")] = lambda r: httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}})
    assert send(env, approved(env, co, ct)).json()["error"]["code"] == "send_failed"

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    env.google.routes[("POST", f"{GMAIL}/messages/send")] = refused
    d = approved(env, co, ct)
    assert send(env, d).json()["error"]["code"] == "send_failed"
    # Failed is editable into a new version, which gets a new Message-ID.
    edited = env.c.patch(f"/api/outreach/drafts/{d['id']}", json={"body": "Retry text"}).json()
    assert edited["version"] == 2


# ---------------------------------------------------------------- sync and replies

def sent_conversation(env):
    connect(env)
    co, ct = seed(env)
    assert send(env, approved(env, co, ct)).status_code == 200
    return co, ct, env.c.get("/api/outreach/conversations").json()[0]["id"]


def test_sync_reads_only_tracked_threads_dedupes_and_auto_suppresses_optout(env):
    co, ct, conv_id = sent_conversation(env)
    serve_history(env, ("r1", "t1"), ("x9", "unrelated-thread"))
    serve_message(env, "r1", "Please unsubscribe me.\n\nOn Mon, Me wrote:\n> would you consider interested?")
    r = env.c.post("/api/gmail/sync")
    assert r.json()["ingested"] == 1
    assert not env.google.called(f"{GMAIL}/messages/x9")  # unrelated mail never fetched
    assert env.c.post("/api/gmail/sync").json()["ingested"] == 0  # duplicate poll result

    detail = env.c.get(f"/api/outreach/conversations/{conv_id}").json()
    reply = [m for m in detail["messages"] if m["direction"] == "inbound"]
    assert len(reply) == 1 and reply[0]["proposed_intent"] == "optout" and "wrote" not in reply[0]["body_text"]
    assert detail["status"] == "opted_out"
    assert env.c.get("/api/outreach/suppressions").json()[0]["value"] == "aino@acme.fi"
    assert send(env, approved(env, co, ct)).json()["error"]["code"] == "suppressed"


def test_expired_history_triggers_bounded_resync(env):
    _, _, conv_id = sent_conversation(env)
    env.google.routes[("GET", f"{GMAIL}/history")] = lambda r: httpx.Response(404, json={"error": {"status": "NOT_FOUND"}})
    env.google.routes[("GET", f"{GMAIL}/threads/t1")] = lambda r: httpx.Response(200, json={"messages": [{"id": "g1"}, {"id": "r2"}]})
    serve_message(env, "r2", "Maybe later, call me in spring.", msgid="<r2@acme.fi>")
    r = env.c.post("/api/gmail/sync").json()
    assert r["resynced"] is True and r["ingested"] == 1
    assert not env.google.called(f"{GMAIL}/messages/g1")  # our own sent message is already stored
    assert env.c.get(f"/api/outreach/conversations/{conv_id}").json()["status"] == "needs_review"


def test_interest_needs_human_confirmation_and_followup_never_fills_financials(env):
    co, ct, conv_id = sent_conversation(env)
    serve_history(env, ("r1", "t1"))
    serve_message(env, "r1", "Yes, we are interested. Tell me more.")
    env.c.post("/api/gmail/sync")
    with env.db() as db:
        assert db.get(Company, uuid.UUID(co)).seller_intent is SellerIntent.unknown
        src = Source(kind=SourceKind.registry, title="Registry")
        db.add(src)
        db.flush()
        db.add(FinancialObservation(company_id=uuid.UUID(co), source_id=src.id, metric=FinancialMetric.revenue,
                                    amount=1234567, currency="EUR", period_start=date(2025, 1, 1), period_end=date(2025, 12, 31),
                                    scope=FinancialScope.entity, status=FinancialStatus.reported, review_status=ReviewStatus.accepted))
        db.commit()

    assert env.c.post(f"/api/outreach/conversations/{conv_id}/followup").json()["error"]["code"] == "interest_not_confirmed"
    detail = env.c.get(f"/api/outreach/conversations/{conv_id}").json()
    reply = next(m for m in detail["messages"] if m["direction"] == "inbound")
    assert reply["proposed_intent"] == "interested" and reply["confirmed_intent"] is None

    r = env.c.post(f"/api/outreach/conversations/{conv_id}/classify", json={"message_id": reply["id"], "intent": "interested"})
    assert r.json()["status"] == "interested"
    with env.db() as db:  # unverified speaker: statement recorded, company intent unchanged
        assert db.get(Company, uuid.UUID(co)).seller_intent is SellerIntent.unknown

    f = env.c.post(f"/api/outreach/conversations/{conv_id}/followup").json()
    assert f["status"] == "draft" and f["subject"] == "Re: Succession at Acme" and f["conversation_id"] == conv_id
    assert "revenue" not in f["disclosure"]["fields"] and "ebitda" in f["disclosure"]["fields"]
    assert "1234567" not in f["body"] and "1 234 567" not in f["body"]

    assert send(env, approve(env, f).json()).status_code == 200
    mime = env.google.sent_mime()
    assert mime["In-Reply-To"] == "<r1@acme.fi>" and mime["References"].split()[0].startswith("<permetheus.")
    assert json.loads(env.google.called(f"{GMAIL}/messages/send")[-1].content)["threadId"] == "t1"


def test_reply_after_approval_forces_re_review(env):
    co, ct, conv_id = sent_conversation(env)
    follow = approved(env, co, ct, conversation_id=conv_id, subject="Re: Succession at Acme")
    serve_history(env, ("r1", "t1"))
    serve_message(env, "r1", "Hmm, who is asking?")
    env.c.post("/api/gmail/sync")
    assert send(env, follow).json()["error"]["code"] == "conversation_changed"


def test_classifier_rules():
    assert mail.classify_reply("We are not interested, thanks")[0] is mail.ReplyIntent.no
    assert mail.classify_reply("Kiinnostaa, soitetaan")[0] is mail.ReplyIntent.interested
    assert mail.classify_reply("Interested, but please remove me from this list")[0] is mail.ReplyIntent.optout
    assert mail.classify_reply("Out of office until Monday")[0] is mail.ReplyIntent.unclear
    assert mail.new_reply_text("Sure.\n> old\nOn Tue, X wrote:\nold text") == "Sure."


# ---------------------------------------------------------------- sequences

STEPS = [
    {"kind": "ask_interest", "subject": "Future of {company_name}", "body": "Hello {contact_name}, a short question."},
    {"kind": "ask_interest", "delay_hours": 72, "approval": "template", "body": "Hello {contact_name}, following up."},
    {"kind": "request_missing_fields", "delay_hours": 1},
]


def make_due(env):
    with env.db() as db:
        for e in db.scalars(select(mail.Enrollment)):
            e.next_run_at = utcnow() - timedelta(seconds=1)
        db.commit()


def test_sequence_validation(env):
    bad_first = [{**STEPS[0], "approval": "template"}]
    assert env.c.post("/api/outreach/sequences", json={"name": "x", "steps": bad_first}).status_code == 422
    unknown = [{**STEPS[0], "body": "Revenue {revenue}"}]
    assert env.c.post("/api/outreach/sequences", json={"name": "x", "steps": unknown}).status_code == 422
    no_wait = [STEPS[0], {**STEPS[1], "delay_hours": 0}]
    assert env.c.post("/api/outreach/sequences", json={"name": "x", "steps": no_wait}).status_code == 422


def test_sequence_first_step_needs_review_template_followup_sends_within_scope(env):
    connect(env)
    co, ct = seed(env)
    seq = env.c.post("/api/outreach/sequences", json={"name": "Owners", "steps": STEPS}).json()
    e = env.c.post(f"/api/outreach/sequences/{seq['id']}/enrollments", json={"company_id": co, "contact_id": ct}).json()

    first = env.c.post("/api/outreach/run-due").json()["results"][0]
    assert first["result"] == "draft_awaiting_review"
    d = env.c.get(f"/api/outreach/drafts/{first['draft_id']}").json()
    assert d["subject"] == "Future of Acme Oy" and d["status"] == "draft"
    assert send(env, approve(env, d).json()).status_code == 200
    enrolled = env.c.get("/api/outreach/enrollments").json()[0]
    assert enrolled["step_index"] == 1 and enrolled["conversation_id"]
    assert env.c.post("/api/outreach/run-due").json()["results"] == []  # durable 72h wait

    r = env.c.post(f"/api/outreach/sequences/{seq['id']}/template-approvals", json={"step_index": 1, "company_ids": [co]})
    assert r.status_code == 201
    make_due(env)
    env.c.post(f"/api/outreach/sequences/{seq['id']}/pause")
    assert env.c.post("/api/outreach/run-due").json()["results"] == []
    env.c.post(f"/api/outreach/sequences/{seq['id']}/resume")
    env.c.post("/api/outreach/stop", json={"stopped": True})
    assert env.c.post("/api/outreach/run-due").json() == {"stopped": True, "results": []}
    env.c.post("/api/outreach/stop", json={"stopped": False})

    auto = env.c.post("/api/outreach/run-due").json()["results"][0]
    assert auto["result"] == "sent" and len(env.google.called(f"{GMAIL}/messages/send")) == 2
    assert env.google.sent_mime()["Subject"] == "Re: Future of Acme Oy"

    make_due(env)  # missing-field step waits for a confirmed interested reply
    assert env.c.post("/api/outreach/run-due").json()["results"][0]["result"] == "awaiting_review"
    assert env.c.get("/api/outreach/enrollments").json()[0]["id"] == e["id"]


def test_template_approval_outside_company_scope_requires_review(env):
    connect(env)
    co, ct = seed(env)
    other, _ = seed(env)
    seq = env.c.post("/api/outreach/sequences", json={"name": "Owners", "steps": STEPS}).json()
    env.c.post(f"/api/outreach/sequences/{seq['id']}/template-approvals", json={"step_index": 1, "company_ids": [other]})
    env.c.post(f"/api/outreach/sequences/{seq['id']}/enrollments", json={"company_id": co, "contact_id": ct})
    first = env.c.post("/api/outreach/run-due").json()["results"][0]
    send(env, approve(env, env.c.get(f"/api/outreach/drafts/{first['draft_id']}").json()).json())
    make_due(env)
    step = env.c.post("/api/outreach/run-due").json()["results"][0]
    assert step["result"] == "draft_awaiting_review"  # company not in the approval scope
    assert len(env.google.called(f"{GMAIL}/messages/send")) == 1


# ---------------------------------------------------------------- review findings

def sends(env) -> int:
    return len(env.google.called(f"{GMAIL}/messages/send"))


def account(env) -> mail.GmailAccount:
    with env.db() as db:
        return db.scalar(select(mail.GmailAccount))


def timed_out_send(env):
    connect(env)
    co, ct = seed(env)
    d = approved(env, co, ct)

    def timeout(request):
        raise httpx.ReadTimeout("read timed out", request=request)

    env.google.routes[("POST", f"{GMAIL}/messages/send")] = timeout
    assert send(env, d).json()["error"]["code"] == "delivery_unknown"
    return d


def test_reconcile_miss_never_fails_and_searches_trash(env):
    d = timed_out_send(env)
    with env.db() as db:  # far past any grace period
        db.scalar(select(mail.Dispatch)).claimed_at = utcnow() - timedelta(days=1)
        db.commit()
    env.google.routes[("GET", f"{GMAIL}/messages")] = lambda r: httpx.Response(200, json={"resultSizeEstimate": 0})
    assert env.c.post("/api/outreach/reconcile").json()["reconciled"][0]["state"] == "delivery_unknown"
    assert env.google.called(f"{GMAIL}/messages")[0].url.params["includeSpamTrash"] == "true"
    assert env.c.get(f"/api/outreach/drafts/{d['id']}").json()["status"] == "delivery_unknown"
    assert env.c.patch(f"/api/outreach/drafts/{d['id']}", json={"body": "x"}).json()["error"]["code"] == "draft_locked"
    assert sends(env) == 1


def test_plain_5xx_from_send_is_delivery_unknown(env):
    connect(env)
    co, ct = seed(env)
    env.google.routes[("POST", f"{GMAIL}/messages/send")] = lambda r: httpx.Response(500, json={"error": {"status": "INTERNAL"}})
    d = approved(env, co, ct)
    assert send(env, d).json()["error"]["code"] == "delivery_unknown"
    assert env.c.get(f"/api/outreach/drafts/{d['id']}").json()["dispatch"]["state"] == "delivery_unknown"


def test_reconnect_same_mailbox_keeps_cursor_so_missed_reply_is_read(env):
    sent_conversation(env)
    env.google.routes[("GET", f"{GMAIL}/profile")] = lambda r: httpx.Response(200, json={"emailAddress": "me@example.com", "historyId": "900"})
    connect(env)  # e.g. after the 7-day Testing refresh token expired
    assert account(env).history_id == "100"
    serve_history(env, ("r1", "t1"))
    serve_message(env, "r1", "Sorry for the delay, who is asking?")
    assert env.c.post("/api/gmail/sync").json()["ingested"] == 1
    assert env.google.called(f"{GMAIL}/history")[0].url.params["startHistoryId"] == "100"


def test_disconnect_keeps_mailbox_identity_and_switch_is_refused_while_history_exists(env):
    sent_conversation(env)
    assert env.c.post("/api/gmail/disconnect").json() == {"connected": False, "revoked": True}
    kept = account(env)
    assert kept.email == "me@example.com" and kept.history_id == "100" and kept.token_ciphertext == ""
    assert env.c.post("/api/gmail/sync").json()["error"]["code"] == "gmail_reauth_required"

    env.google.routes[("GET", f"{GMAIL}/profile")] = lambda r: httpx.Response(200, json={"emailAddress": "other@example.com", "historyId": "5"})
    state = start_oauth(env)
    r = env.c.get("/oauth/google/callback", params={"code": "abc", "state": state}, follow_redirects=False)
    assert r.headers["location"].endswith("reason=mailbox_mismatch")
    assert account(env).email == "me@example.com"
    assert len(env.google.called("/revoke")) == 2  # the refused mailbox's fresh token is revoked too


def test_switch_without_history_replaces_and_revokes_old_mailbox(env):
    connect(env)
    env.google.routes[("GET", f"{GMAIL}/profile")] = lambda r: httpx.Response(200, json={"emailAddress": "other@example.com", "historyId": "5"})
    connect(env)
    assert account(env).email == "other@example.com" and account(env).history_id == "5"
    assert len(env.google.called("/revoke")) == 1


def sequence_at_step1(env):
    connect(env)
    co, ct = seed(env)
    seq = env.c.post("/api/outreach/sequences", json={"name": "Owners", "steps": STEPS}).json()
    env.c.post(f"/api/outreach/sequences/{seq['id']}/enrollments", json={"company_id": co, "contact_id": ct})
    first = env.c.post("/api/outreach/run-due").json()["results"][0]
    assert send(env, approve(env, env.c.get(f"/api/outreach/drafts/{first['draft_id']}").json()).json()).status_code == 200
    env.c.post(f"/api/outreach/sequences/{seq['id']}/template-approvals", json={"step_index": 1, "company_ids": [co]})
    return co, ct


def test_overlapping_run_due_sends_a_step_once(env):
    sequence_at_step1(env)
    make_due(env)
    with env.db() as a, env.db() as b:
        ea, eb = a.scalar(select(mail.Enrollment)), b.scalar(select(mail.Enrollment))  # both read before either claims
        first, second = mail._run_step(env.app, a, ea), mail._run_step(env.app, b, eb)
    assert first["result"] == "sent" and second["result"] == "skipped"
    assert sends(env) == 2 and len(env.c.get("/api/outreach/drafts").json()) == 2


def test_reply_in_gmail_but_not_synced_blocks_in_thread_send(env):
    co, ct, conv_id = sent_conversation(env)
    follow = approved(env, co, ct, conversation_id=conv_id, subject="Re: Succession at Acme")
    env.google.add("t1", "r5")  # arrived after the last sync
    assert send(env, follow).json()["error"]["code"] == "conversation_changed"
    assert sends(env) == 1


def test_template_step_not_auto_sent_after_contact_replied(env):
    sequence_at_step1(env)
    serve_history(env, ("r1", "t1"))
    serve_message(env, "r1", "Yes, we are interested. Tell me more.")
    env.c.post("/api/gmail/sync")
    conv = env.c.get("/api/outreach/conversations").json()[0]
    env.c.post(f"/api/outreach/conversations/{conv['id']}/classify",
               json={"message_id": conv["latest_reply"]["id"], "intent": "interested"})
    assert env.c.get("/api/outreach/enrollments").json()[0]["state"] == "active"  # re-activated by "interested"
    make_due(env)
    step = env.c.post("/api/outreach/run-due").json()["results"][0]
    assert step["result"] == "blocked" and step["error"] == "conversation_changed"
    assert sends(env) == 1


def test_reply_from_someone_else_cannot_claim_authority_or_the_contacts_name(env):
    co, _, conv_id = sent_conversation(env)
    serve_history(env, ("r1", "t1"))
    serve_message(env, "r1", "We are interested.", sender="other@acme.fi")
    env.c.post("/api/gmail/sync")
    reply = env.c.get(f"/api/outreach/conversations/{conv_id}").json()["latest_reply"]
    url = f"/api/outreach/conversations/{conv_id}/classify"
    owner = env.c.post(url, json={"message_id": reply["id"], "intent": "interested", "speaker_authority": "owner"})
    assert owner.status_code == 409 and owner.json()["error"]["code"] == "speaker_unverified"
    assert env.c.post(url, json={"message_id": reply["id"], "intent": "interested"}).status_code == 200
    again = env.c.post(url, json={"message_id": reply["id"], "intent": "interested"})
    assert again.json()["error"]["code"] == "already_classified"
    with env.db() as db:
        statements = db.scalars(select(IntentStatement)).all()
        assert [s.speaker_name for s in statements] == ["other@acme.fi"]
        assert db.get(Company, uuid.UUID(co)).seller_intent is SellerIntent.unknown


def test_stop_committed_before_claim_is_caught_by_recheck(env, monkeypatch):
    connect(env)
    co, ct = seed(env)
    d = approved(env, co, ct)
    env.c.post("/api/outreach/stop", json={"stopped": True})
    real, calls = mail._suppression_hit, []

    def first_check_misses(db, draft):  # the stop lands between the first check and the claim
        calls.append(1)
        return None if len(calls) == 1 else real(db, draft)

    monkeypatch.setattr(mail, "_suppression_hit", first_check_misses)
    assert send(env, d).json()["error"]["code"] == "outreach_stopped"
    after = env.c.get(f"/api/outreach/drafts/{d['id']}").json()
    assert sends(env) == 0 and after["status"] == "failed" and "outreach_stopped" in after["dispatch"]["error"]


def test_edit_or_approve_that_read_before_a_claim_changes_nothing(env):
    connect(env)
    co, ct = seed(env)
    d = approved(env, co, ct)
    with env.db() as stale:
        read = stale.get(mail.OutreachDraft, uuid.UUID(d["id"]))  # read before the claim (held: the identity map is weak)
        with env.db() as other:
            other.get(mail.OutreachDraft, uuid.UUID(d["id"])).status = mail.DraftStatus.sending
            other.commit()
        assert read.status is mail.DraftStatus.approved  # passes the pre-check; only the conditional update stops it
        with pytest.raises(mail.ApiError) as exc:
            mail.patch_draft(uuid.UUID(d["id"]), mail.DraftPatch(body="Never sent"), stale)
        assert exc.value.code == "draft_conflict"
    fresh = draft(env, co, ct)
    with env.db() as stale:
        read = stale.get(mail.OutreachDraft, uuid.UUID(fresh["id"]))
        env.c.patch(f"/api/outreach/drafts/{fresh['id']}", json={"body": "Edited"})
        assert read.version == 1
        with pytest.raises(mail.ApiError) as exc:
            mail.approve_draft(uuid.UUID(fresh["id"]), mail.ApproveIn(version=1, content_hash=fresh["content_hash"]), stale)
        assert exc.value.code == "stale_preview"
    assert env.c.get(f"/api/outreach/drafts/{d['id']}").json()["body"] != "Never sent"


def test_auto_reply_after_optout_keeps_thread_closed_and_optout_covers_contact(env):
    _, _, conv_id = sent_conversation(env)
    serve_history(env, ("r1", "t1"))
    serve_message(env, "r1", "Please remove me from your list.", sender="assistant@acme.fi")
    env.c.post("/api/gmail/sync")
    assert {s["value"] for s in env.c.get("/api/outreach/suppressions").json()} == {"assistant@acme.fi", "aino@acme.fi"}
    serve_history(env, ("r2", "t1"), history_id="130")
    serve_message(env, "r2", "Out of office until Monday.", msgid="<r2@acme.fi>")
    assert env.c.post("/api/gmail/sync").json()["ingested"] == 1
    assert env.c.get(f"/api/outreach/conversations/{conv_id}").json()["status"] == "opted_out"


def test_history_pages_are_followed(env):
    sent_conversation(env)
    serve_message(env, "r1", "First")
    serve_message(env, "r2", "Second", msgid="<r2@acme.fi>")

    def history(request):
        if request.url.params.get("pageToken") != "p2":
            return httpx.Response(200, json={"history": [{"messagesAdded": [{"message": {"id": "r1", "threadId": "t1"}}]}],
                                             "nextPageToken": "p2"})
        return httpx.Response(200, json={"history": [{"messagesAdded": [{"message": {"id": "r2", "threadId": "t1"}}]}],
                                         "historyId": "150"})

    env.google.routes[("GET", f"{GMAIL}/history")] = history
    assert env.c.post("/api/gmail/sync").json() == {"ingested": 2, "resynced": False, "complete": True, "tracked_threads": 1}
    assert account(env).history_id == "150"


def test_history_too_long_falls_back_to_resync(env, monkeypatch):
    sent_conversation(env)
    monkeypatch.setattr(mail, "MAX_HISTORY_PAGES", 2)
    env.google.routes[("GET", f"{GMAIL}/history")] = lambda r: httpx.Response(200, json={"history": [], "nextPageToken": "more"})
    serve_message(env, "r1", "Hello")
    r = env.c.post("/api/gmail/sync").json()
    assert r["resynced"] is True and r["ingested"] == 1 and len(env.google.called(f"{GMAIL}/history")) == 2


def test_resync_over_limit_does_not_advance_cursor(env, monkeypatch):
    co, _, _ = sent_conversation(env)
    with env.db() as db:
        db.add(mail.Conversation(company_id=uuid.UUID(co), gmail_thread_id="t2", subject="Other"))
        db.commit()
    monkeypatch.setattr(mail, "MAX_RESYNC_THREADS", 1)
    env.google.routes[("GET", f"{GMAIL}/history")] = lambda r: httpx.Response(404, json={"error": {"status": "NOT_FOUND"}})
    r = env.c.post("/api/gmail/sync").json()
    assert r["resynced"] is True and r["complete"] is False
    assert account(env).history_id == "100" and "cursor not advanced" in account(env).last_error


def test_thread_started_during_sync_is_still_tracked(env):
    co, _, _ = sent_conversation(env)

    def history(request):  # a send commits a new conversation after sync started
        with env.db() as db:
            db.add(mail.Conversation(company_id=uuid.UUID(co), gmail_thread_id="t2", subject="New"))
            db.commit()
        return httpx.Response(200, json={"history": [{"messagesAdded": [{"message": {"id": "r7", "threadId": "t2"}}]}],
                                         "historyId": "140"})

    env.google.routes[("GET", f"{GMAIL}/history")] = history
    serve_message(env, "r7", "Automatic reply: received", tid="t2", msgid="<r7@acme.fi>")
    assert env.c.post("/api/gmail/sync").json()["ingested"] == 1


def test_new_thread_returned_by_gmail_is_the_one_tracked(env):
    co, ct, conv_id = sent_conversation(env)
    follow = approved(env, co, ct, conversation_id=conv_id, subject="A different subject")
    env.google.routes[("POST", f"{GMAIL}/messages/send")] = lambda r: env.google.accept("g2", "t2")
    sent = send(env, follow).json()
    new = next(c for c in env.c.get("/api/outreach/conversations").json() if c["gmail_thread_id"] == "t2")
    assert sent["conversation_id"] == new["id"] != conv_id
    serve_history(env, ("r1", "t2"))
    serve_message(env, "r1", "Answering the new thread", tid="t2")
    assert env.c.post("/api/gmail/sync").json()["ingested"] == 1


def test_second_finisher_is_a_no_op(env):
    d = timed_out_send(env)
    with env.db() as stale:
        record = stale.scalar(select(mail.Dispatch))
        dr = stale.get(mail.OutreachDraft, uuid.UUID(d["id"]))
        env.google.routes[("GET", f"{GMAIL}/messages")] = lambda r: httpx.Response(200, json={"messages": [{"id": "g9", "threadId": "t9"}]})
        assert env.c.post("/api/outreach/reconcile").json()["reconciled"][0]["state"] == "sent"
        assert mail._finalize_sent(stale, dr, record, "me@example.com", "g9", "t9") is False
    with env.db() as db:
        assert len(db.scalars(select(mail.MailMessage)).all()) == 1


def test_reconcile_found_send_reads_replies_in_its_thread(env):
    timed_out_send(env)
    env.google.routes[("GET", f"{GMAIL}/messages")] = lambda r: httpx.Response(200, json={"messages": [{"id": "g9", "threadId": "t9"}]})
    env.google.add("t9", "g9")
    serve_message(env, "r1", "Quick reply before you reconciled", tid="t9")
    env.c.post("/api/outreach/reconcile")
    conv = env.c.get("/api/outreach/conversations").json()[0]
    assert conv["gmail_thread_id"] == "t9" and conv["latest_reply"]["body_text"] == "Quick reply before you reconciled"


PG_URL = os.environ.get("PERMETHEUS_TEST_POSTGRES_URL")


@pytest.mark.skipif(not PG_URL, reason="set PERMETHEUS_TEST_POSTGRES_URL to a disposable PostgreSQL database")
def test_postgres_concurrent_run_due_claims_once():
    with make_env(PG_URL) as env:
        try:
            sequence_at_step1(env)
            make_due(env)
            barrier = threading.Barrier(2)
            with env.db() as a, env.db() as b:
                enrollments = [s.scalar(select(mail.Enrollment)) for s in (a, b)]

                def run(pair):
                    barrier.wait()
                    return mail._run_step(env.app, *pair)

                with ThreadPoolExecutor(2) as pool:
                    results = list(pool.map(run, [(a, enrollments[0]), (b, enrollments[1])]))
            assert sorted(r["result"] for r in results) == ["sent", "skipped"] and sends(env) == 2
        finally:
            Base.metadata.drop_all(env.db.kw["bind"])


def test_oauth_rejects_cross_hostname_cookie_binding(env):
    r = env.c.post("/api/gmail/connect", headers={"Origin": "http://127.0.0.1:4310"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "oauth_host_mismatch"
