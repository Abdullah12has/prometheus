"""Report readiness without printing credentials."""
import json
import shutil
import socket
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent.parent
values = {}
if (root / '.env').exists():
    values = dict(line.split('=', 1) for line in (root / '.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
report = {'tools': {name: bool(shutil.which(name)) for name in ('uv', 'node', 'npm', 'docker', 'cargo', 'ffmpeg')}}
report['configuration'] = {key: bool(values.get(key)) for key in ('DATABASE_URL', 'SESSION_SECRET', 'ADMIN_PASSWORD', 'LLM_BASE_URL', 'LLM_API_KEY', 'LLM_MODEL', 'GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'TWILIO_FROM_NUMBER')}
report['listening_ports'] = {}
for port in (4310, 4311, 4312, 4313, 4314, 5433, 8888):
    with socket.socket() as sock:
        sock.settimeout(.15)
        report['listening_ports'][port] = sock.connect_ex(('127.0.0.1', port)) == 0
result = subprocess.run(['git', 'check-ignore', '.env'], cwd=root, capture_output=True)
report['env_ignored'] = result.returncode == 0
print(json.dumps(report, indent=2))
