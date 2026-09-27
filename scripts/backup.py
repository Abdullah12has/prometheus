#!/usr/bin/env python3
"""Create a private local-data backup with a checksum manifest."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import shutil
import subprocess
import base64
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise SystemExit(f"Refusing symlinked backup destination: {path}")
    path = path.resolve()
    if path.exists() and any(path.iterdir()):
        raise SystemExit(f"Backup destination must be new or empty: {path}")
    if path.is_relative_to(ROOT):
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", "--", str(path.relative_to(ROOT))],
            cwd=ROOT,
            check=False,
        )
        if ignored.returncode:
            raise SystemExit(
                f"Refusing to write backup under non-ignored path {path}. "
                "Choose an ignored private directory with --output."
            )
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    if path.stat().st_mode & 0o077:
        raise SystemExit(f"Backup directory is not private: {path}")


EXCLUDED_DATA_DIRECTORIES = {"bin", "logs", "tmp"}


def copy_tree(source: Path, destination: Path, entries: list[Path]) -> None:
    if not source.exists():
        print(f"Skipping absent local-data directory: {source.relative_to(ROOT)}")
        return
    if source.is_symlink():
        raise SystemExit(f"Refusing symlinked data directory: {source}")
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        if any(part in EXCLUDED_DATA_DIRECTORIES for part in relative.parts):
            continue
        target = destination / relative
        if item.is_symlink():
            raise SystemExit(f"Refusing symlink in local data: {item}")
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(0o700)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            target.chmod(0o600)
            entries.append(target)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dump_postgres(destination: Path, service: str) -> None:
    if shutil.which("docker") is None:
        raise SystemExit("Missing prerequisite: Docker Desktop with the docker CLI.")
    command = [
        "docker",
        "compose",
        "exec",
        "-T",
        service,
        "pg_dump",
        "-U",
        "permetheus",
        "-d",
        "permetheus",
        "--clean",
        "--if-exists",
    ]
    print("Creating PostgreSQL dump through Docker Compose.")
    with destination.open("wb") as stream:
        result = subprocess.run(command, cwd=ROOT, stdout=stream, check=False)
    if result.returncode:
        destination.unlink(missing_ok=True)
        raise SystemExit(
            "PostgreSQL dump failed. Start the database container and verify its compose "
            "configuration, then rerun backup."
        )
    destination.chmod(0o600)


def encrypt_env(destination: Path) -> bool:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return False
    if env_file.is_symlink() or not env_file.is_file():
        raise SystemExit("Refusing to back up a symlinked or non-regular .env file.")
    try:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError as exc:
        raise SystemExit("Run backup with `uv run python scripts/backup.py` to use the installed crypto dependency.") from exc
    password = getpass.getpass("Backup encryption passphrase: ")
    confirmation = getpass.getpass("Confirm passphrase: ")
    if len(password) < 12 or password != confirmation:
        raise SystemExit("Passphrases must match and contain at least 12 characters.")
    encrypted = destination / ".env.enc"
    salt = os.urandom(16)
    key = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(password.encode("utf-8"))
    token = Fernet(base64.urlsafe_b64encode(key)).encrypt(env_file.read_bytes())
    encrypted.write_bytes(b"ENV-BACKUP-V1\n" + salt + token)
    encrypted.chmod(0o600)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="private backup directory")
    parser.add_argument("--postgres-service", default="db", help="Compose database service name")
    args = parser.parse_args()
    output = args.output or Path.home() / ".permetheus-backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = output.expanduser()
    if output.resolve().is_relative_to(ROOT / "data"):
        raise SystemExit("Choose a backup destination outside data/ to avoid copying a backup into itself.")
    from restore import app_is_stopped
    app_is_stopped()
    private_directory(output)
    entries: list[Path] = []
    copy_tree(ROOT / "data", output / "data", entries)
    dump_postgres(output / "postgres.sql", args.postgres_service)
    entries.append(output / "postgres.sql")
    env_backed_up = encrypt_env(output)
    if env_backed_up:
        entries.append(output / ".env.enc")
    manifest = {
        "format": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "files": [
            {"path": str(path.relative_to(output)), "sha256": sha256(path)}
            for path in sorted(entries)
        ],
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    print(f"Backup complete: {output}")
    print("Confidential: it contains application/database data and, when present, an encrypted .env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
