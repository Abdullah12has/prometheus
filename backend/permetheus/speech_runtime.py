"""Private subprocess coordination for local ASR and synthesis workers."""

import asyncio
import base64
import json
import math
import os
from pathlib import Path
import signal
import uuid


class SpeechRuntime:
    """Persistent local speech workers. Hold ``asr_lock`` across a live session."""
    _LINE_LIMIT = 4 * 1024 * 1024
    _MAX_SAMPLES = 160_000
    _MAX_TEXT = 3000
    _START_TIMEOUT = 180
    _REQUEST_TIMEOUT = 300

    def __init__(self, root: Path, asr_binary: Path, asr_model: Path, tts_python: Path,
                 tts_script: Path | None = None):
        self.root = root.resolve()
        self.data_root = (self.root / "data").resolve()
        self.asr_binary = asr_binary.resolve()
        self.asr_model = asr_model.resolve()
        # Keep a venv interpreter's symlink path: resolving it can bypass its
        # adjacent pyvenv.cfg and silently lose the installed speech packages.
        self.tts_python = Path(os.path.abspath(tts_python))
        self.tts_script = (tts_script or self.root / "speech" / "tts_worker.py").resolve()
        self.asr_lock = asyncio.Lock()
        self._tts_lock = asyncio.Lock()
        self._asr_io_lock = asyncio.Lock()
        self._asr = None
        self._tts = None
        self._asr_stderr_task = None
        self._tts_stderr_task = None
        self._active_asr_id = None
        self._sample_rate = None
        self._closed = False

    def health(self) -> dict:
        return {
            "asr": self._worker_status(self._asr),
            "tts": self._worker_status(self._tts),
            "asr_stream_active": self._active_asr_id is not None,
            "closed": self._closed,
        }

    @staticmethod
    def _worker_status(proc) -> str:
        return "ready" if proc is not None and proc.returncode is None else "stopped"

    def _data_path(self, value: Path | str, *, output: bool = False) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = self.data_root / path
        path = path.resolve()
        if not path.is_relative_to(self.data_root):
            raise ValueError("Speech files must be inside the private data directory")
        if not output and not path.is_file():
            raise ValueError("Speech input file does not exist")
        return path

    @staticmethod
    def _request_id(value: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise ValueError("Request id must contain 1–128 characters")
        return value

    def _log_path(self, name: str) -> Path:
        logs = self.data_root / "logs"
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        logs.chmod(0o700)
        return logs / name

    def _start_stderr_drain(self, proc, name: str) -> asyncio.Task:
        path = self._log_path(name)

        async def drain():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            logging = True
            try:
                os.fchmod(fd, 0o600)
                while chunk := await proc.stderr.read(8192):
                    if logging:
                        try:
                            os.write(fd, chunk)
                        except OSError:
                            logging = False
            finally:
                os.close(fd)

        return asyncio.create_task(drain())

    async def _spawn(self, *args, env=None):
        if self._closed:
            raise RuntimeError("Speech runtime is closed")
        self.data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_root.chmod(0o700)
        return await asyncio.create_subprocess_exec(
            *map(str, args),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=self._LINE_LIMIT,
            env=env,
        )

    async def _ensure_asr(self):
        if self._worker_status(self._asr) == "ready":
            return self._asr
        await self._stop_asr()
        proc = await self._spawn(self.asr_binary, "--model", self.asr_model)
        self._asr = proc
        self._asr_stderr_task = self._start_stderr_drain(proc, "asr.stderr.log")
        try:
            ready = await self._read_json(proc, self._START_TIMEOUT)
            if ready.get("type") != "ready":
                raise RuntimeError("ASR worker did not send ready")
            return proc
        except BaseException:
            await self._stop_asr()
            raise

    async def _ensure_tts(self):
        if self._worker_status(self._tts) == "ready":
            return self._tts
        await self._stop_tts()
        env = os.environ.copy()
        env["PERMETHEUS_DATA_DIR"] = str(self.data_root)
        proc = await self._spawn(self.tts_python, self.tts_script, env=env)
        self._tts = proc
        self._tts_stderr_task = self._start_stderr_drain(proc, "tts.stderr.log")
        try:
            ready = await self._read_json(proc, self._START_TIMEOUT)
            if ready.get("event") != "ready" or not isinstance(ready.get("sample_rate"), int):
                raise RuntimeError("TTS worker did not send a valid ready event")
            self._sample_rate = ready["sample_rate"]
            return proc
        except BaseException:
            await self._stop_tts()
            raise

    async def _read_json(self, proc, timeout: float) -> dict:
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("Speech worker response timed out") from exc
        except (ValueError, asyncio.LimitOverrunError) as exc:
            raise RuntimeError("Speech worker response exceeds the line limit") from exc
        if not line:
            raise RuntimeError("Speech worker exited before replying")
        if len(line) > self._LINE_LIMIT:
            raise RuntimeError("Speech worker response exceeds the size limit")
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Speech worker returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Speech worker response must be an object")
        return value

    async def _write_json(self, proc, request: dict):
        data = json.dumps(request, separators=(",", ":")).encode() + b"\n"
        proc.stdin.write(data)
        try:
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise RuntimeError("Speech worker exited before accepting the request") from exc

    @staticmethod
    def _expect(value: dict, request_id: str, event_key: str, event: str):
        if value.get("id") != request_id:
            raise RuntimeError("Speech worker response id did not match the request")
        actual = value.get(event_key)
        if actual == "error":
            raise RuntimeError(str(value.get("error", value.get("message", "Speech worker failed"))))
        if actual != event:
            raise RuntimeError(f"Unexpected speech worker event: {actual!r}")

    async def _asr_request(self, request: dict, timeout: float = _REQUEST_TIMEOUT) -> dict:
        proc = await self._ensure_asr()
        expected = request.pop("_expected")
        try:
            await self._write_json(proc, request)
            value = await self._read_json(proc, timeout)
        except (TimeoutError, RuntimeError, asyncio.CancelledError):
            await self._stop_asr()
            raise
        if value.get("id") != request["id"] or value.get("type") not in (expected, "error"):
            await self._stop_asr()
            raise RuntimeError("ASR worker returned an unexpected response")
        if value.get("type") == "error":
            raise RuntimeError(str(value.get("error", "ASR worker failed")))
        return value

    async def transcribe(self, path: Path | str) -> str:
        local = self._data_path(path)
        async with self.asr_lock:
            if self._active_asr_id is not None:
                raise RuntimeError("A live ASR session is already active")
            async with self._asr_io_lock:
                request_id = uuid.uuid4().hex
                value = await self._asr_request({
                    "id": request_id, "op": "transcribe", "path": str(local), "_expected": "final"
                })
                text = value.get("text")
                if not isinstance(text, str):
                    raise RuntimeError("ASR worker final event has no text")
                return text

    async def asr_start(self, request_id: str):
        request_id = self._request_id(request_id)
        async with self._asr_io_lock:
            if self._active_asr_id is not None:
                raise RuntimeError("A live ASR session is already active")
            await self._asr_request({"id": request_id, "op": "start", "_expected": "started"})
            self._active_asr_id = request_id

    async def asr_feed(self, request_id: str, samples) -> dict:
        request_id = self._request_id(request_id)
        if request_id != self._active_asr_id:
            raise RuntimeError("No live ASR session matches this id")
        if not isinstance(samples, (list, tuple)):
            raise ValueError("Samples must be a list or tuple of normalized audio floats")
        if len(samples) > self._MAX_SAMPLES:
            raise ValueError(f"Feed must contain 1–{self._MAX_SAMPLES} samples")
        chunk = samples
        if not chunk or len(chunk) > self._MAX_SAMPLES:
            raise ValueError(f"Feed must contain 1–{self._MAX_SAMPLES} samples")
        normalized = []
        for value in chunk:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError("Samples must be finite values in [-1, 1]")
            try:
                sample = float(value)
            except OverflowError as exc:
                raise ValueError("Samples must be finite values in [-1, 1]") from exc
            if not math.isfinite(sample) or not -1 <= sample <= 1:
                raise ValueError("Samples must be finite values in [-1, 1]")
            normalized.append(sample)
        async with self._asr_io_lock:
            if request_id != self._active_asr_id:
                raise RuntimeError("No live ASR session matches this id")
            return await self._asr_request({
                "id": request_id, "op": "feed", "samples": normalized, "_expected": "partial"
            }, timeout=60)

    async def asr_finalize(self, request_id: str) -> str:
        request_id = self._request_id(request_id)
        async with self._asr_io_lock:
            if request_id != self._active_asr_id:
                raise RuntimeError("No live ASR session matches this id")
            try:
                value = await self._asr_request({
                    "id": request_id, "op": "finalize", "_expected": "final"
                }, timeout=60)
                text = value.get("text")
                if not isinstance(text, str):
                    raise RuntimeError("ASR worker final event has no text")
                return text
            finally:
                self._active_asr_id = None

    async def asr_cancel(self, request_id: str):
        request_id = self._request_id(request_id)
        async with self._asr_io_lock:
            if request_id != self._active_asr_id:
                return
            try:
                await self._asr_request({
                    "id": request_id, "op": "cancel", "_expected": "cancelled"
                }, timeout=10)
            finally:
                self._active_asr_id = None

    async def tts_stream(self, text: str, voice_path: Path | str | None = None):
        if not isinstance(text, str) or not text.strip() or len(text) > self._MAX_TEXT:
            raise ValueError(f"Text must contain 1–{self._MAX_TEXT} characters")
        voice = str(self._data_path(voice_path)) if voice_path is not None else None
        request_id = uuid.uuid4().hex
        async with self._tts_lock:
            proc = await self._ensure_tts()
            completed = False
            try:
                request = {"id": request_id, "op": "synthesize", "text": text}
                if voice is not None:
                    request["voice"] = voice
                await self._write_json(proc, request)
                while True:
                    value = await self._read_json(proc, self._REQUEST_TIMEOUT)
                    if value.get("id") != request_id:
                        raise RuntimeError("TTS worker response id did not match the request")
                    event = value.get("event")
                    if event == "error":
                        raise RuntimeError(str(value.get("message", "Local synthesis failed")))
                    if event == "done":
                        completed = True
                        break
                    if event != "audio" or value.get("sample_rate") != self._sample_rate:
                        raise RuntimeError("TTS worker returned an invalid audio event")
                    try:
                        pcm = base64.b64decode(value["pcm"], validate=True)
                    except (KeyError, ValueError, TypeError) as exc:
                        raise RuntimeError("TTS worker returned invalid PCM data") from exc
                    if not pcm or len(pcm) % 2:
                        raise RuntimeError("TTS worker returned malformed PCM data")
                    yield self._sample_rate, pcm
            finally:
                if not completed:
                    await self._stop_tts()

    async def clone_voice(self, source: Path | str, target: Path | str):
        source_path = self._data_path(source)
        if source_path.stat().st_size > 50 * 1024 * 1024:
            raise ValueError("Voice sample exceeds 50 MB")
        target_path = self._data_path(target, output=True)
        if target_path.suffix != ".safetensors" or target_path.exists():
            raise ValueError("Voice state target must be a new .safetensors file")
        async with self._tts_lock:
            request_id = uuid.uuid4().hex
            proc = await self._ensure_tts()
            try:
                await self._write_json(proc, {
                    "id": request_id, "op": "clone", "source": str(source_path), "target": str(target_path)
                })
                value = await self._read_json(proc, self._REQUEST_TIMEOUT)
            except BaseException:
                await self._stop_tts()
                raise
            if value.get("id") != request_id or value.get("event") not in ("cloned", "error"):
                await self._stop_tts()
                raise RuntimeError("TTS worker returned an unexpected response")
            self._expect(value, request_id, "event", "cloned")

    async def _stop_process(self, attr: str, task_attr: str):
        proc = getattr(self, attr)
        setattr(self, attr, None)
        task = getattr(self, task_attr)
        setattr(self, task_attr, None)
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
        if task is not None:
            try:
                await asyncio.wait_for(task, 2)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _stop_asr(self):
        await self._stop_process("_asr", "_asr_stderr_task")
        self._active_asr_id = None

    async def _stop_tts(self):
        await self._stop_process("_tts", "_tts_stderr_task")
        self._sample_rate = None

    async def close(self):
        self._closed = True
        await asyncio.gather(self._stop_asr(), self._stop_tts())
