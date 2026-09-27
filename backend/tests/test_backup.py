"""Local backup secrets authenticate before any restore mutation."""
import importlib.util
from pathlib import Path

import pytest


def script(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[2] / 'scripts' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_encrypted_env_roundtrip_tampering_and_path_rejection(tmp_path, monkeypatch):
    backup, restore = script('backup'), script('restore')
    source, target, archive = (tmp_path / name for name in ('source', 'target', 'archive'))
    for path in (source, target, archive): path.mkdir()
    original = b'SESSION_SECRET=fixture-only-secret\n'
    (source / '.env').write_bytes(original)
    monkeypatch.setattr(backup, 'ROOT', source)
    monkeypatch.setattr(restore, 'ROOT', target)
    monkeypatch.setattr(backup.getpass, 'getpass', lambda _: 'fixture-passphrase')
    assert backup.encrypt_env(archive)
    encrypted = archive / '.env.enc'
    staging, files = restore.stage_files([(encrypted, target / '.env')], True)
    try:
        assert files[target / '.env'].read_bytes() == original
        assert not (target / '.env').exists()
    finally: staging.cleanup()
    data = bytearray(encrypted.read_bytes()); data[-10] ^= 1; encrypted.write_bytes(data)
    with pytest.raises(SystemExit, match='authenticate'):
        restore.stage_files([(encrypted, target / '.env')], True)
    assert not (target / '.env').exists()
    for path in ('../escape', '/absolute', 'data\\escape'):
        with pytest.raises(SystemExit): restore.safe_member(archive, path)
    (archive / 'link').symlink_to(source)
    with pytest.raises(SystemExit): restore.safe_member(archive, 'link/.env')
