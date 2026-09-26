"""Serialized local synthesis worker. JSON lines in/out; diagnostics on stderr.

Run with a Python environment containing pocket-tts. Requests are private IPC,
not a network API. Terminate the process to cancel inference immediately.
"""
import base64
import contextlib
import json
import os
from pathlib import Path
import sys


def emit(event):
    print(json.dumps(event), flush=True)


def local_path(value, root, *, output=False):
    if not isinstance(value, str) or not value:
        raise ValueError('A local voice file is required')
    path = Path(value).resolve()
    if not path.is_relative_to(root):
        raise ValueError('Voice files must be inside the data directory')
    if not output and (not path.is_file() or path.stat().st_size > 50 * 1024 * 1024):
        raise ValueError('Voice file is missing or exceeds 50 MB')
    return path


def main():
    root = Path(os.environ.get('PERMETHEUS_DATA_DIR', 'data')).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        from pocket_tts import TTSModel
        from pocket_tts.models.tts_model import export_model_state
        torch.set_num_threads(4)
        model = TTSModel.load_model(language='english')
        default_state = model.get_state_for_audio_prompt('alba')
    emit({'event': 'ready', 'sample_rate': model.sample_rate, 'voice_cloning': model.has_voice_cloning})
    # ponytail: one serialized inference worker; use separate workers only when concurrent sessions are required.
    for line in sys.stdin:
        request_id = None
        try:
            if len(line) > 65536:
                raise ValueError('Request exceeds 64 KB')
            request = json.loads(line)
            request_id = request.get('id')
            if not isinstance(request_id, str) or len(request_id) > 128:
                raise ValueError('A bounded request id is required')
            op = request.get('op')
            if op == 'clone':
                if not model.has_voice_cloning:
                    raise ValueError('Voice cloning model is unavailable')
                source = local_path(request.get('source'), root)
                target = local_path(request.get('target'), root, output=True)
                if target.suffix != '.safetensors' or target.exists():
                    raise ValueError('Target must be a new .safetensors file')
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix('.tmp')
                try:
                    with contextlib.redirect_stdout(sys.stderr):
                        state = model.get_state_for_audio_prompt(source, truncate=True)
                        export_model_state(state, temporary)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
                emit({'id': request_id, 'event': 'cloned'})
            elif op == 'synthesize':
                text = request.get('text')
                if not isinstance(text, str) or not text.strip() or len(text) > 3000:
                    raise ValueError('Text must contain 1 to 3000 characters')
                with contextlib.redirect_stdout(sys.stderr):
                    state = model.get_state_for_audio_prompt(local_path(request['voice'], root)) if request.get('voice') else default_state
                    chunks = model.generate_audio_stream(state, text)
                for chunk in chunks:
                    pcm = chunk.detach().cpu().clamp(-1, 1).mul(32767).to(torch.int16).numpy().astype('<i2').tobytes()
                    emit({'id': request_id, 'event': 'audio', 'sample_rate': model.sample_rate, 'pcm': base64.b64encode(pcm).decode()})
                emit({'id': request_id, 'event': 'done'})
            else:
                raise ValueError('Unknown operation')
        except Exception as exc:
            print(f'{type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
            emit({'id': request_id, 'event': 'error', 'message': str(exc) if isinstance(exc, ValueError) else 'Local synthesis failed; inspect local diagnostics'})


if __name__ == '__main__':
    main()
