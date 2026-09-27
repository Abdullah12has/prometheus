# Local setup, backup, and restore

Setup uses the installed package managers and downloads pinned dependencies as needed. Backup and restore also use the project's cryptography dependency. The scripts do not install global tools or run automatically.

## Prerequisites

Install these tools for your user account before running setup:

- Python 3.12
- `uv`
- Node.js/npm
- Rust/Cargo
- Docker Desktop (database and search)
- ffmpeg (recording normalization)

The backend architecture files expected by the scripts must be present in the
repository: `native/asr`, `speech/requirements.lock`, and the repository's
Docker Compose database service. Missing prerequisites produce an actionable
error.

## Setup

From the repository root:

```sh
python3 scripts/setup.py
```

This runs `uv sync --locked --python 3.12` and `npm ci` in `web`, builds the Rust ASR release binary and copies
it to `data/bin/native-asr-bridge`, and creates an independent `.venv-speech`
from the pinned speech lock file. Existing `.env` values are preserved.
Missing database, session, and admin credentials are generated with the
operating system CSPRNG; secrets are never printed. A missing `DATABASE_URL`
is generated from the Compose database credentials with URL-escaped password
syntax. Setup fills `TTS_PYTHON` with the speech virtual environment path when
it is blank, and fills `ASR_MODEL_PATH` only when the pinned model is already
cached. It enables the background worker unless `WORKER_ENABLED` is already
set (including an explicit `false`). Existing non-empty settings are
preserved. `.env` is required to be git-ignored and is secured as mode `0600`.

Setup does not download the model by default. It recognizes this exact cache
path:

`~/.cache/huggingface/hub/models--handy-computer--nemotron-3.5-asr-streaming-0.6b-gguf/snapshots/6d44e540bc31b0de1dbe174a3cea87f53a7f22fb/nemotron-3.5-asr-streaming-0.6b-Q8_0.gguf`

To explicitly download that pinned official Hugging Face artifact:

```sh
python3 scripts/setup.py --download-model
```

Stop the web/API before backup so database rows and local files belong to the same snapshot. Keep database/search containers running.

## Backup

Run backup and restore through `uv` so they use the project's installed
`cryptography` dependency:

```sh
uv run python scripts/backup.py
```

The default destination is `~/.permetheus-backups/<UTC timestamp>`, outside the
repository. A custom destination inside the repository must already be covered
by its ignore rules; otherwise the script refuses to write. Destinations must
be new or empty and cannot be symlinks. The destination and files are mode
`0700`/`0600`.

The backup includes the application `data/` tree and a `pg_dump` produced
through the Compose `db` service. `data/bin`, `data/logs`, and `data/tmp` are
excluded. Use `--postgres-service NAME` only if the Compose database service
is renamed. If `.env` exists, backup prompts twice for a passphrase of at least
12 characters and writes it as `.env.enc` using Fernet authenticated
encryption with a random salt and Scrypt-derived key. The passphrase is not
saved or displayed. Every backup file is listed in `manifest.json` with a
SHA-256 checksum. Backups are confidential.

## Restore

Inspection is the safe default. It validates the manifest, rejects symlinks
and path traversal, and prints backup filenames only:

```sh
uv run python scripts/restore.py /path/to/backup
uv run python scripts/restore.py /path/to/backup --inspect
```

Replacement requires a verified backup, an explicit `--confirm-replace`, and
the local app stopped on ports 4310 and 4311. Database and search containers
may remain running so `psql` can import the dump:

```sh
uv run python scripts/restore.py /path/to/backup \
  --confirm-replace
```

The SQL import runs with `ON_ERROR_STOP` in a single transaction. All backup
files are copied to private staging and rechecked before the database or local
files are changed. To also decrypt and replace `.env`, pass `--restore-env`;
restore prompts for the backup passphrase and writes `.env` with mode `0600`.
Without that option the current `.env` is left alone. Restore never deletes
the backup. Review the verified file list and stop the application before
confirming replacement.
