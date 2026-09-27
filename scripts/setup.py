#!/usr/bin/env python3
"""Reproducible local setup without installing tools globally."""

from __future__ import annotations

import argparse
import secrets
import shutil
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
MODEL_RELATIVE = Path(
    ".cache/huggingface/hub/models--handy-computer--nemotron-3.5-asr-streaming-0.6b-gguf"
    "/snapshots/6d44e540bc31b0de1dbe174a3cea87f53a7f22fb"
    "/nemotron-3.5-asr-streaming-0.6b-Q8_0.gguf"
)
MODEL_URL = (
    "https://huggingface.co/handy-computer/nemotron-3.5-asr-streaming-0.6b-gguf"
    "/resolve/6d44e540bc31b0de1dbe174a3cea87f53a7f22fb/"
    "nemotron-3.5-asr-streaming-0.6b-Q8_0.gguf"
)


def run(command: list[str], *, cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("$", " ".join(command))
    return subprocess.run(command, cwd=cwd, check=check, text=True)


def require(command: str, hint: str) -> str:
    found = shutil.which(command)
    if not found:
        raise SystemExit(f"Missing prerequisite: {command}. {hint}")
    return found


def env_is_ignored() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", ".env"], cwd=ROOT, check=False
    )
    if result.returncode:
        raise SystemExit(
            ".env exists but is not ignored by git; add it to the repository's ignore rules "
            "before running setup. Its contents were not displayed."
        )


def prepare_env() -> None:
    env_file = ROOT / ".env"
    example = ROOT / ".env.example"
    if env_file.is_symlink():
        raise SystemExit("Refusing to change permissions or write through a symlinked .env.")
    if not env_file.exists():
        if not example.exists():
            print("No .env.example found; skipping .env creation.")
            return
        env_file.write_bytes(example.read_bytes())
        env_file.chmod(0o600)
    else:
        env_file.chmod(0o600)
    env_is_ignored()
    # Read only keys and whether their values are empty. Existing values are
    # copied byte-for-byte and are never displayed.
    configured: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        configured[name.strip()] = value.strip()
    additions: list[str] = []
    password = configured.get("POSTGRES_PASSWORD", "")
    if not password:
        password = secrets.token_urlsafe(32)
        additions.append(f"POSTGRES_PASSWORD={password}")
    if not configured.get("DATABASE_URL"):
        additions.append(
            "DATABASE_URL="
            f"postgresql+psycopg://permetheus:{quote(password, safe='')}"
            "@localhost:5433/permetheus"
        )
    for name in ("SESSION_SECRET", "ADMIN_PASSWORD"):
        if not configured.get(name):
            additions.append(f"{name}={secrets.token_urlsafe(32)}")
    if not configured.get("WORKER_ENABLED"):
        additions.append("WORKER_ENABLED=true")
    model_path = Path.home() / MODEL_RELATIVE
    if model_path.is_file() and not configured.get("ASR_MODEL_PATH"):
        additions.append(f"ASR_MODEL_PATH={model_path}")
    tts_python = ROOT / ".venv-speech" / "bin" / "python"
    if not configured.get("TTS_PYTHON"):
        additions.append(f"TTS_PYTHON={tts_python}")
    if additions:
        with env_file.open("a", encoding="utf-8") as stream:
            stream.write("\n" + "\n".join(additions) + "\n")
        env_file.chmod(0o600)
    if env_file.stat().st_mode & 0o077:
        raise SystemExit("Could not secure .env permissions to 0600.")
    print(".env preserved/created with private permissions; values were not displayed.")


def install_web() -> None:
    require("npm", "Install Node.js/npm for this repository user, then rerun setup.")
    run(["npm", "ci"], cwd=ROOT / "web")


def build_asr() -> None:
    cargo = require("cargo", "Install Rust with rustup; setup does not install global tools.")
    asr_dir = ROOT / "native" / "asr"
    if not asr_dir.exists():
        raise SystemExit(f"ASR source directory is missing: {asr_dir}")
    run([cargo, "build", "--release", "--locked"], cwd=asr_dir)
    metadata = subprocess.run(
        [cargo, "metadata", "--format-version", "1", "--no-deps"],
        cwd=asr_dir,
        check=True,
        text=True,
        capture_output=True,
    )
    import json

    package = json.loads(metadata.stdout)["packages"][0]
    binaries = [target["name"] for target in package["targets"] if "bin" in target["kind"]]
    candidates = [asr_dir / "target" / "release" / name for name in binaries]
    binary = next((path for path in candidates if path.is_file()), None)
    if binary is None:
        raise SystemExit("Rust build succeeded but no executable binary was found.")
    destination = ROOT / "data" / "bin" / "native-asr-bridge"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, destination)
    destination.chmod(destination.stat().st_mode | 0o111)
    print(f"Copied ASR binary to {destination.relative_to(ROOT)}")


def setup_speech() -> None:
    uv = require("uv", "Install uv for your user; setup does not install global tools.")
    lock = ROOT / "speech" / "requirements.lock"
    if not lock.exists():
        raise SystemExit(f"Missing pinned speech requirements: {lock}")
    venv = ROOT / ".venv-speech"
    if not (venv / "bin" / "python").is_file():
        run([uv, "venv", str(venv), "--python", "3.12"])
    run([uv, "pip", "sync", str(lock), "--python", str(venv / "bin" / "python")])


def download_model() -> None:
    target = Path.home() / MODEL_RELATIVE
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        print("Pinned ASR model is already cached.")
        return
    temporary = target.with_suffix(target.suffix + ".partial")
    print("Downloading the explicitly requested pinned ASR model; this may be large.")
    try:
        with urllib.request.urlopen(MODEL_URL) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    env_file = ROOT / ".env"
    if env_file.is_file() and not env_file.is_symlink():
        configured = {
            line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
            for line in env_file.read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        }
        if not configured.get("ASR_MODEL_PATH"):
            with env_file.open("a", encoding="utf-8") as stream:
                stream.write(f"\nASR_MODEL_PATH={target}\n")
            env_file.chmod(0o600)
    print("Pinned ASR model downloaded to the user cache.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-model", action="store_true", help="download the pinned official HF model")
    args = parser.parse_args()
    uv = require("uv", "Install uv for your user.")
    require("ffmpeg", "Install ffmpeg for local recordings.")
    prepare_env()
    run([uv, "sync", "--locked", "--python", "3.12"])
    install_web()
    build_asr()
    setup_speech()
    if args.download_model:
        download_model()
    else:
        cached = Path.home() / MODEL_RELATIVE
        if not cached.is_file():
            print("ASR model not found in the pinned cache path; rerun with --download-model.")
        else:
            print("Pinned ASR model detected in the user cache.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
