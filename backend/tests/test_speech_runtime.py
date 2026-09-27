import asyncio
import tempfile
import textwrap
import unittest
from pathlib import Path

from permetheus.speech_runtime import SpeechRuntime


ASR_SOURCE = '''\
#!/usr/bin/env python3
import json, os, sys
count = __file__ + ".count"
try:
    n = int(open(count).read()) + 1
except FileNotFoundError:
    n = 1
open(count, "w").write(str(n))
print(json.dumps({"type":"ready"}), flush=True)
for line in sys.stdin:
    q = json.loads(line)
    if n == 1 and os.environ.get("KILL_FIRST_ASR") == "1":
        os._exit(4)
    op = q["op"]
    event = {"id":q["id"], "type":{"start":"started", "feed":"partial", "finalize":"final", "cancel":"cancelled", "transcribe":"final"}[op]}
    if op == "feed": event.update(committed="hello", tentative=" world")
    if op in ("finalize", "transcribe"): event["text"] = "hello world"
    print(json.dumps(event), flush=True)
'''

TTS_SOURCE = '''\
import base64, json, sys, time
print(json.dumps({"event":"ready", "sample_rate":24000}), flush=True)
for line in sys.stdin:
    q=json.loads(line)
    if q["op"] == "synthesize":
        for _ in range(3):
            print(json.dumps({"id":q["id"], "event":"audio", "sample_rate":24000, "pcm":base64.b64encode(b"\\x01\\x00").decode()}), flush=True)
            time.sleep(.1)
        print(json.dumps({"id":q["id"], "event":"done"}), flush=True)
    elif q["op"] == "clone":
        open(q["target"], "wb").write(b"state")
        print(json.dumps({"id":q["id"], "event":"cloned"}), flush=True)
'''


class SpeechRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        self.asr = self.root / "fake-asr"
        self.asr.write_text(textwrap.dedent(ASR_SOURCE))
        self.asr.chmod(0o700)
        self.tts = self.root / "fake-tts.py"
        self.tts.write_text(textwrap.dedent(TTS_SOURCE))
        self.runtime = SpeechRuntime(
            self.root, self.asr, self.root / "model.gguf", Path("/usr/bin/python3"), self.tts
        )

    async def asyncTearDown(self):
        await self.runtime.close()
        self.temp.cleanup()

    async def test_asr_stream_and_batch(self):
        async with self.runtime.asr_lock:
            await self.runtime.asr_start("session-1")
            update = await self.runtime.asr_feed("session-1", [0.0, 0.25])
            self.assertEqual((update["committed"], update["tentative"]), ("hello", " world"))
            self.assertEqual(await self.runtime.asr_finalize("session-1"), "hello world")
        audio = self.root / "data" / "recording.wav"
        audio.write_bytes(b"test")
        self.assertEqual(await self.runtime.transcribe(audio), "hello world")

    async def test_rejects_path_escape_and_invalid_samples(self):
        with self.assertRaises(ValueError):
            await self.runtime.transcribe(self.root / "outside.wav")
        with self.assertRaises(ValueError):
            await self.runtime.clone_voice(self.root / "outside.wav", self.root / "outside.safetensors")
        async with self.runtime.asr_lock:
            await self.runtime.asr_start("validate")
            with self.assertRaises(ValueError):
                await self.runtime.asr_feed("validate", [0.0, float("nan")])
            await self.runtime.asr_cancel("validate")
        self.assertEqual(self.runtime.health()["asr"], "ready")

    async def test_tts_stream_cancellation_stops_worker_and_restarts(self):
        stream = self.runtime.tts_stream("Hello")
        self.assertEqual(await anext(stream), (24000, b"\x01\x00"))
        await stream.aclose()
        self.assertEqual(self.runtime.health()["tts"], "stopped")
        chunks = [chunk async for chunk in self.runtime.tts_stream("Again")]
        self.assertEqual(len(chunks), 3)
        self.assertEqual(self.runtime.health()["tts"], "ready")

    async def test_asr_worker_restart_after_process_death(self):
        import os
        old = os.environ.get("KILL_FIRST_ASR")
        os.environ["KILL_FIRST_ASR"] = "1"
        try:
            audio = self.root / "data" / "recording.wav"
            audio.write_bytes(b"test")
            with self.assertRaises(RuntimeError):
                await self.runtime.transcribe(audio)
            self.assertEqual(await self.runtime.transcribe(audio), "hello world")
        finally:
            if old is None:
                os.environ.pop("KILL_FIRST_ASR", None)
            else:
                os.environ["KILL_FIRST_ASR"] = old


if __name__ == "__main__":
    unittest.main()
