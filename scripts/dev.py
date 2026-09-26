"""Run application processes together; terminate children on shutdown."""
import signal
import subprocess
import time
from pathlib import Path

root = Path(__file__).resolve().parent.parent
processes = []

def stop(*_, exit_code=0):
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
    raise SystemExit(exit_code)

signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
try:
    processes.append(subprocess.Popen(['uv', 'run', 'uvicorn', 'permetheus.app:app', '--host', '127.0.0.1', '--port', '4311'], cwd=root))
    processes.append(subprocess.Popen(['npm', 'run', 'dev', '--', '--host', '127.0.0.1'], cwd=root / 'web'))
    while all(process.poll() is None for process in processes):
        time.sleep(.5)
finally:
    stop(exit_code=1)
