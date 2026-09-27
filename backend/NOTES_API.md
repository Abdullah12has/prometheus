# Recording notes API

All routes require the operator session and CSRF header for writes. Audio stays under the configured private data directory. The API ignores client filenames and accepts raw audio bytes only.

`POST /api/notes` creates an `uploading` note:

```json
{"title":"Owner call","company_id":null}
```

Upload each recording chunk with `PUT /api/notes/{id}/chunks/{sequence}`. Sequence numbers start at zero; up to 4,802 chunks are accepted to support three-second browser recordings over four hours. Each request body is one raw chunk, up to 8 MiB, with a supported audio `Content-Type` (`audio/webm`, `audio/ogg`, `audio/wav`, `audio/x-wav`, `audio/mpeg`, `audio/mp4`, `audio/aac`, or `audio/flac`). Repeating a sequence with the same bytes and media type is idempotent. Different bytes for an occupied sequence return `409`. `GET /api/notes/{id}` returns `received_sequences` so a browser can resume an interrupted upload.

A single uploaded audio file uses the same path as one chunk at sequence `0`.

Finalize with `POST /api/notes/{id}/finalize`:

```json
{"chunk_count":2,"mime_type":"audio/webm;codecs=opus"}
```

The count and media type must match contiguous uploaded chunks. A complete recording is assembled to private disk, hashed, and queued as the persistent `note.transcribe` job. Recordings are limited to four hours and 1 GiB. `GET /api/notes/{id}/audio` serves the authorized original. `GET /api/notes` lists notes; `PATCH /api/notes/{id}` edits the title or `transcript_corrected` while preserving `transcript_raw`; `DELETE /api/notes/{id}` cancels a pending job and removes private files.

The persistent job runner calls `await process_note(note_id, sessionmaker, runtime, llm)`. It probes duration, normalizes to mono 16 kHz WAV chunks no longer than 30 seconds, saves incremental transcript segments and progress, then requests a structured summary. Each proposed fact or action is retained only if its quote occurs in the transcript; proposals remain unapproved and do not update company data. If the LLM is unavailable, the raw transcript still completes and remains available.
