#!/usr/bin/env python3
"""Inspect or explicitly replace local data from a verified backup."""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def safe_member(root: Path, relative: str) -> Path:
    if not relative or "\\" in relative or "\x00" in relative:
        raise SystemExit(f"Backup contains an unsafe path: {relative!r}")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise SystemExit(f"Backup contains an unsafe path: {relative}")
    candidate = root / path
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise SystemExit(f"Backup contains a symlinked path: {relative}")
    return candidate


def load_verified(backup: Path) -> list[tuple[Path, Path]]:
    if backup.is_symlink() or not backup.is_dir():
        raise SystemExit(f"Backup directory does not exist or is a symlink: {backup}")
    manifest_path = backup / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise SystemExit("Backup has no regular manifest.json.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("format") != 1 or not isinstance(manifest.get("files"), list):
            raise ValueError("unsupported manifest format")
        files = manifest["files"]
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"Invalid backup manifest: {exc}") from exc
    verified: list[tuple[Path, Path]] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise SystemExit("Invalid file entry in backup manifest.")
        relative = item["path"]
        if relative in seen:
            raise SystemExit(f"Duplicate path in backup manifest: {relative}")
        seen.add(relative)
        source = safe_member(backup, relative)
        if not source.is_file() or source.is_symlink():
            raise SystemExit(f"Backup file is missing or is a symlink: {item['path']}")
        expected = item.get("sha256")
        if not isinstance(expected, str) or digest(source) != expected:
            raise SystemExit(f"Checksum mismatch: {item['path']}")
        target = ROOT / ".env" if relative == ".env.enc" else safe_member(ROOT, relative)
        if target not in (ROOT / "postgres.sql", ROOT / ".env") and not target.is_relative_to(ROOT / "data"):
            raise SystemExit(f"Backup contains a file outside data/: {item['path']}")
        if target == ROOT / ".env" and relative != ".env.enc":
            raise SystemExit("Only encrypted .env backups are accepted.")
        if target == ROOT / "data" or target.is_symlink() or (target.exists() and not target.is_file()):
            raise SystemExit(f"Restore target is not a regular file: {item['path']}")
        verified.append((source, target))
    return verified


def app_is_stopped() -> None:
    active_ports = []
    for port in (4310, 4311):
        with socket.socket() as probe:
            probe.settimeout(0.25)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                active_ports.append(port)
    if active_ports:
        raise SystemExit(
            "Refusing replacement while the local web/API is running on port(s) "
            + ", ".join(map(str, active_ports))
            + ". Stop the app first; database and search containers may remain running."
        )


def restore_file(source: Path, target: Path) -> None:
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise SystemExit(f"Refusing to replace a non-regular target: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)
    target.chmod(0o600)


def stage_files(verified: list[tuple[Path, Path]], restore_env: bool) -> tuple[tempfile.TemporaryDirectory[str], dict[Path, Path]]:
    staging = tempfile.TemporaryDirectory(prefix=".restore-stage-", dir=ROOT)
    staging_root = Path(staging.name)
    staged: dict[Path, Path] = {}
    for source, target in verified:
        if target == ROOT / ".env" and not restore_env:
            continue
        stage_path = staging_root / ("env/.env" if target == ROOT / ".env" else str(target.relative_to(ROOT)))
        stage_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if source.name == ".env.enc":
            try:
                from cryptography.fernet import Fernet, InvalidToken
                from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
            except ImportError as exc:
                staging.cleanup()
                raise SystemExit("Run restore with `uv run python scripts/restore.py` to use the installed crypto dependency.") from exc
            password = getpass.getpass("Backup encryption passphrase: ")
            encrypted = source.read_bytes()
            header = b"ENV-BACKUP-V1\n"
            if not encrypted.startswith(header) or len(encrypted) <= len(header) + 16:
                staging.cleanup()
                raise SystemExit("Invalid encrypted .env backup; nothing was changed.")
            salt = encrypted[len(header):len(header) + 16]
            token = encrypted[len(header) + 16:]
            key = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(password.encode("utf-8"))
            try:
                stage_path.write_bytes(Fernet(base64.urlsafe_b64encode(key)).decrypt(token))
            except InvalidToken as exc:
                staging.cleanup()
                raise SystemExit("Could not authenticate/decrypt .env backup; nothing was changed.") from exc
        else:
            shutil.copyfile(source, stage_path)
        stage_path.chmod(0o600)
        if digest(stage_path) != digest(source) and source.name != ".env.enc":
            staging.cleanup()
            raise SystemExit(f"Backup changed while staging: {source.name}")
        staged[target] = stage_path
    return staging, staged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path, help="backup directory to inspect or restore")
    parser.add_argument("--inspect", action="store_true", help="verify and list the backup without changing anything")
    parser.add_argument("--confirm-replace", action="store_true", help="allow replacement of current local data")
    parser.add_argument("--restore-env", action="store_true", help="decrypt and replace .env as part of confirmed restore")
    parser.add_argument("--postgres-service", default="db", help="Compose database service name")
    args = parser.parse_args()
    backup = args.backup.expanduser()
    if backup.is_symlink():
        raise SystemExit(f"Backup directory is a symlink: {backup}")
    backup = backup.resolve()
    verified = load_verified(backup)
    print(f"Verified {len(verified)} backup files; no secrets or file contents were displayed.")
    for source, _ in verified:
        print(" ", source.relative_to(backup))
    if args.inspect or not args.confirm_replace:
        print("Inspection only. Use --confirm-replace to replace local data.")
        return 0
    app_is_stopped()
    if args.restore_env and not any(target == ROOT / ".env" for _, target in verified):
        raise SystemExit("This backup has no encrypted .env file.")
    staging, staged = stage_files(verified, args.restore_env)
    print("Replacing local files after checksum validation and stopped-app check.")
    try:
        postgres = staged.get(ROOT / "postgres.sql")
        if postgres is not None:
            with postgres.open("rb") as stream:
                result = subprocess.run(
                    ["docker", "compose", "exec", "-T", args.postgres_service, "psql", "-v", "ON_ERROR_STOP=1",
                     "--single-transaction", "-U", "permetheus", "-d", "permetheus"],
                    cwd=ROOT,
                    stdin=stream,
                    check=False,
                )
            if result.returncode:
                raise SystemExit("PostgreSQL restore failed; local files were not restored.")
        for target, stage_path in staged.items():
            if target == ROOT / "postgres.sql":
                continue
            restore_file(stage_path, target)
    finally:
        staging.cleanup()
    print("Restore complete. The backup remains confidential and was not deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
