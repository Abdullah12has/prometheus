"""Private, recoverable recording notes with local transcription."""

import asyncio
import hashlib
import os
import re
import shutil
import tempfile
import uuid
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .auth import require_session
from .db import get_db, get_or_404
from .errors import ApiError
from .models import Base, Company, IdMixin, Job, JobState, JsonType, utcnow

router = APIRouter(prefix="/api", tags=["notes"], dependencies=[Depends(require_session)])

MAX_CHUNK_BYTES = 8 * 1024 * 1024
MAX_NOTE_BYTES = 1024 * 1024 * 1024
MAX_DURATION_SECONDS = 4 * 60 * 60
CHUNK_SECONDS = 30
MAX_NORMALIZED_CHUNKS = MAX_DURATION_SECONDS // CHUNK_SECONDS
MAX_UPLOAD_CHUNKS = MAX_DURATION_SECONDS // 3 + 2
MIME_EXTENSIONS = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
}


class Note(IdMixin, Base):
    __tablename__ = "notes"

    title: Mapped[str] = mapped_column(String(300))
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="uploading", index=True)
    mime_type: Mapped[str | None] = mapped_column(String(64))
    chunk_count: Mapped[int | None] = mapped_column(Integer)
    audio_sha256: Mapped[str | None] = mapped_column(String(64))
    duration_seconds: Mapped[float | None]
    transcript_raw: Mapped[str | None] = mapped_column(Text)
    transcript_corrected: Mapped[str | None] = mapped_column(Text)
    transcript_segments: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, default=list)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    processing_error: Mapped[str | None] = mapped_column(String(300))
    summary_warning: Mapped[str | None] = mapped_column(String(300))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class NoteChunk(IdMixin, Base):
    __tablename__ = "note_chunks"
    __table_args__ = (UniqueConstraint("note_id", "sequence"),)

    note_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(Integer)
    mime_type: Mapped[str] = mapped_column(String(64))


class NoteCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    company_id: uuid.UUID | None = None


class FinalizeIn(BaseModel):
    chunk_count: int = Field(ge=1, le=MAX_UPLOAD_CHUNKS)
    mime_type: str = Field(min_length=1, max_length=64)


class NotePatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=300)
    transcript_corrected: str | None = Field(None, max_length=1_000_000)


def _data_root(request: Request) -> Path:
    settings = request.app.state.settings
    configured = getattr(settings, "data_dir", None)
    if configured is None and hasattr(request.app.state, "speech"):
        configured = request.app.state.speech.data_root
    return Path(configured).resolve() if configured is not None else Path(__file__).resolve().parents[2] / "data"


def _note_dir(data_root: Path, note_id: uuid.UUID) -> Path:
    root = (data_root / "notes").resolve()
    path = (root / str(note_id)).resolve()
    if not path.is_relative_to(root):
        raise ApiError(500, "storage_error", "Note storage path is invalid")
    return path


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _prepare_note_dir(data_root: Path, note_id: uuid.UUID) -> Path:
    _private_dir(data_root)
    _private_dir(data_root / "notes")
    directory = _note_dir(data_root, note_id)
    _private_dir(directory)
    _private_dir(directory / "chunks")
    return directory


def _blob_path(directory: Path, sequence: int, digest: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ApiError(500, "storage_error", "Stored chunk digest is invalid")
    path = (directory / "chunks" / f"{sequence}-{digest}.bin").resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ApiError(500, "storage_error", "Stored chunk path is invalid")
    return path


def _note_out(note: Note, chunks: list[NoteChunk] | None = None) -> dict[str, Any]:
    result = {
        "id": str(note.id),
        "title": note.title,
        "company_id": str(note.company_id) if note.company_id else None,
        "status": note.status,
        "mime_type": note.mime_type,
        "chunk_count": note.chunk_count,
        "received_sequences": [c.sequence for c in chunks] if chunks is not None else None,
        "audio_sha256": note.audio_sha256,
        "duration_seconds": note.duration_seconds,
        "transcript_raw": note.transcript_raw,
        "transcript": note.transcript_corrected if note.transcript_corrected is not None else note.transcript_raw,
        "transcript_corrected": note.transcript_corrected,
        "transcript_segments": note.transcript_segments,
        "summary": note.summary,
        "progress": note.progress,
        "processing_error": note.processing_error,
        "summary_warning": note.summary_warning,
        "created_at": note.created_at.isoformat(),
        "updated_at": note.updated_at.isoformat(),
    }
    if chunks is not None:
        result["chunks"] = [
            {"sequence": c.sequence, "sha256": c.sha256, "bytes": c.byte_size} for c in chunks
        ]
    return result


@router.post("/notes", status_code=201)
def create_note(body: NoteCreate, request: Request, db: Session = Depends(get_db)):
    title = body.title.strip()
    if not title:
        raise ApiError(422, "validation_error", "Title cannot be blank")
    if body.company_id is not None and db.get(Company, body.company_id) is None:
        raise ApiError(404, "not_found", "Company not found")
    note = Note(title=title, company_id=body.company_id)
    db.add(note)
    db.commit()
    _prepare_note_dir(_data_root(request), note.id)
    return _note_out(note, [])


@router.get("/notes")
def list_notes(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    notes = db.scalars(select(Note).order_by(Note.created_at.desc()).limit(limit)).all()
    return [_note_out(note) for note in notes]


@router.get("/notes/{note_id}")
def get_note(note_id: uuid.UUID, db: Session = Depends(get_db)):
    note = get_or_404(db, Note, note_id)
    chunks = db.scalars(
        select(NoteChunk).where(NoteChunk.note_id == note_id).order_by(NoteChunk.sequence)
    ).all()
    return _note_out(note, chunks)


@router.put("/notes/{note_id}/chunks/{sequence}")
async def upload_chunk(
    note_id: uuid.UUID, sequence: int, request: Request, db: Session = Depends(get_db)
):
    note = get_or_404(db, Note, note_id)
    if note.status != "uploading":
        raise ApiError(409, "conflict", "Note recording is no longer accepting chunks")
    if sequence < 0 or sequence >= MAX_UPLOAD_CHUNKS:
        raise ApiError(422, "validation_error", "Chunk sequence is outside the supported range")
    mime_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if mime_type not in MIME_EXTENSIONS:
        raise ApiError(415, "unsupported_media_type", "Unsupported audio type")
    header_length = request.headers.get("content-length")
    if header_length:
        try:
            declared = int(header_length)
        except ValueError as exc:
            raise ApiError(400, "bad_request", "Invalid Content-Length") from exc
        if declared < 0 or declared > MAX_CHUNK_BYTES:
            raise ApiError(413, "chunk_too_large", "Audio chunks are limited to 8 MiB")
    else:
        declared = None

    data_root = _data_root(request)
    directory = _prepare_note_dir(data_root, note_id)
    chunks_dir = directory / "chunks"
    existing = db.scalar(
        select(NoteChunk).where(NoteChunk.note_id == note_id, NoteChunk.sequence == sequence)
    )
    current_bytes = db.scalar(
        select(func.coalesce(func.sum(NoteChunk.byte_size), 0)).where(NoteChunk.note_id == note_id)
    ) or 0
    if existing is not None:
        current_bytes -= existing.byte_size
    available = shutil.disk_usage(data_root).free
    if declared is not None and current_bytes + declared > MAX_NOTE_BYTES:
        raise ApiError(413, "storage_limit", "Recording exceeds local storage limits")
    if declared is not None and available < declared:
        raise ApiError(507, "insufficient_storage", "Not enough local disk space")

    fd, tmp_name = tempfile.mkstemp(prefix="upload-", suffix=".tmp", dir=chunks_dir)
    tmp = Path(tmp_name)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as stream:
            async for part in request.stream():
                size += len(part)
                if size > MAX_CHUNK_BYTES or current_bytes + size > MAX_NOTE_BYTES:
                    raise ApiError(413, "storage_limit", "Recording exceeds local storage limits")
                if available < size:
                    raise ApiError(507, "insufficient_storage", "Not enough local disk space")
                digest.update(part)
                stream.write(part)
            stream.flush()
            os.fsync(stream.fileno())
        if size == 0:
            raise ApiError(400, "bad_request", "Audio chunk is empty")
        if declared is not None and declared != size:
            raise ApiError(400, "bad_request", "Chunk size does not match Content-Length")
        sha256 = digest.hexdigest()
        locked_note = db.scalar(select(Note).where(Note.id == note_id).with_for_update().execution_options(populate_existing=True))
        if locked_note is None or locked_note.status != "uploading":
            raise ApiError(409, "conflict", "Note recording is no longer accepting chunks")
        existing = db.scalar(
            select(NoteChunk).where(NoteChunk.note_id == note_id, NoteChunk.sequence == sequence)
        )
        current_bytes = db.scalar(
            select(func.coalesce(func.sum(NoteChunk.byte_size), 0)).where(NoteChunk.note_id == note_id)
        ) or 0
        if existing is not None:
            current_bytes -= existing.byte_size
        if current_bytes + size > MAX_NOTE_BYTES:
            raise ApiError(413, "storage_limit", "Recording exceeds local storage limits")
        if existing is not None:
            if existing.sha256 != sha256 or existing.mime_type != mime_type:
                raise ApiError(409, "chunk_conflict", "A different chunk already uses this sequence")
            target = _blob_path(directory, sequence, sha256)
            if not target.is_file():
                os.replace(tmp, target)
            return {"sequence": sequence, "sha256": sha256, "idempotent": True}

        target = _blob_path(directory, sequence, sha256)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.parent.chmod(0o700)
        if target.exists():
            tmp.unlink(missing_ok=True)
        else:
            os.replace(tmp, target)
        db.add(NoteChunk(note_id=note_id, sequence=sequence, sha256=sha256, byte_size=size, mime_type=mime_type))
        note.updated_at = utcnow()
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            winner = db.scalar(
                select(NoteChunk).where(NoteChunk.note_id == note_id, NoteChunk.sequence == sequence)
            )
            if winner is None or winner.sha256 != sha256 or winner.mime_type != mime_type:
                if target.exists() and (winner is None or winner.sha256 != sha256):
                    target.unlink(missing_ok=True)
                raise ApiError(409, "chunk_conflict", "A different chunk already uses this sequence")
            return {"sequence": sequence, "sha256": sha256, "idempotent": True}
        return {"sequence": sequence, "sha256": sha256, "idempotent": False}
    finally:
        tmp.unlink(missing_ok=True)


@router.post("/notes/{note_id}/finalize", status_code=202)
def finalize_note(note_id: uuid.UUID, body: FinalizeIn, request: Request, db: Session = Depends(get_db)):
    note = db.scalar(select(Note).where(Note.id == note_id).with_for_update().execution_options(populate_existing=True))
    if note is None:
        raise ApiError(404, "not_found", "Note not found")
    if note.status != "uploading":
        return _note_out(note)
    mime_type = body.mime_type.split(";", 1)[0].strip().lower()
    if mime_type not in MIME_EXTENSIONS:
        raise ApiError(415, "unsupported_media_type", "Unsupported audio type")
    chunks = db.scalars(
        select(NoteChunk).where(NoteChunk.note_id == note_id).order_by(NoteChunk.sequence)
    ).all()
    if len(chunks) != body.chunk_count or [c.sequence for c in chunks] != list(range(body.chunk_count)):
        raise ApiError(409, "chunks_incomplete", "Chunks must be uploaded contiguously before finalization")
    if any(c.mime_type != mime_type for c in chunks):
        raise ApiError(409, "mime_type_mismatch", "Chunk media types do not match the recording type")
    total_bytes = sum(c.byte_size for c in chunks)
    if total_bytes > MAX_NOTE_BYTES:
        raise ApiError(413, "storage_limit", "Recording exceeds local storage limits")
    data_root = _data_root(request)
    if shutil.disk_usage(data_root).free < total_bytes:
        raise ApiError(507, "insufficient_storage", "Not enough local disk space to assemble recording")
    directory = _prepare_note_dir(data_root, note_id)
    target = directory / f"recording{MIME_EXTENSIONS[mime_type]}"
    fd, temporary_name = tempfile.mkstemp(prefix="assemble-", suffix=".tmp", dir=directory)
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as output:
            for chunk in chunks:
                source = _blob_path(directory, chunk.sequence, chunk.sha256)
                if not source.is_file():
                    raise ApiError(409, "chunk_missing", "An uploaded audio chunk is missing")
                piece_hash = hashlib.sha256()
                with source.open("rb") as input_file:
                    while block := input_file.read(1024 * 1024):
                        piece_hash.update(block)
                        digest.update(block)
                        output.write(block)
                if piece_hash.hexdigest() != chunk.sha256:
                    raise ApiError(409, "chunk_corrupt", "An uploaded audio chunk failed its integrity check")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)

    note.status = "queued"
    note.mime_type = mime_type
    note.chunk_count = body.chunk_count
    note.audio_sha256 = digest.hexdigest()
    note.processing_error = None
    note.progress = 0
    note.updated_at = utcnow()
    job_key = f"note.transcribe:{note_id}"
    if db.scalar(select(Job.id).where(Job.idempotency_key == job_key)) is None:
        db.add(Job(kind="note.transcribe", payload={"note_id": str(note_id)}, company_id=note.company_id,
                   idempotency_key=job_key, state=JobState.queued))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        current = get_or_404(db, Note, note_id)
        return _note_out(current)
    return _note_out(note)


@router.get("/notes/{note_id}/audio")
def get_note_audio(note_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    note = get_or_404(db, Note, note_id)
    if note.status == "uploading" or not note.mime_type:
        raise ApiError(409, "not_ready", "Recording has not been finalized")
    path = _note_dir(_data_root(request), note_id) / f"recording{MIME_EXTENSIONS[note.mime_type]}"
    if not path.is_file():
        raise ApiError(404, "not_found", "Recording file not found")
    return FileResponse(path, media_type=note.mime_type, filename=f"note-{note_id}{path.suffix}")


@router.patch("/notes/{note_id}")
def patch_note(note_id: uuid.UUID, body: NotePatch, db: Session = Depends(get_db)):
    note = get_or_404(db, Note, note_id)
    changes = body.model_fields_set
    if "title" in changes:
        title = (body.title or "").strip()
        if not title:
            raise ApiError(422, "validation_error", "Title cannot be blank")
        note.title = title
    if "transcript_corrected" in changes:
        note.transcript_corrected = body.transcript_corrected
    note.updated_at = utcnow()
    db.commit()
    return _note_out(note)


@router.delete("/notes/{note_id}", status_code=204)
def delete_note(note_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    note = get_or_404(db, Note, note_id)
    directory = _note_dir(_data_root(request), note_id)
    db.execute(
        update(Job).where(Job.idempotency_key == f"note.transcribe:{note_id}",
                          Job.state.in_([JobState.queued, JobState.running]))
        .values(state=JobState.cancelled, updated_at=utcnow())
    )
    db.delete(note)
    db.commit()
    shutil.rmtree(directory, ignore_errors=True)
    return Response(status_code=204)


async def _run_command(*args: str, timeout: float, capture_stdout: bool = False) -> tuple[bytes, bytes]:
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE if capture_stdout else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError as exc:
        if process is not None:
            process.kill()
            await process.wait()
        raise RuntimeError("Audio processing timed out") from exc
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode != 0:
        raise RuntimeError("Audio processing failed")
    return stdout or b"", stderr or b""


async def _normalize_audio(source: Path, destination: Path) -> tuple[float, list[Path]]:
    ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        raise RuntimeError("Local ffmpeg tools are unavailable")
    demuxers = {".webm": "webm", ".ogg": "ogg", ".wav": "wav", ".mp3": "mp3",
                ".m4a": "mov", ".aac": "aac", ".flac": "flac"}
    demuxer = demuxers.get(source.suffix.lower())
    if demuxer is None:
        raise RuntimeError("Unsupported audio container")
    stdout, _ = await _run_command(
        ffprobe, "-protocol_whitelist", "file,pipe", "-f", demuxer, "-v", "error",
        "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
        str(source), timeout=60, capture_stdout=True,
    )
    known_duration = None
    try:
        candidate = float(stdout.strip())
        if candidate > 0:
            known_duration = candidate
    except ValueError:
        pass
    if known_duration is not None and known_duration > MAX_DURATION_SECONDS:
        raise RuntimeError("Recording duration exceeds four hours or is invalid")
    reserve_seconds = known_duration if known_duration is not None else MAX_DURATION_SECONDS
    if shutil.disk_usage(destination.parent).free < min(reserve_seconds, MAX_DURATION_SECONDS) * 32_000:
        raise RuntimeError("Not enough local disk space for normalized audio")
    shutil.rmtree(destination, ignore_errors=True)
    _private_dir(destination)
    output = destination / "part-%05d.wav"
    await _run_command(
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "file,pipe",
        "-f", demuxer, "-i", str(source), "-t", str(MAX_DURATION_SECONDS + 1),
        "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        "-f", "segment", "-segment_time", str(CHUNK_SECONDS), "-reset_timestamps", "1",
        str(output), timeout=3600,
    )
    segments = sorted(destination.glob("part-*.wav"))
    if not segments or len(segments) > MAX_NORMALIZED_CHUNKS:
        raise RuntimeError("Audio normalization produced an invalid number of chunks")
    for segment in segments:
        segment.chmod(0o600)
    actual_duration = 0.0
    for segment in segments:
        with wave.open(str(segment), "rb") as wav:
            actual_duration += wav.getnframes() / wav.getframerate()
    if actual_duration <= 0 or actual_duration > MAX_DURATION_SECONDS:
        raise RuntimeError("Recording duration exceeds four hours or is invalid")
    if known_duration is not None and abs(known_duration - actual_duration) > max(5.0, actual_duration * 0.02):
        raise RuntimeError("Audio duration did not match decoded content")
    return actual_duration, segments


def _clean_proposals(value: dict[str, Any], transcript: str) -> dict[str, Any]:
    summary = value.get("summary")
    if not isinstance(summary, str):
        summary = ""
    valid = transcript.casefold()
    output: dict[str, Any] = {"summary": summary[:10_000], "facts": [], "actions": []}
    for key in ("facts", "actions"):
        rows = value.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows[:100]:
            if not isinstance(row, dict):
                continue
            text, quote = row.get("text"), row.get("quote")
            if (isinstance(text, str) and text.strip() and len(text) <= 2000
                    and isinstance(quote, str) and quote.strip() and len(quote) <= 1000
                    and quote.casefold() in valid):
                output[key].append({"text": text.strip(), "quote": quote, "status": "proposed"})
    return output


def _note_exists(sessionmaker, note_id: uuid.UUID, job_guard=None) -> bool:
    with sessionmaker() as db:
        exists = db.get(Note, note_id) is not None
        return exists and (job_guard is None or bool(job_guard()))


async def process_note(note_id: uuid.UUID, sessionmaker, runtime, llm, job_guard=None) -> None:
    """Worker entry point for a persistent ``note.transcribe`` job."""
    data_root = Path(runtime.data_root).resolve()
    directory = _prepare_note_dir(data_root, note_id)
    with sessionmaker() as db:
        note = db.get(Note, note_id)
        if note is None:
            raise LookupError("Note no longer exists")
        if note.status == "completed":
            return
        note.status = "processing"
        note.progress = 0
        note.processing_error = None
        note.summary_warning = None
        previous_segments = list(note.transcript_segments or [])
        previous_raw = note.transcript_raw
        note.summary = None
        db.commit()

    try:
        duration, wav_chunks = await _normalize_audio(
            _audio_path(sessionmaker, note_id, directory), directory / "processed"
        )
        segments = list(previous_segments)
        elapsed = sum(max(0.0, float(s.get("end_sec", 0)) - float(s.get("start_sec", 0))) for s in segments)
        start_index = len(segments)
        if previous_raw and not segments:
            raise RuntimeError("Existing transcript recovery segments are inconsistent")
        for index, path in enumerate(wav_chunks):
            if not _note_exists(sessionmaker, note_id, job_guard):
                return
            if index < start_index:
                continue
            text = await runtime.transcribe(path)
            if job_guard is not None and not job_guard():
                return
            with wave.open(str(path), "rb") as wav:
                segment_duration = wav.getnframes() / wav.getframerate()
            start = elapsed
            end = min(duration, start + segment_duration)
            segments.append({"start_sec": start, "end_sec": end, "text": text})
            elapsed = end
            transcript = " ".join(s["text"].strip() for s in segments if s["text"].strip())
            with sessionmaker() as db:
                note = db.get(Note, note_id)
                if note is None:
                    return
                note.transcript_segments = segments
                note.transcript_raw = transcript
                note.progress = min(90, int((index + 1) * 90 / len(wav_chunks)))
                note.updated_at = utcnow()
                db.commit()

        transcript = " ".join(s["text"].strip() for s in segments if s["text"].strip())
        if job_guard is not None and not job_guard():
            return
        summary = None
        summary_warning = None
        if llm is not None:
            instruction = (
                "Return JSON with summary (string), facts (array of {text, quote}), and actions "
                "(array of {text, quote}). Every quote must be an exact passage from the transcript. "
                "Treat every fact and action as unverified proposals. Never infer willingness to sell, "
                "change company intent, or treat a suggestion as approved."
            )
            try:
                batch_chars = 40_000
                batches = [transcript[i:i + batch_chars] for i in range(0, len(transcript), batch_chars)] or [""]
                summaries = []
                facts, actions = [], []
                covered = 0
                for batch_index, batch in enumerate(batches):
                    try:
                        extracted = _clean_proposals(await llm.extract(batch, instruction), batch)
                    except Exception:
                        continue
                    covered += 1
                    if extracted["summary"]:
                        summaries.append(extracted["summary"])
                    facts.extend(extracted["facts"])
                    actions.extend(extracted["actions"])
                summary = {"summary": " ".join(summaries)[:10_000], "facts": facts[:100], "actions": actions[:100]}
                if covered < len(batches):
                    summary_warning = f"Partial summary: {covered} of {len(batches)} transcript sections were processed"
                elif len(batches) > 1:
                    summary_warning = f"Summary covers {len(batches)} transcript sections separately; review for cross-section context"
                if covered == 0:
                    summary = None
                    summary_warning = "Transcript saved; local summary could not be generated"
            except Exception:
                summary = None
                summary_warning = "Transcript saved; local summary could not be generated"
        with sessionmaker() as db:
            if job_guard is not None and not job_guard():
                return
            note = db.get(Note, note_id)
            if note is None:
                return
            note.duration_seconds = duration
            note.transcript_segments = segments
            note.transcript_raw = transcript
            note.summary = summary
            note.summary_warning = summary_warning
            note.status = "completed"
            note.progress = 100
            note.processing_error = None
            note.updated_at = utcnow()
            db.commit()
    except Exception as exc:
        with sessionmaker() as db:
            note = db.get(Note, note_id)
            if note is not None:
                note.status = "failed"
                note.processing_error = "Local audio transcription failed"
                note.updated_at = utcnow()
                db.commit()
        raise RuntimeError("Note processing failed") from exc


def _audio_path(sessionmaker, note_id: uuid.UUID, directory: Path) -> Path:
    with sessionmaker() as db:
        note = db.get(Note, note_id)
        if note is None or not note.mime_type:
            raise LookupError("Note audio is unavailable")
        return directory / f"recording{MIME_EXTENSIONS[note.mime_type]}"
