import asyncio
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
import uuid
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from permetheus.auth import require_session
from permetheus.db import make_engine, make_sessionmaker
from permetheus.errors import install
from permetheus.models import Base, Job, JobState
from permetheus.documents import Document
from permetheus.notes import Note, _normalize_audio, process_note, router
from permetheus.research import router as research_router


class FakeSpeech:
    def __init__(self, data_root):
        self.data_root = data_root
        self.calls = []

    async def transcribe(self, path):
        self.calls.append(Path(path))
        return f"Chunk {len(self.calls)} says the owner is open to a discussion."


class FakeLanguageModel:
    async def extract(self, transcript, instruction):
        self.instruction = instruction
        return {
            "summary": "The owner is open to a discussion.",
            "facts": [
                {"text": "Open to a discussion", "quote": "the owner is open to a discussion"},
                {"text": "Invented claim", "quote": "the company will be sold next month"},
            ],
            "actions": [{"text": "Arrange a follow-up", "quote": "open to a discussion"}],
        }


class NotesApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.engine = make_engine("sqlite://")
        self.sessions = make_sessionmaker(self.engine)
        Base.metadata.create_all(self.engine)
        app = FastAPI()
        app.state.settings = SimpleNamespace(data_dir=self.data)
        app.state.sessionmaker = self.sessions
        app.state.speech = FakeSpeech(self.data)
        app.state.llm = FakeLanguageModel()
        app.include_router(router)
        app.include_router(research_router)
        install(app)
        app.dependency_overrides[require_session] = lambda: object()
        self.app = app
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.temp.cleanup()

    def create(self):
        response = self.client.post("/api/notes", json={"title": "First call"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def upload(self, note_id, sequence, data, mime="audio/webm"):
        return self.client.put(
            f"/api/notes/{note_id}/chunks/{sequence}",
            content=data,
            headers={"Content-Type": mime},
        )

    def test_chunks_are_idempotent_conflict_safe_and_recoverable(self):
        note = self.create()
        note_id = note["id"]
        second = b"second"
        self.assertEqual(self.upload(note_id, 1, second).status_code, 200)
        self.assertEqual(self.upload(note_id, 1, second).json()["idempotent"], True)
        self.assertEqual(self.upload(note_id, 1, b"changed").status_code, 409)
        self.assertEqual(self.upload(note_id, 2, b"x" * (8 * 1024 * 1024 + 1)).status_code, 413)
        detail = self.client.get(f"/api/notes/{note_id}").json()
        self.assertEqual(detail["received_sequences"], [1])
        chunk_path = self.data / "notes" / note_id / "chunks" / f"1-{detail['chunks'][0]['sha256']}.bin"
        self.assertEqual(stat.S_IMODE(chunk_path.stat().st_mode), 0o600)
        incomplete = self.client.post(
            f"/api/notes/{note_id}/finalize",
            json={"chunk_count": 2, "mime_type": "audio/webm"},
        )
        self.assertEqual(incomplete.status_code, 409)
        self.assertEqual(self.upload(note_id, 0, b"first").status_code, 200)
        finalized = self.client.post(
            f"/api/notes/{note_id}/finalize",
            json={"chunk_count": 2, "mime_type": "audio/webm;codecs=opus"},
        )
        self.assertEqual(finalized.status_code, 202, finalized.text)
        self.assertEqual(finalized.json()["status"], "queued")
        job_id = finalized.json()["job_id"]
        self.assertTrue(job_id)
        self.assertEqual(self.client.get(f"/api/notes/{note_id}").json()["job_id"], job_id)
        audio = self.client.get(f"/api/notes/{note_id}/audio")
        self.assertEqual(audio.content, b"firstsecond")
        with self.sessions() as db:
            job = db.query(Job).one()
            self.assertEqual(job.kind, "note.transcribe")

    def test_failed_note_exposes_job_and_retry_preserves_transcripts(self):
        note = self.create()
        note_id = uuid.UUID(note["id"])
        self.upload(note["id"], 0, b"recorded audio")
        finalized = self.client.post(f"/api/notes/{note['id']}/finalize", json={
            "chunk_count": 1, "mime_type": "audio/webm",
        }).json()
        job_id = uuid.UUID(finalized["job_id"])
        with self.sessions() as db:
            row = db.get(Note, note_id)
            row.status = "failed"
            row.processing_error = "temporary worker error"
            row.transcript_raw = "Original ASR transcript."
            row.transcript_corrected = "Human correction."
            row.transcript_segments = [{"start_sec": 0, "end_sec": 1, "text": "Original ASR transcript."}]
            job = db.get(Job, job_id)
            job.state = JobState.failed
            job.attempts = 3
            db.commit()

        failed = self.client.get(f"/api/notes/{note_id}").json()
        self.assertEqual(failed["job_id"], str(job_id))
        self.assertEqual(failed["status"], "failed")
        retried = self.client.post(f"/api/jobs/{job_id}/retry")
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(retried.json()["state"], "queued")
        with self.sessions() as db:
            self.assertEqual(db.query(Job).count(), 1)
            self.assertEqual(db.query(Job).one().id, job_id)
            self.assertEqual(db.query(Job).one().attempts, 3)
        queued = self.client.get(f"/api/notes/{note_id}").json()
        self.assertEqual(queued["status"], "queued")
        self.assertIsNone(queued["processing_error"])
        self.assertEqual(queued["job_id"], str(job_id))
        self.assertEqual(queued["transcript_raw"], "Original ASR transcript.")
        self.assertEqual(queued["transcript_corrected"], "Human correction.")
        self.assertEqual(queued["transcript_segments"][0]["text"], "Original ASR transcript.")

    def test_local_paths_mime_title_corrections_and_delete(self):
        note = self.create()
        note_id = note["id"]
        self.assertEqual(self.upload(note_id, 0, b"voice", "text/plain").status_code, 415)
        self.assertEqual(self.upload(note_id, 0, b"voice").status_code, 200)
        self.assertEqual(self.client.post(
            f"/api/notes/{note_id}/finalize",
            json={"chunk_count": 1, "mime_type": "audio/webm"},
        ).status_code, 202)
        directory = self.data / "notes" / note_id
        self.assertTrue((directory / "recording.webm").is_file())
        self.assertEqual(stat.S_IMODE(os.stat(directory / "recording.webm").st_mode), 0o600)
        updated = self.client.patch(
            f"/api/notes/{note_id}",
            json={"title": "Corrected title", "transcript_corrected": "Reviewed wording."},
        ).json()
        self.assertEqual(updated["transcript"], "Reviewed wording.")
        self.assertEqual(updated["transcript_corrected"], "Reviewed wording.")
        self.assertIsNone(updated["transcript_raw"])
        self.assertEqual(self.client.delete(f"/api/notes/{note_id}").status_code, 204)
        self.assertFalse(directory.exists())
        self.assertEqual(self.client.get(f"/api/notes/{note_id}").status_code, 404)
        with self.sessions() as db:
            job = db.query(Job).one()
            self.assertEqual(job.state, JobState.cancelled)

    def test_router_requires_a_session(self):
        app = FastAPI()
        app.state.sessionmaker = self.sessions
        app.state.settings = SimpleNamespace(data_dir=self.data)
        app.include_router(router)
        install(app)
        with TestClient(app) as anonymous:
            self.assertEqual(anonymous.get("/api/notes").status_code, 401)

    def test_upload_sequence_supports_long_recordings(self):
        note = self.create()
        self.assertEqual(self.upload(note["id"], 480, b"long-session").status_code, 200)

    def test_worker_persists_chunk_times_and_only_quoted_proposals(self):
        note = self.create()
        note_id = uuid.UUID(note["id"])
        with self.sessions() as db:
            row = db.get(Note, note_id)
            row.status = "queued"
            row.mime_type = "audio/webm"
            row.transcript_corrected = "Human correction survives retry."
            db.commit()

        processed = self.data / "notes" / str(note_id) / "processed"

        async def fake_normalize(source, destination):
            destination.mkdir(parents=True)
            chunk = destination / "part-00000.wav"
            with wave.open(str(chunk), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(b"\0\0" * 16000)
            return 1.0, [chunk]

        with patch("permetheus.notes._normalize_audio", fake_normalize):
            asyncio.run(process_note(note_id, self.sessions, self.app.state.speech, self.app.state.llm))
        saved = self.client.get(f"/api/notes/{note_id}").json()
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["transcript_corrected"], "Human correction survives retry.")
        self.assertEqual(saved["progress"], 100)
        self.assertIn("start_sec", saved["transcript_segments"][0])
        self.assertEqual(len(saved["summary"]["facts"]), 1)
        self.assertEqual(saved["summary"]["facts"][0]["status"], "proposed")
        self.assertEqual(len(saved["summary"]["actions"]), 1)
        corrected = self.client.patch(
            f"/api/notes/{note_id}", json={"transcript_corrected": "Reviewed transcript."}
        ).json()
        self.assertEqual(corrected["transcript_raw"], saved["transcript_raw"])
        self.assertEqual(corrected["transcript"], "Reviewed transcript.")

    def test_audio_duration_is_checked_before_ffmpeg(self):
        probe = self.root / "ffprobe"
        ffmpeg = self.root / "ffmpeg"
        marker = self.root / "ffmpeg-ran"
        probe.write_text("#!/bin/sh\nprintf '14401\\n'\n")
        ffmpeg.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        probe.chmod(0o700)
        ffmpeg.chmod(0o700)

        def find_tool(name):
            return str(probe if name == "ffprobe" else ffmpeg)

        with patch("permetheus.notes.shutil.which", side_effect=find_tool):
            with self.assertRaisesRegex(RuntimeError, "four hours"):
                asyncio.run(_normalize_audio(self.root / "recording.webm", self.data / "processed"))
        self.assertFalse(marker.exists())

    def test_durationless_webm_is_decoded_and_measured(self):
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("ffmpeg and ffprobe are unavailable")
        source_wav = self.root / "source.wav"
        with wave.open(str(source_wav), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 16000)
        webm = self.root / "durationless.webm"
        with webm.open("wb") as output:
            subprocess.run(
                [ffmpeg, "-nostdin", "-v", "error", "-i", str(source_wav), "-f", "webm", "pipe:1"],
                stdout=output, check=True,
            )
        fake_probe = self.root / "ffprobe-no-duration"
        fake_probe.write_text("#!/bin/sh\nprintf 'N/A\\n'\n")
        fake_probe.chmod(0o700)
        with patch("permetheus.notes.shutil.which", side_effect=lambda name: str(fake_probe if name == "ffprobe" else ffmpeg)):
            duration, segments = asyncio.run(_normalize_audio(webm, self.data / "processed"))
        self.assertAlmostEqual(duration, 1.0, delta=0.1)
        self.assertEqual(len(segments), 1)


if __name__ == "__main__":
    unittest.main()
