"""Browser voice conversations with local speech (ASR + TTS) and a text-only language model.

Routes are mounted by the parent app, which injects ``app.state.speech`` (SpeechRuntime)
and ``app.state.llm`` (LanguageModel). Nothing here sends email, places calls or changes
company records: conversation outcomes are stored as proposals for human review.
"""

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import logging
import re
import secrets
import shutil
import sys
import uuid
import wave
from array import array
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response, WebSocket
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import ForeignKey, String, Text, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from .auth import COOKIE, _hash, require_session
from .db import get_db, get_or_404
from .errors import ApiError
from .models import AuthSession, Base, Company, IdMixin, JsonType, enum_col, utcnow
from .research import required_coverage

log = logging.getLogger("permetheus.voice")

INPUT_RATE = 16_000
MAX_FRAME_SAMPLES = 16_000
MAX_TURN_SAMPLES = 30 * INPUT_RATE      # hard cap per user turn; not a VAD
MAX_QUEUED_SAMPLES = 20 * INPUT_RATE    # ASR backlog before frames are refused
MAX_TEXT_MESSAGE = 4096
AUTH_TIMEOUT = 10
TICKET_TTL = timedelta(seconds=60)
MAX_UPLOAD = 10 * 1024 * 1024
SAMPLE_RATE_CLONE = 24_000
MIN_SAMPLE_SECONDS, MAX_SAMPLE_SECONDS = 3, 30
FFMPEG_TIMEOUT = 30
REPLY_MAX_CHARS = 240
HISTORY_TURNS = 20
AI_DISCLOSURE = re.compile(r"\b(AI|A\.I\.|artificial intelligence)\b", re.IGNORECASE)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
BROWSER_LABEL = "Browser conversation"
PHONE_LABEL = "Phone calling unavailable"


def _bounded_sentence(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) > limit:
        text = text[:max(0, limit - 1)].rsplit(' ', 1)[0].rstrip(' ,;:.!?')
        return text + '.' if text else ''
    return text


DEFAULT_INTRODUCTION = (
    "Hi, I'm {name} from Mergero, an AI acquisition representative. "
    "I was wondering whether you'd be interested in selling your company?"
)

SAFETY_PROMPT = (
    "You are Mergero's acquisition outreach representative, conducting an initial business-owner conversation. "
    "Your role is professional M&A origination: understand the owner's interest, timing and conditions, "
    "and seek permission for a colleague to gather company information by email. You are not a general-purpose assistant. "
    "You are an AI: never claim or imply to be human, and confirm you are an AI whenever asked. "
    "Do not impersonate a real person or claim qualifications, a buyer mandate, a valuation or an offer you do not have. "
    "Speak in English, warmly and professionally, in at most two short natural sentences and 30 words total, with one question at a time. Then stop and listen. "
    "No lists, markdown, emojis, pushiness or repeated introductions. Listen to the answer and remember details already given. "
    "The introduction already asks about selling the company; do not repeat that question after they answer. "
    "If interested or open to exploring: acknowledge it, then ask permission for the Mergero team to email a short "
    "information request. If permission is already given, do not ask for it again. Ask for the best email address if missing, then confirm it accurately, spelling it back "
    "if unclear. Do not guess an address. Ask separately about a useful timeframe if not already given. "
    "Explain briefly when relevant that the email would request a company overview, ownership, recent revenue and "
    "profitability, and the owner's goals. Gather detailed financials over email, not an interrogation on this call. "
    "If not interested or not now, but they have not requested an end or no further contact: acknowledge without arguing "
    "and ask once whether anything in the future might change their situation. If they engage, ask separately whether "
    "Mergero should keep them in mind and when, if ever, they would welcome another conversation. Never treat a polite "
    "answer as consent to follow up. If they decline again, thank them and close. "
    "If they say stop, do not contact, remove me, or ask to end: acknowledge immediately, propose recording their "
    "no-contact preference for review, and close without another sales or future-timing question. "
    "If busy, ask once whether they want to suggest a better time; otherwise close. "
    "If they are not the owner or appropriate decision maker, ask politely for the right contact; do not assume authority. "
    "You cannot send email, place calls, change records or promise execution. Describe email and follow-up as requests "
    "for the team to review, never as sent, scheduled or completed. Do not promise confidentiality terms or a transaction. "
    "Never invent company facts, finances, interest or consent. Close by briefly recapping only agreed next steps."
)


class VoiceKind(StrEnum):
    default = "default"
    cloned = "cloned"


class VoiceSessionStatus(StrEnum):
    pending = "pending"
    active = "active"
    ended = "ended"
    failed = "failed"


class VoiceAgent(IdMixin, Base):
    __tablename__ = "voice_agents"
    name: Mapped[str] = mapped_column(String(120))
    introduction: Mapped[str] = mapped_column(Text)
    instructions: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(8), default="en")
    max_duration_seconds: Mapped[int] = mapped_column(default=300)
    voice_kind: Mapped[VoiceKind] = mapped_column(enum_col(VoiceKind), default=VoiceKind.default)
    # Relative to the speech data root; never returned by the API.
    profile_path: Mapped[str | None] = mapped_column(String(500))
    voice_authorization: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class VoiceSession(IdMixin, Base):
    __tablename__ = "voice_sessions"
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("voice_agents.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    channel: Mapped[str] = mapped_column(String(16), default="browser")
    status: Mapped[VoiceSessionStatus] = mapped_column(enum_col(VoiceSessionStatus), default=VoiceSessionStatus.pending)
    recording_consent: Mapped[bool]
    ticket_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    ticket_expires_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]
    end_reason: Mapped[str | None] = mapped_column(String(32))
    transcript: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, default=list)
    proposed_outcome: Mapped[dict[str, Any] | None] = mapped_column(JsonType)


router = APIRouter(prefix="/api/voice", tags=["voice"])


# ---------- schemas ----------

def _check_disclosure(value: str | None) -> str | None:
    if value is not None and not AI_DISCLOSURE.search(value):
        raise ValueError("Introduction must tell the other person they are speaking with an AI")
    return value


class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    introduction: str = Field(DEFAULT_INTRODUCTION, min_length=1, max_length=600)
    instructions: str = Field("", max_length=4000)
    language: Literal["en"] = "en"
    max_duration_seconds: int = Field(300, ge=30, le=900)

    @field_validator("introduction")
    @classmethod
    def disclose(cls, value):
        return _check_disclosure(value)


class AgentPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    introduction: str | None = Field(None, min_length=1, max_length=600)
    instructions: str | None = Field(None, max_length=4000)
    max_duration_seconds: int | None = Field(None, ge=30, le=900)
    # Only switching back to the bundled voice is a PATCH; cloning goes through voice-sample.
    voice: Literal["default"] | None = None

    @field_validator("introduction")
    @classmethod
    def disclose(cls, value):
        return _check_disclosure(value)


class BrowserSessionIn(BaseModel):
    agent_id: uuid.UUID
    company_id: uuid.UUID | None = None
    # Explicit, per-session choice by whoever starts this browser call (often the operator running
    # their own test): true persists the transcript for review, false never writes one. The client
    # must send its real, current value; it must never be defaulted to true to imply consent that
    # was not actually given.
    recording_consent: bool


def agent_out(agent: VoiceAgent) -> dict:
    if agent.voice_kind == VoiceKind.cloned:
        voice = {"kind": "cloned", "label": "Cloned voice", "authorization": agent.voice_authorization}
    else:
        voice = {"kind": "default", "label": "Default voice", "authorization": None}
    return {
        "id": str(agent.id), "name": agent.name, "introduction": agent.introduction,
        "instructions": agent.instructions, "language": agent.language,
        "max_duration_seconds": agent.max_duration_seconds, "voice": voice,
        "created_at": agent.created_at, "updated_at": agent.updated_at,
    }


def session_out(s: VoiceSession, *, detail: bool = False) -> dict:
    out = {
        "id": str(s.id), "agent_id": str(s.agent_id), "company_id": s.company_id and str(s.company_id),
        "channel": s.channel, "channel_label": BROWSER_LABEL, "status": s.status,
        "recording_consent": s.recording_consent, "created_at": s.created_at,
        "started_at": s.started_at, "ended_at": s.ended_at, "end_reason": s.end_reason,
    }
    if detail:
        out["transcript"] = s.transcript if s.recording_consent else []
        out["transcript_note"] = None if s.recording_consent else "Not recorded: the participant did not consent"
        out["proposed_outcome"] = s.proposed_outcome
    return out


def _runtime(request: Request):
    runtime = getattr(request.app.state, "speech", None)
    if runtime is None:
        raise ApiError(503, "speech_unavailable", "Local speech runtime is not running")
    return runtime


def _voice_path(agent: VoiceAgent) -> str | None:
    return agent.profile_path if agent.voice_kind == VoiceKind.cloned else None


@contextlib.asynccontextmanager
async def _speech_maintenance(runtime):
    """Reserve the shared speech device while changing or previewing a voice profile."""
    if runtime.asr_lock.locked():
        raise ApiError(409, "voice_busy", "A live conversation is using the speech runtime; try again after it ends")
    await runtime.asr_lock.acquire()
    try:
        yield
    finally:
        runtime.asr_lock.release()


# ---------- agents ----------

@router.get("/capabilities")
def capabilities(request: Request, _: AuthSession = Depends(require_session)):
    llm = getattr(request.app.state, "llm", None)
    runtime = getattr(request.app.state, "speech", None)
    speech = runtime is not None and all(path.is_file() for name in ("asr_binary", "asr_model", "tts_python", "tts_script") if (path := getattr(runtime, name, None)) is not None)
    return {
        "browser": {"available": speech and bool(llm and llm.configured), "label": BROWSER_LABEL,
                    "speech_runtime": speech, "language_model": bool(llm and llm.configured)},
        "phone": {"available": False, "label": PHONE_LABEL, "reason": "Telephone calling is deferred"},
    }


@router.get("/agents")
def list_agents(_: AuthSession = Depends(require_session), db: Session = Depends(get_db)):
    return [agent_out(a) for a in db.scalars(select(VoiceAgent).order_by(VoiceAgent.created_at))]


@router.post("/agents", status_code=201)
def create_agent(body: AgentIn, _: AuthSession = Depends(require_session), db: Session = Depends(get_db)):
    agent = VoiceAgent(**body.model_dump())
    db.add(agent)
    db.commit()
    return agent_out(agent)


@router.patch("/agents/{agent_id}")
def patch_agent(agent_id: uuid.UUID, body: AgentPatch, _: AuthSession = Depends(require_session),
                db: Session = Depends(get_db)):
    agent = get_or_404(db, VoiceAgent, agent_id)
    changes = body.model_dump(exclude_unset=True)
    if changes.pop("voice", None) == "default":
        agent.voice_kind = VoiceKind.default  # cloned profile is kept until replaced
    for key, value in changes.items():
        if value is None:
            raise ApiError(422, "validation_error", f"{key} cannot be null")
        setattr(agent, key, value)
    db.commit()
    return agent_out(agent)


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise ApiError(413, "too_large", f"Upload exceeds {limit // (1024 * 1024)} MB")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ApiError(413, "too_large", f"Upload exceeds {limit // (1024 * 1024)} MB")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, bytes]]:
    # ponytail: stdlib multipart parser on an already size-bounded body; avoids a new dependency.
    if not content_type.lower().startswith("multipart/form-data") or "\n" in content_type:
        raise ApiError(415, "unsupported_media_type", "Send multipart/form-data")
    message = BytesParser(policy=policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body)
    if not message.is_multipart():
        raise ApiError(400, "bad_request", "Malformed multipart body")
    fields, files = {}, {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True) or b""
        if not name:
            continue
        if part.get_filename() is not None:
            files[name] = payload
        else:
            fields[name] = payload.decode("utf-8", "replace").strip()
    return fields, files


async def _decode_sample(data: bytes) -> bytes:
    """Decode arbitrary audio to 24 kHz mono PCM16 with a bounded ffmpeg subprocess (pipes only)."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ApiError(503, "ffmpeg_unavailable", "ffmpeg is required to validate voice samples")
    # Pipe-only protocols stop container formats (playlists, concat) from reading other files or URLs.
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "pipe",
        "-i", "pipe:0", "-t", str(MAX_SAMPLE_SECONDS + 1), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE_CLONE),
        "-f", "s16le", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        pcm, err = await asyncio.wait_for(proc.communicate(data), FFMPEG_TIMEOUT)
    except BaseException as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        if isinstance(exc, TimeoutError):
            raise ApiError(422, "invalid_audio", "Audio decoding timed out") from None
        raise
    if proc.returncode != 0 or not pcm:
        log.info("voice sample rejected by ffmpeg: %s", err[-500:].decode("utf-8", "replace"))
        raise ApiError(422, "invalid_audio", "File is not decodable audio")
    seconds = len(pcm) / 2 / SAMPLE_RATE_CLONE
    if seconds > MAX_SAMPLE_SECONDS:
        raise ApiError(422, "sample_too_long", f"Voice sample must be at most {MAX_SAMPLE_SECONDS} seconds")
    if seconds < MIN_SAMPLE_SECONDS:
        raise ApiError(422, "sample_too_short", f"Voice sample must be at least {MIN_SAMPLE_SECONDS} seconds")
    return pcm


def _wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


@router.post("/agents/{agent_id}/voice-sample")
async def upload_voice_sample(agent_id: uuid.UUID, request: Request,
                              session: AuthSession = Depends(require_session),
                              db: Session = Depends(get_db)):
    """multipart: sample (file, <=10 MB, 3-30 s).

    Current clients send `consent_action=create_own_voice`: the explicit act of recording or
    uploading a sample and pressing save, paired with a short in-product notice, is the consent
    record. The server never fabricates this by defaulting a checkbox; it only accepts an explicit
    signal and stores who/when initiated it (see `initiated_by` below).

    Older clients may instead send the previous authorization form fields, still accepted so
    already-deployed clients keep working: `voice_owner_name` (1-200 chars), `owner_authorization`
    (must be exactly "true") and `authorization_basis` (`self` or `written_permission`).
    """
    agent = get_or_404(db, VoiceAgent, agent_id)
    runtime = _runtime(request)
    if runtime.asr_lock.locked():
        raise ApiError(409, "voice_busy", "A live conversation is using the speech runtime; try again after it ends")
    fields, files = _parse_multipart(request.headers.get("content-type", ""),
                                     await _read_body(request, MAX_UPLOAD + 64 * 1024))
    owner = fields.get("voice_owner_name", "").strip()
    basis = fields.get("authorization_basis")
    consent_action = fields.get("consent_action")
    if consent_action == "create_own_voice":
        # New contract: no checkbox value is invented; `basis` is informational only.
        basis = "operator_provided"
    elif fields.get("owner_authorization") == "true" and basis in ("self", "written_permission") \
            and 1 <= len(owner) <= 200:
        pass  # legacy authorization-form clients
    else:
        raise ApiError(400, "authorization_required",
                       "Cloning requires consent_action=create_own_voice, or the legacy "
                       "voice_owner_name / owner_authorization=true / authorization_basis fields")
    sample = files.get("sample")
    if not sample:
        raise ApiError(400, "sample_required", "Attach the voice sample as the 'sample' file field")
    if len(sample) > MAX_UPLOAD:
        raise ApiError(413, "too_large", "Voice sample exceeds 10 MB")
    pcm = await _decode_sample(sample)

    folder = runtime.data_root / "voices" / str(agent.id)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    stem = uuid.uuid4().hex
    source, target = folder / f"sample-{stem}.wav", folder / f"{stem}.safetensors"
    source.write_bytes(_wav(pcm, SAMPLE_RATE_CLONE))
    source.chmod(0o600)
    try:
        async with _speech_maintenance(runtime):
            await runtime.clone_voice(source, target)
            previous = agent.profile_path
            agent.profile_path = str(target.relative_to(runtime.data_root))
            agent.voice_kind = VoiceKind.cloned
            agent.voice_authorization = {
                "voice_owner_name": owner or None, "basis": basis, "authorized_at": utcnow().isoformat(),
                "sample_seconds": round(len(pcm) / 2 / SAMPLE_RATE_CLONE, 2),
                "sample_sha256": hashlib.sha256(sample).hexdigest(),
                "consent_action": consent_action or "legacy_authorization_form",
                # Initiation audit: who took the explicit action and when/from where, instead of a
                # fabricated checkbox record.
                "initiated_by": {"session_id": str(session.id),
                                 "ip": request.client.host if request.client else None},
            }
            db.commit()
            if previous and previous != agent.profile_path:
                (runtime.data_root / previous).unlink(missing_ok=True)
    except ApiError:
        target.unlink(missing_ok=True)
        raise
    except Exception:
        log.exception("voice cloning failed for agent %s", agent.id)
        target.unlink(missing_ok=True)
        raise ApiError(503, "voice_clone_failed", "Local voice cloning failed; check the speech worker logs")
    finally:
        source.unlink(missing_ok=True)  # the raw recording is not retained once cloned

    return agent_out(agent)


@router.get("/agents/{agent_id}/preview")
async def preview_agent(agent_id: uuid.UUID, request: Request, _: AuthSession = Depends(require_session),
                        db: Session = Depends(get_db)):
    """The agent's introduction as audio/wav, synthesized locally with the agent's voice."""
    agent = get_or_404(db, VoiceAgent, agent_id)
    runtime = _runtime(request)
    chunks, rate = [], None
    try:
        async with _speech_maintenance(runtime):
            async for rate, pcm in runtime.tts_stream(agent.introduction.replace("{name}", agent.name), _voice_path(agent)):
                chunks.append(pcm)
    except ApiError:
        raise
    except Exception:
        log.exception("preview synthesis failed for agent %s", agent.id)
        raise ApiError(503, "synthesis_failed", "Local speech synthesis failed; check the speech worker logs")
    if rate is None:
        raise ApiError(503, "synthesis_failed", "Local speech synthesis returned no audio")
    # ponytail: buffered WAV (a short introduction); stream chunks if previews grow long.
    return Response(_wav(b"".join(chunks), rate), media_type="audio/wav", headers={"Cache-Control": "no-store"})


# ---------- sessions ----------

@router.post("/browser-sessions", status_code=201)
def create_browser_session(body: BrowserSessionIn, request: Request, _: AuthSession = Depends(require_session),
                           db: Session = Depends(get_db)):
    get_or_404(db, VoiceAgent, body.agent_id)
    if body.company_id is not None:
        get_or_404(db, Company, body.company_id)
    _runtime(request)
    llm = getattr(request.app.state, "llm", None)
    if llm is None or not llm.configured:
        raise ApiError(503, "llm_unavailable", "Configure the language model before starting a conversation")
    ticket = secrets.token_urlsafe(32)
    session = VoiceSession(agent_id=body.agent_id, company_id=body.company_id,
                           recording_consent=body.recording_consent, ticket_hash=_hash(ticket),
                           ticket_expires_at=utcnow() + TICKET_TTL)
    db.add(session)
    db.commit()
    return {
        "session_id": str(session.id), "ticket": ticket, "expires_at": session.ticket_expires_at,
        "websocket_path": f"/api/voice/browser/{session.id}", "channel": "browser", "channel_label": BROWSER_LABEL,
        "audio_input": {"encoding": "pcm_s16le", "sample_rate": INPUT_RATE, "channels": 1,
                        "max_frame_samples": MAX_FRAME_SAMPLES},
    }


@router.get("/sessions")
def list_sessions(agent_id: uuid.UUID | None = None, _: AuthSession = Depends(require_session),
                  db: Session = Depends(get_db)):
    query = select(VoiceSession).order_by(VoiceSession.created_at.desc()).limit(200)
    if agent_id is not None:
        query = query.where(VoiceSession.agent_id == agent_id)
    return [session_out(s) for s in db.scalars(query)]


@router.get("/sessions/{session_id}")
def session_detail(session_id: uuid.UUID, _: AuthSession = Depends(require_session), db: Session = Depends(get_db)):
    return session_out(get_or_404(db, VoiceSession, session_id), detail=True)


# ---------- live conversation ----------

class Conversation:
    """One live browser call. The receiver never awaits ASR, LLM or TTS work directly."""

    END = object()

    def __init__(self, ws: WebSocket, runtime, llm, sessionmaker, session_id: uuid.UUID, agent: dict,
                 company_name: str | None, record: bool):
        self.ws, self.runtime, self.llm, self.sm = ws, runtime, llm, sessionmaker
        self.session_id, self.agent, self.company_name, self.record = session_id, agent, company_name, record
        self.send_lock = asyncio.Lock()
        self.audio: asyncio.Queue = asyncio.Queue()
        self.queued_samples = 0
        self.turns: list[dict] = []
        self.current: dict | None = None
        self.reply_task: asyncio.Task | None = None
        self.reply_tasks: set[asyncio.Task] = set()
        self.asr_id: str | None = None
        self.turn_samples = 0

    async def send(self, **message) -> None:
        async with self.send_lock:
            with contextlib.suppress(Exception):  # peer gone; the receiver notices the disconnect
                await self.ws.send_json(message)

    async def error(self, code: str, message: str, fatal: bool = False) -> None:
        await self.send(type="error", code=code, message=message, fatal=fatal)

    def persist(self, **fields) -> None:
        # ponytail: tiny synchronous writes on the event loop; move to a thread if the DB is remote/slow.
        if self.record:
            fields["transcript"] = [{k: t[k] for k in ("id", "role", "text", "status", "at")}
                                    for t in self.turns if t["text"]]
        if not fields:
            return
        with self.sm() as db:
            db.execute(update(VoiceSession).where(VoiceSession.id == self.session_id).values(**fields))
            db.commit()

    async def run(self) -> str:
        self.start_reply(self.fixed(self.agent["introduction"].replace("{name}", self.agent["name"])))
        recv = asyncio.create_task(self.receive_loop())
        asr = asyncio.create_task(self.asr_loop())
        try:
            done, _ = await asyncio.wait({recv, asr}, timeout=self.agent["max_duration_seconds"],
                                         return_when=asyncio.FIRST_COMPLETED)
            if recv in done:
                if recv.exception() is None:
                    return recv.result()
                log.error("voice receiver failed", exc_info=recv.exception())
                await self.error("server_error", "The conversation failed unexpectedly", fatal=True)
                return "error"
            if asr in done:
                log.error("local ASR failed", exc_info=asr.exception())
                await self.error("asr_failed", "Local speech recognition failed", fatal=True)
                return "speech_error"
            return "max_duration"
        finally:
            for task in (recv, asr):
                task.cancel()
            await asyncio.gather(recv, asr, return_exceptions=True)
            await self.stop_reply()
            if self.asr_id is not None:
                with contextlib.suppress(Exception):
                    await self.runtime.asr_cancel(self.asr_id)
                self.asr_id = None

    async def receive_loop(self) -> str:
        while True:
            message = await self.ws.receive()
            if message["type"] == "websocket.disconnect":
                return "disconnect"
            data = message.get("bytes")
            if data is not None:
                if not data or len(data) % 2 or len(data) > MAX_FRAME_SAMPLES * 2:
                    await self.error("bad_frame", f"Audio frames must be PCM16 mono with 1-{MAX_FRAME_SAMPLES} samples")
                    continue
                if self.queued_samples + len(data) // 2 > MAX_QUEUED_SAMPLES:
                    await self.error("audio_overflow", "Speech recognition is behind; frame dropped")
                    continue
                pcm = array("h", data)
                if sys.byteorder == "big":
                    pcm.byteswap()
                self.queued_samples += len(pcm)
                self.audio.put_nowait([s / 32768 for s in pcm])
                continue
            text = message.get("text") or ""
            try:
                event = json.loads(text) if len(text) <= MAX_TEXT_MESSAGE else None
            except ValueError:
                event = None
            kind = event.get("type") if isinstance(event, dict) else None
            if kind == "end_turn":
                self.audio.put_nowait(self.END)
            elif kind == "interrupt":
                await self.interrupt()
            elif kind == "playback_ack":
                self.acknowledge(event.get("utterance_id"))
            elif kind == "stop":
                return "client_stop"
            else:
                await self.error("bad_message", "Expected end_turn, interrupt, playback_ack or stop")

    async def asr_loop(self) -> None:
        while True:
            item = await self.audio.get()
            if item is self.END:
                await self.finish_turn()
                continue
            self.queued_samples -= len(item)
            if self.asr_id is None:
                self.asr_id, self.turn_samples = uuid.uuid4().hex, 0
                await self.runtime.asr_start(self.asr_id)
            self.turn_samples += len(item)
            result = await self.runtime.asr_feed(self.asr_id, item)
            committed, tentative = result.get("committed", ""), result.get("tentative", "")
            if not isinstance(committed, str) or not isinstance(tentative, str):
                raise RuntimeError("ASR worker returned invalid committed or tentative text")
            # Keep `text` as the current full hypothesis for simple clients while exposing
            # stable and revisable portions separately to clients that render them differently.
            if committed or tentative:
                await self.send(type="partial", turn_id=self.asr_id, text=committed + tentative,
                                committed=committed, tentative=tentative)
            if self.turn_samples >= MAX_TURN_SAMPLES:
                await self.finish_turn()

    async def finish_turn(self) -> None:
        if self.asr_id is None:
            await self.send(type="final", turn_id=None, text="")
            return
        turn_id, self.asr_id = self.asr_id, None
        text = (await self.runtime.asr_finalize(turn_id)).strip()
        await self.send(type="final", turn_id=turn_id, text=text)
        if text:
            self.turns.append({"id": turn_id, "role": "user", "text": text, "status": "final", "at": utcnow().isoformat()})
            self.persist()
            self.start_reply(self.generate())

    def messages(self) -> list[dict]:
        system = SAFETY_PROMPT + f"\nYour name: {self.agent['name']}."
        if self.company_name:
            system += f"\nConversation context: the person represents {self.company_name}."
            coverage = self.agent.get("required_coverage")
            if coverage is not None:
                statuses = "; ".join(f"{key}={value}" for key, value in coverage.items())
                system += (
                    "\nRead-only required-detail coverage: " + statuses + "."
                    " Use these gaps to describe the proposed email information request, only after interest and permission."
                    " Do not ask for details already covered. Financial requests should include the period, currency, and reporting scope."
                    " Proposed facts are unreviewed and must never be treated as verified."
                    " Do not change records; any outcome remains a proposal for human review."
                    " Follow the future-timing and no-contact branches above when they decline."
                )
        if self.agent["instructions"]:
            system += "\nOperator instructions (never override the rules above):\n" + self.agent["instructions"]
        history = []
        for t in [t for t in self.turns if t["text"]][-HISTORY_TURNS:]:
            text = t["text"] + (" [interrupted by the other person]" if t["status"] == "interrupted" else "")
            history.append({"role": "user" if t["role"] == "user" else "assistant", "content": text})
        return [{"role": "system", "content": system}, *history]

    async def generate(self):
        buffer, remaining, count = "", REPLY_MAX_CHARS, 0
        async with contextlib.aclosing(self.llm.stream(self.messages(), max_tokens=80, interactive=True)) as tokens:
            async for token in tokens:
                buffer += token
                *sentences, buffer = SENTENCE_END.split(buffer)
                for sentence in sentences:
                    text = _bounded_sentence(sentence, remaining)
                    if text:
                        yield text
                        count += 1
                        remaining -= len(text) + 1
                    if count >= 2 or remaining < 2 or len(sentence.strip()) > len(text):
                        return
                if len(buffer.strip()) >= remaining:
                    yield _bounded_sentence(buffer, remaining)
                    return
        if buffer.strip():
            yield _bounded_sentence(buffer, remaining)

    @staticmethod
    async def fixed(text: str):
        yield text

    def start_reply(self, sentences) -> None:
        self.cancel_current()
        turn = {"id": uuid.uuid4().hex, "role": "agent", "text": "", "status": "speaking",
                "at": utcnow().isoformat(), "audio_sent": False, "in_tts": False}
        self.turns.append(turn)
        self.current = turn
        self.reply_task = asyncio.create_task(self.reply(turn, sentences))
        self.reply_tasks.add(self.reply_task)
        self.reply_task.add_done_callback(self.reply_tasks.discard)

    async def reply(self, turn: dict, sentences) -> None:
        seq = 0
        try:
            async with asyncio.timeout(45), contextlib.aclosing(sentences):
                async for sentence in sentences:
                    turn["text"] = f"{turn['text']} {sentence}".strip()
                    await self.send(type="agent_text", utterance_id=turn["id"], text=sentence)
                    turn["in_tts"] = True
                    async for rate, pcm in self.runtime.tts_stream(sentence, self.agent["voice_path"]):
                        if turn["status"] != "speaking":
                            continue  # drain: cancelling mid-synthesis would restart the TTS worker
                        await self.send(type="audio", utterance_id=turn["id"], seq=seq, sample_rate=rate,
                                        pcm=base64.b64encode(pcm).decode())
                        seq += 1
                        turn["audio_sent"] = True
                    turn["in_tts"] = False
                    if turn["status"] != "speaking":
                        return
            if turn["status"] == "speaking":
                turn["status"] = "awaiting_playback" if turn["audio_sent"] else "unplayed"
                await self.send(type="agent_done", utterance_id=turn["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("voice reply failed")
            if turn["status"] == "speaking":
                turn["status"] = "failed"
            await self.error("reply_failed", "The assistant could not reply; the conversation continues")
        finally:
            turn["in_tts"] = False
            self.persist()

    def cancel_current(self) -> dict | None:
        turn, task = self.current, self.reply_task
        if turn is None or turn["status"] not in ("speaking", "awaiting_playback"):
            return None
        turn["status"] = "interrupted" if turn["audio_sent"] else "unplayed"
        if task is not None and not task.done() and not turn["in_tts"]:
            task.cancel()  # safe outside synthesis (e.g. waiting on the language model)
        elif task is not None and not task.done():
            # Let a short synthesis finish to retain the warm worker, but never queue behind a stuck one.
            timer = asyncio.get_running_loop().call_later(2, task.cancel)
            task.add_done_callback(lambda _: timer.cancel())
        return turn

    async def interrupt(self) -> None:
        turn = self.cancel_current()
        if turn is not None:
            await self.send(type="clear", utterance_id=turn["id"])
            self.persist()

    def acknowledge(self, utterance_id) -> None:
        for turn in self.turns:
            if turn["id"] == utterance_id and turn["status"] == "awaiting_playback":
                turn["status"] = "played"
                self.persist()

    async def stop_reply(self) -> None:
        for turn in self.turns:
            if turn["status"] == "speaking":
                turn["status"] = "interrupted" if turn["audio_sent"] else "unplayed"
        tasks = list(self.reply_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for t in self.turns:
            if t["status"] == "awaiting_playback":
                t["status"] = "unconfirmed"  # audio sent, playback never acknowledged


async def _propose_outcome(llm, turns: list[dict]) -> dict | None:
    lines = [f"{'Person' if t['role'] == 'user' else 'Mergero AI representative'}: {t['text']}" for t in turns if t["text"]]
    if not any(t["role"] == "user" and t["text"] for t in turns):
        return None
    try:
        value = await asyncio.wait_for(llm.extract("\n".join(lines), (
            'Keys: "summary" (two sentences), "stated_interest" (one of unknown, interested, conditional, '
            'not_now, not_interested; only what the person explicitly said), "follow_ups" (list of short strings). '
            'Include explicitly stated email permission and address, timing, future change triggers, keep-in-mind '
            'permission or refusal, and any no-contact request in the summary and follow_ups. Missing consent '
            'is unknown, never granted. A no-contact request overrides follow-up suggestions. Do not claim '
            'any email was sent, follow-up scheduled, or company record updated.'
        )), 45)
    except Exception:
        log.warning("outcome proposal failed", exc_info=True)
        return None
    interest = value.get("stated_interest")
    follow_ups = value.get("follow_ups") if isinstance(value.get("follow_ups"), list) else []
    return {
        "review_status": "proposed", "source": "model",
        "note": "Model proposal from the transcript; not applied to company records until a human reviews it",
        "summary": str(value.get("summary") or "")[:1000],
        "stated_interest": interest if interest in ("unknown", "interested", "conditional", "not_now",
                                                    "not_interested") else "unknown",
        "follow_ups": [str(f)[:300] for f in follow_ups[:10]],
    }


async def _close(ws: WebSocket, code: int, error: str | None = None, message: str = "") -> None:
    with contextlib.suppress(Exception):
        if error:
            await ws.send_json({"type": "error", "code": error, "message": message, "fatal": True})
        await ws.close(code)


@router.websocket("/browser/{session_id}")
async def browser_conversation(ws: WebSocket, session_id: uuid.UUID):
    app = ws.app
    origin = ws.headers.get("origin")
    if origin is None or origin.rstrip("/") not in app.state.settings.allowed_origins:
        await ws.close(4403)  # before accept: the browser sees a rejected handshake
        return
    token = ws.cookies.get(COOKIE)
    with app.state.sessionmaker() as db:
        authed = token and db.scalar(select(AuthSession.id).where(
            AuthSession.token_hash == _hash(token), AuthSession.expires_at > utcnow()))
    if not authed:
        await ws.close(4401)
        return
    await ws.accept()
    try:
        first = await asyncio.wait_for(ws.receive(), AUTH_TIMEOUT)
        event = json.loads(first.get("text") or "null")
        ticket = event.get("ticket") if isinstance(event, dict) and event.get("type") == "auth" else None
    except (TimeoutError, ValueError, RuntimeError):
        ticket = None
    if not isinstance(ticket, str) or not 0 < len(ticket) <= 256:
        return await _close(ws, 4401, "ticket_required", 'First message must be {"type":"auth","ticket":"..."}')

    sm = app.state.sessionmaker
    with sm() as db:
        consumed = db.execute(update(VoiceSession).where(
            VoiceSession.id == session_id, VoiceSession.ticket_hash == _hash(ticket),
            VoiceSession.status == VoiceSessionStatus.pending, VoiceSession.ticket_expires_at > utcnow(),
        ).values(ticket_hash=None)).rowcount  # single use: the hash is cleared atomically
        db.commit()
        session = db.get(VoiceSession, session_id) if consumed else None
        agent = session and db.get(VoiceAgent, session.agent_id)
        company = session and session.company_id and db.get(Company, session.company_id)
        agent_snapshot = agent and {
            "name": agent.name, "introduction": agent.introduction, "instructions": agent.instructions,
            "max_duration_seconds": agent.max_duration_seconds, "voice_path": _voice_path(agent)}
        if agent_snapshot is not None and company is not None:
            agent_snapshot["required_coverage"] = required_coverage(db, company.id)
        record = bool(session and session.recording_consent)
    if not consumed or agent_snapshot is None:
        return await _close(ws, 4401, "invalid_ticket", "Ticket is invalid, expired or already used")

    def fail(reason: str) -> None:
        with sm() as db:
            db.execute(update(VoiceSession).where(VoiceSession.id == session_id).values(
                status=VoiceSessionStatus.failed, end_reason=reason, ended_at=utcnow()))
            db.commit()

    runtime, llm = getattr(app.state, "speech", None), getattr(app.state, "llm", None)
    if runtime is None or llm is None or not llm.configured:
        fail("unavailable")
        return await _close(ws, 1011, "voice_unavailable", "Local speech or the language model is not available")
    if runtime.asr_lock.locked():
        fail("busy")
        return await _close(ws, 4409, "voice_busy", "Another conversation is active; only one call runs at a time")

    async with runtime.asr_lock:
        conversation = Conversation(ws, runtime, llm, sm, session_id, agent_snapshot,
                                    company.name if company else None, record)
        conversation.persist(status=VoiceSessionStatus.active, started_at=utcnow())
        await conversation.send(type="ready", session_id=str(session_id), channel="browser",
                                channel_label=BROWSER_LABEL, input_sample_rate=INPUT_RATE,
                                max_frame_samples=MAX_FRAME_SAMPLES, recording=record,
                                max_duration_seconds=agent_snapshot["max_duration_seconds"])
        reason = "error"
        try:
            reason = await conversation.run()
        finally:
            conversation.persist(status=VoiceSessionStatus.ended, ended_at=utcnow(), end_reason=reason)
    await conversation.send(type="ended", reason=reason)
    await _close(ws, 1000)
    if record:
        outcome = await _propose_outcome(llm, conversation.turns)
        if outcome:
            conversation.persist(proposed_outcome=outcome)
