import asyncio
import io
import math
import shutil
import struct
import uuid
import wave
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import update
from starlette.websockets import WebSocketDisconnect

from permetheus import voice as voice_mod
from permetheus.models import Company, SellerIntent, utcnow

from conftest import PASSWORD, make_client

ORIGIN = "http://localhost:4310"
INTRO = "Hi, I'm Aino, an AI assistant calling on behalf of the deal team."


class FakeRuntime:
    """Mirrors the SpeechRuntime surface used by voice.py."""

    def __init__(self, root: Path):
        self.data_root = root
        self.asr_lock = asyncio.Lock()
        self.final_text = "hello there"
        self.tts_delay, self.tts_chunks = 0.0, 3
        self.spoken, self.fed, self.cancelled, self.cloned = [], 0, [], []
        self.fail_next_clone = False

    async def asr_start(self, request_id):
        assert self.asr_lock.locked(), "live ASR must run while the conversation holds asr_lock"

    async def asr_feed(self, request_id, samples):
        assert 0 < len(samples) <= 16000 and all(-1 <= s <= 1 for s in samples)
        self.fed += len(samples)
        return {"type": "partial", "id": request_id, "committed": "he", "tentative": "l"}

    async def asr_finalize(self, request_id):
        return self.final_text

    async def asr_cancel(self, request_id):
        self.cancelled.append(request_id)

    async def tts_stream(self, text, voice_path=None):
        self.spoken.append((text, voice_path))
        for _ in range(self.tts_chunks):
            await asyncio.sleep(self.tts_delay)
            yield 24000, b"\x01\x00" * 240

    async def clone_voice(self, source, target):
        if self.fail_next_clone:
            self.fail_next_clone = False
            raise RuntimeError("synthetic clone failure")
        with wave.open(str(source)) as w:
            self.cloned.append((w.getframerate(), w.readframes(w.getnframes())))
        Path(target).write_bytes(b"voice-state")

    async def close(self):
        return None


class FakeLLM:
    configured = True

    def __init__(self):
        self.tokens = ["Thanks for your time. ", "I'm an AI assistant", ", how can I help?"]
        self.calls = []

    async def stream(self, messages, max_tokens=250):
        self.calls.append(messages)
        for token in self.tokens:
            await asyncio.sleep(0)
            yield token

    async def extract(self, text, instruction):
        return {"summary": "Owner is open to a follow-up.", "stated_interest": "interested", "follow_ups": ["email"]}


@pytest.fixture
def v(tmp_path):
    runtime, llm = FakeRuntime(tmp_path), FakeLLM()
    client = make_client()
    client.app.state.speech, client.app.state.llm = runtime, llm
    with client:
        r = client.post("/api/auth/login", json={"password": PASSWORD})
        client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        yield SimpleNamespace(client=client, runtime=runtime, llm=llm, root=tmp_path)


def make_agent(v, **overrides):
    r = v.client.post("/api/voice/agents", json={"name": "Aino", "introduction": INTRO, **overrides})
    assert r.status_code == 201, r.text
    return r.json()


def start_session(v, agent_id, consent=True, **extra):
    r = v.client.post("/api/voice/browser-sessions",
                      json={"agent_id": agent_id, "recording_consent": consent, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def connect(v, session_id, origin=ORIGIN):
    return v.client.websocket_connect(f"/api/voice/browser/{session_id}", headers={"origin": origin})


def until(ws, kind):
    seen = []
    while True:
        message = ws.receive_json()
        seen.append(message)
        if message["type"] == kind:
            return message, seen


def pcm_frame(samples=1600):
    return struct.pack(f"<{samples}h", *[int(8000 * math.sin(i / 10)) for i in range(samples)])


def wav_bytes(seconds, rate=24000):
    frames = struct.pack(f"<{int(seconds * rate)}h",
                         *[int(6000 * math.sin(i / 7)) for i in range(int(seconds * rate))])
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue(), frames


AUTHORIZATION = {"voice_owner_name": "Aino Virtanen", "owner_authorization": "true", "authorization_basis": "self"}
CONSENT = {"consent_action": "create_own_voice"}  # current contract: no checkbox fields


# ---------- agents ----------

def test_agents_require_auth_csrf_and_ai_disclosure(v):
    r = v.client.post("/api/voice/agents", json={"name": "Sam", "introduction": "Hi, this is Sam from the team."})
    assert r.status_code == 422 and "AI" in r.text
    agent = make_agent(v)
    assert agent["voice"]["kind"] == "default" and agent["voice"]["label"] == "Default voice"
    assert "profile_path" not in agent

    r = v.client.patch(f"/api/voice/agents/{agent['id']}", json={"introduction": "Hello, I am Sam."})
    assert r.status_code == 422
    r = v.client.patch(f"/api/voice/agents/{agent['id']}", json={"max_duration_seconds": 120})
    assert r.json()["max_duration_seconds"] == 120
    assert [a["id"] for a in v.client.get("/api/voice/agents").json()] == [agent["id"]]

    token = v.client.headers.pop("X-CSRF-Token")
    assert v.client.post("/api/voice/agents", json={"name": "x", "introduction": INTRO}).status_code == 403
    v.client.headers["X-CSRF-Token"] = token
    v.client.cookies.clear()
    assert v.client.get("/api/voice/agents").status_code == 401


def test_capabilities_label_phone_unavailable(v):
    caps = v.client.get("/api/voice/capabilities").json()
    assert caps["browser"]["available"] is True and caps["browser"]["label"] == "Browser conversation"
    assert caps["phone"]["available"] is False


# ---------- voice samples ----------

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@needs_ffmpeg
def test_voice_sample_requires_authorization_and_bounds(v):
    agent_id = make_agent(v)["id"]
    url = f"/api/voice/agents/{agent_id}/voice-sample"
    good, _ = wav_bytes(5)

    r = v.client.post(url, files={"sample": ("s.wav", good, "audio/wav")}, data={"voice_owner_name": "A"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "authorization_required"
    r = v.client.post(url, files={"sample": ("s.wav", b"\0" * (11 * 1024 * 1024), "audio/wav")}, data=AUTHORIZATION)
    assert r.status_code == 413
    r = v.client.post(url, files={"sample": ("s.wav", b"not audio at all" * 100, "audio/wav")}, data=AUTHORIZATION)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_audio"
    r = v.client.post(url, files={"sample": ("s.wav", wav_bytes(35)[0], "audio/wav")}, data=AUTHORIZATION)
    assert r.status_code == 422 and r.json()["error"]["code"] == "sample_too_long"
    r = v.client.post(url, files={"sample": ("s.wav", wav_bytes(1)[0], "audio/wav")}, data=AUTHORIZATION)
    assert r.status_code == 422 and r.json()["error"]["code"] == "sample_too_short"
    assert v.runtime.cloned == []

    asyncio.run(v.runtime.asr_lock.acquire())
    r = v.client.post(url, files={"sample": ("s.wav", good, "audio/wav")}, data=AUTHORIZATION)
    assert r.status_code == 409


@needs_ffmpeg
def test_voice_clone_preview_and_default_switch(v):
    agent_id = make_agent(v)["id"]
    good, frames = wav_bytes(5)
    r = v.client.post(f"/api/voice/agents/{agent_id}/voice-sample",
                      files={"sample": ("s.wav", good, "audio/wav")}, data=AUTHORIZATION)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["voice"]["kind"] == "cloned"
    assert body["voice"]["authorization"]["voice_owner_name"] == "Aino Virtanen"
    assert str(v.root) not in r.text and "safetensors" not in r.text
    assert v.runtime.cloned == [(24000, frames)]  # multipart + ffmpeg round trip is lossless
    profiles = list((v.root / "voices" / agent_id).iterdir())
    assert [p.suffix for p in profiles] == [".safetensors"]  # raw sample deleted after cloning

    r = v.client.get(f"/api/voice/agents/{agent_id}/preview")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(r.content)) as w:
        assert w.getframerate() == 24000 and w.getnframes() == 720
    assert v.runtime.spoken[-1] == (INTRO, f"voices/{agent_id}/{profiles[0].name}")

    r = v.client.patch(f"/api/voice/agents/{agent_id}", json={"voice": "default"})
    assert r.json()["voice"]["kind"] == "default"
    v.client.get(f"/api/voice/agents/{agent_id}/preview")
    assert v.runtime.spoken[-1] == (INTRO, None)

    asyncio.run(v.runtime.asr_lock.acquire())
    busy = v.client.get(f"/api/voice/agents/{agent_id}/preview")
    v.runtime.asr_lock.release()
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "voice_busy"


@needs_ffmpeg
def test_voice_sample_create_own_voice_consent_records_initiation_audit(v):
    """Current contract: no owner-name/checkbox fields, just the explicit action, and the server
    records who/when initiated it instead of fabricating a checked consent record."""
    agent_id = make_agent(v)["id"]
    good, frames = wav_bytes(5)
    r = v.client.post(f"/api/voice/agents/{agent_id}/voice-sample",
                      files={"sample": ("s.wav", good, "audio/wav")}, data=CONSENT)
    assert r.status_code == 200, r.text
    auth = r.json()["voice"]["authorization"]
    assert auth["consent_action"] == "create_own_voice" and auth["basis"] == "operator_provided"
    assert auth["voice_owner_name"] is None
    assert isinstance(auth["initiated_by"]["session_id"], str) and auth["initiated_by"]["session_id"]
    assert v.runtime.cloned == [(24000, frames)]


def test_voice_sample_rejects_missing_consent_signal(v):
    agent_id = make_agent(v)["id"]
    good, _ = wav_bytes(5)
    # No consent_action and no legacy authorization fields: never silently treated as authorized.
    r = v.client.post(f"/api/voice/agents/{agent_id}/voice-sample", files={"sample": ("s.wav", good, "audio/wav")})
    assert r.status_code == 400 and r.json()["error"]["code"] == "authorization_required"


@needs_ffmpeg
def test_failed_clone_preserves_previous_voice_and_retry_reuses_same_agent(v):
    agent_id = make_agent(v)["id"]
    good, frames = wav_bytes(5)
    url = f"/api/voice/agents/{agent_id}/voice-sample"

    v.runtime.fail_next_clone = True
    r = v.client.post(url, files={"sample": ("s.wav", good, "audio/wav")}, data=CONSENT)
    assert r.status_code == 503 and r.json()["error"]["code"] == "voice_clone_failed"

    agent = next(a for a in v.client.get("/api/voice/agents").json() if a["id"] == agent_id)
    assert agent["voice"]["kind"] == "default"  # previous (bundled) voice untouched by the failure
    folder = v.root / "voices" / agent_id
    assert not folder.exists() or list(folder.iterdir()) == []  # no orphaned partial state

    # Retry against the same agent id (no duplicate agent created) with the same sample succeeds.
    r = v.client.post(url, files={"sample": ("s.wav", good, "audio/wav")}, data=CONSENT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == agent_id and body["voice"]["kind"] == "cloned"
    assert v.runtime.cloned == [(24000, frames)]
    assert len(v.client.get("/api/voice/agents").json()) == 1  # still exactly one agent


# ---------- browser sessions ----------

def test_session_requires_configured_llm_and_speech(v):
    agent_id = make_agent(v)["id"]
    v.llm.configured = False
    r = v.client.post("/api/voice/browser-sessions", json={"agent_id": agent_id, "recording_consent": True})
    assert r.status_code == 503 and r.json()["error"]["code"] == "llm_unavailable"
    v.llm.configured = True
    r = v.client.post("/api/voice/browser-sessions", json={"agent_id": agent_id})
    assert r.status_code == 422  # consent must be explicit
    v.client.app.state.speech = None
    r = v.client.post("/api/voice/browser-sessions", json={"agent_id": agent_id, "recording_consent": True})
    assert r.status_code == 503 and r.json()["error"]["code"] == "speech_unavailable"
    v.client.app.state.speech = v.runtime


def test_websocket_rejects_bad_origin_and_missing_cookie(v):
    s = start_session(v, make_agent(v)["id"])
    for origin in (None, "https://evil.example"):
        headers = {"origin": origin} if origin else {}
        with pytest.raises(WebSocketDisconnect) as exc:
            with v.client.websocket_connect(s["websocket_path"], headers=headers):
                pass
        assert exc.value.code == 4403
    v.client.cookies.clear()
    with pytest.raises(WebSocketDisconnect) as exc:
        with connect(v, s["session_id"]):
            pass
    assert exc.value.code == 4401


def test_ticket_must_be_first_message_not_query(v):
    s = start_session(v, make_agent(v)["id"])
    url = f"{s['websocket_path']}?ticket={s['ticket']}"
    with v.client.websocket_connect(url, headers={"origin": ORIGIN}) as ws:
        ws.send_json({"type": "end_turn"})
        assert ws.receive_json()["code"] == "ticket_required"


def test_ticket_is_single_use_and_expires(v):
    agent_id = make_agent(v)["id"]
    s = start_session(v, agent_id)
    with connect(v, s["session_id"]) as ws:
        ws.send_json({"type": "auth", "ticket": s["ticket"]})
        until(ws, "ready")
        ws.send_json({"type": "stop"})
        until(ws, "ended")
    with connect(v, s["session_id"]) as ws:
        ws.send_json({"type": "auth", "ticket": s["ticket"]})
        assert ws.receive_json()["code"] == "invalid_ticket"

    s2 = start_session(v, agent_id)
    with v.client.app.state.sessionmaker() as db:
        db.execute(update(voice_mod.VoiceSession).values(ticket_expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
    with connect(v, s2["session_id"]) as ws:
        ws.send_json({"type": "auth", "ticket": s2["ticket"]})
        assert ws.receive_json()["code"] == "invalid_ticket"
    # A ticket only opens its own session.
    s3, s4 = start_session(v, agent_id), start_session(v, agent_id)
    with connect(v, s3["session_id"]) as ws:
        ws.send_json({"type": "auth", "ticket": s4["ticket"]})
        assert ws.receive_json()["code"] == "invalid_ticket"


def test_only_one_active_call(v):
    s = start_session(v, make_agent(v)["id"])
    asyncio.run(v.runtime.asr_lock.acquire())
    with connect(v, s["session_id"]) as ws:
        ws.send_json({"type": "auth", "ticket": s["ticket"]})
        assert ws.receive_json()["code"] == "voice_busy"
    assert v.client.get(f"/api/voice/sessions/{s['session_id']}").json()["status"] == "failed"


def test_full_turn_persists_transcript_and_never_touches_company(v):
    with v.client.app.state.sessionmaker() as db:
        company = Company(name="Acme Oy", name_normalized="acme oy")
        db.add(company)
        db.commit()
        company_id = str(company.id)
    s = start_session(v, make_agent(v)["id"], company_id=company_id)
    with connect(v, s["session_id"]) as ws:
        ws.send_json({"type": "auth", "ticket": s["ticket"]})
        ready, _ = until(ws, "ready")
        assert ready["recording"] is True and ready["input_sample_rate"] == 16000
        intro_done, seen = until(ws, "agent_done")
        audio = [m for m in seen if m["type"] == "audio"]
        assert len(audio) == 3 and audio[0]["sample_rate"] == 24000 and audio[0]["pcm"]
        ws.send_json({"type": "playback_ack", "utterance_id": intro_done["utterance_id"]})

        ws.send_bytes(pcm_frame(1600))
        ws.send_bytes(b"\x00" * 3)                    # odd length
        partial = until(ws, "partial")[0]
        assert partial["text"] == "hel"
        assert partial["committed"] == "he" and partial["tentative"] == "l"
        assert until(ws, "error")[0]["code"] == "bad_frame"
        ws.send_bytes(b"\x00\x00" * 16001)            # over the frame limit
        assert until(ws, "error")[0]["code"] == "bad_frame"
        ws.send_json({"type": "end_turn"})
        final, _ = until(ws, "final")
        assert final["text"] == "hello there"
        done, seen = until(ws, "agent_done")
        texts = [m["text"] for m in seen if m["type"] == "agent_text"]
        assert texts == ["Thanks for your time.", "I'm an AI assistant, how can I help?"]
        ws.send_json({"type": "stop"})
        assert until(ws, "ended")[0]["reason"] == "client_stop"

    assert v.runtime.fed == 1600
    system = v.llm.calls[0][0]["content"]
    assert "never claim or imply to be human" in system and "Acme Oy" in system
    assert "financial.revenue=missing" in system and "financial.ebitda=missing" in system
    assert "financial.employees=missing" in system and "owner_intent=unconfirmed" in system
    assert "First ask whether the person is willing" in system
    assert "period, currency, and reporting scope" in system
    assert "Proposed facts are unreviewed" in system and "Do not change records" in system
    detail = v.client.get(f"/api/voice/sessions/{s['session_id']}").json()
    assert detail["status"] == "ended" and detail["channel_label"] == "Browser conversation"
    assert [(t["role"], t["status"]) for t in detail["transcript"]] == [
        ("agent", "played"), ("user", "final"), ("agent", "unconfirmed")]
    assert detail["transcript"][1]["text"] == "hello there"
    assert detail["proposed_outcome"]["review_status"] == "proposed"
    with v.client.app.state.sessionmaker() as db:
        assert db.get(Company, company.id).seller_intent == SellerIntent.unknown


def test_interrupt_clears_audio_and_marks_turn(v):
    v.runtime.tts_delay, v.runtime.tts_chunks = 0.02, 20
    s = start_session(v, make_agent(v)["id"])
    with connect(v, s["session_id"]) as ws:
        ws.send_json({"type": "auth", "ticket": s["ticket"]})
        first, _ = until(ws, "audio")
        ws.send_json({"type": "interrupt"})         # receiver stays responsive mid-synthesis
        cleared, _ = until(ws, "clear")
        assert cleared["utterance_id"] == first["utterance_id"]
        ws.send_json({"type": "stop"})
        _, after = until(ws, "ended")
        assert not [m for m in after if m["type"] == "audio"]
    detail = v.client.get(f"/api/voice/sessions/{s['session_id']}").json()
    assert [(t["role"], t["status"]) for t in detail["transcript"]] == [("agent", "interrupted")]


def test_unassociated_voice_session_gets_no_company_coverage_context(v):
    s = start_session(v, make_agent(v)["id"])
    with connect(v, s["session_id"]) as ws:
        ws.send_json({"type": "auth", "ticket": s["ticket"]})
        until(ws, "ready")
        intro, _ = until(ws, "agent_done")
        ws.send_json({"type": "playback_ack", "utterance_id": intro["utterance_id"]})
        ws.send_bytes(pcm_frame())
        ws.send_json({"type": "end_turn"})
        until(ws, "final")
        until(ws, "agent_done")
        ws.send_json({"type": "stop"})
        until(ws, "ended")
    system = v.llm.calls[0][0]["content"]
    assert "Conversation context:" not in system
    assert "required-detail coverage" not in system


def test_no_transcript_without_consent_and_asr_cancelled_on_stop(v):
    s = start_session(v, make_agent(v)["id"], consent=False)
    with connect(v, s["session_id"]) as ws:
        ws.send_json({"type": "auth", "ticket": s["ticket"]})
        assert until(ws, "ready")[0]["recording"] is False
        ws.send_bytes(pcm_frame())
        until(ws, "partial")
        ws.send_json({"type": "stop"})              # mid-turn: live ASR must be cancelled
        until(ws, "ended")
    assert len(v.runtime.cancelled) == 1
    detail = v.client.get(f"/api/voice/sessions/{s['session_id']}").json()
    assert detail["transcript"] == [] and detail["proposed_outcome"] is None
    with v.client.app.state.sessionmaker() as db:
        assert db.get(voice_mod.VoiceSession, uuid.UUID(detail["id"])).transcript == []
