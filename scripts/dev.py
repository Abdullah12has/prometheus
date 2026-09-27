"""Run the local API and web process groups; stop both on exit."""
import os
import signal
import subprocess
import time
from pathlib import Path

root = Path(__file__).resolve().parent.parent


def main():
    processes = []
    def interrupted(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        for command, directory in [
            (['uv', 'run', 'uvicorn', 'permetheus.app:create_app', '--factory', '--host', '127.0.0.1', '--port', '4311'], root),
            (['npm', 'run', 'dev', '--', '--host', '127.0.0.1'], root / 'web'),
        ]:
            processes.append(subprocess.Popen(command, cwd=directory, start_new_session=True))
        while all(process.poll() is None for process in processes):
            time.sleep(.5)
        return 1
    finally:
        for process in processes:
            try: os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError: pass
        for process in processes:
            try: process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                process.wait()


if __name__ == '__main__':
    raise SystemExit(main())
