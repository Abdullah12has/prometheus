# Voice API (browser conversations)

All speech runs locally: streaming ASR and TTS come from the speech runtime. The language model only
sees transcript text. Nothing falls back to cloud speech or browser speech APIs. Phone calling is
deferred, so the UI should label the two channels separately: **Browser conversation** (available)
and **Phone calling unavailable**.

Authentication uses the operator session cookie (`permetheus_session`, path `/api`). Unsafe HTTP
methods also need `X-CSRF-Token`. Errors look like `{"error": {"code", "message", "details?"}}`.
Responses never include filesystem paths or secrets.

## Integration (parent app)

```python
from permetheus import voice          # import before init_db so the tables get created
app.include_router(voice.router)
app.state.speech = SpeechRuntime(...)  # holds asr_lock for the length of a live call
app.state.llm = LanguageModel(base_url, key, model)
```

Tables: `voice_agents` and `voice_sessions`. They are created by `Base.metadata.create_all`.

## HTTP

| Method | Path | Notes |
|---|---|---|
| GET | `/api/voice/capabilities` | `{browser: {available, label, speech_runtime, language_model}, phone: {available: false, label, reason}}` |
| GET | `/api/voice/agents` | List of agents |
| POST | `/api/voice/agents` | `{name, introduction, instructions?, language: "en", max_duration_seconds: 30–900}` → 201 |
| PATCH | `/api/voice/agents/{id}` | Any of the fields above, plus `voice: "default"` to switch back to the bundled voice |
| POST | `/api/voice/agents/{id}/voice-sample` | multipart, see below |
| GET | `/api/voice/agents/{id}/preview` | `audio/wav` of the introduction in the agent's voice. Usable as `<audio src>` (same-origin cookie). Returns 409 while a call is active |
| POST | `/api/voice/browser-sessions` | `{agent_id, company_id?, recording_consent: bool}` (consent is required) → 201 ticket |
| GET | `/api/voice/sessions?agent_id=` | Newest 200 sessions, without transcripts |
| GET | `/api/voice/sessions/{id}` | Detail: transcript, turn statuses, `proposed_outcome` |

The `introduction` must tell the other person they are talking to an AI: it has to contain "AI",
"A.I." or "artificial intelligence", otherwise the request fails with 422. A fixed system prompt
also forbids claiming to be human, impersonating anyone, or making commitments. Operator
`instructions` are appended after that prompt.

Agent `voice` field:
`{kind: "default", label: "Bundled default voice (not cloned)", authorization: null}` or
`{kind: "cloned", label: "Authorized cloned voice", authorization: {voice_owner_name, basis, authorized_at, sample_seconds, sample_sha256}}`.

### Voice sample (cloning)

Send `multipart/form-data` with these fields:

- `sample`: an audio file of at most 10 MB and 3–30 s. Any format ffmpeg can decode.
- `voice_owner_name`: 1–200 characters.
- `owner_authorization`: must be exactly `true`.
- `authorization_basis`: `self` or `written_permission`.

The server reads the body with a size limit, then decodes it through ffmpeg (pipes only, 30 s
timeout, output capped at 31 s) to 24 kHz mono. It then calls `runtime.clone_voice` to write
`data/voices/<agent>/<id>.safetensors`. The decoded WAV is deleted after cloning.

Errors:

| Status | Code |
|---|---|
| 400 | `authorization_required`, `sample_required` |
| 413 | `too_large` |
| 415 | `unsupported_media_type` |
| 422 | `invalid_audio`, `sample_too_long`, `sample_too_short` |
| 409 | `voice_busy` (a call is active) |
| 503 | `voice_clone_failed`, `ffmpeg_unavailable`, `speech_unavailable` |

### Browser session ticket

`POST /api/voice/browser-sessions` returns:

```json
{"session_id": "…", "ticket": "…", "expires_at": "…", "websocket_path": "/api/voice/browser/<id>",
 "channel": "browser", "channel_label": "Browser conversation",
 "audio_input": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1, "max_frame_samples": 16000}}
```

The ticket is valid for 60 s, works only once, and only for this session. Only its SHA-256 is stored.
Returns 503 `llm_unavailable` or `speech_unavailable` when a dependency is missing.

## WebSocket `/api/voice/browser/{session_id}`

Opening the socket:

1. The handshake needs an allowed `Origin` and a valid session cookie. Otherwise the socket closes
   before accept with code **4403** (origin) or **4401** (cookie).
2. The first message must be `{"type":"auth","ticket":"…"}` and must arrive within 10 s. Tickets in
   the query string are ignored. A missing ticket gets error `ticket_required`, then close 4401. An
   invalid, expired or reused ticket gets `invalid_ticket`, then close 4401.
3. Only one call can run at a time. If another is active the client gets `voice_busy` and close
   **4409**, and the session is marked `failed`.
4. The server sends `ready`, then immediately speaks the agent's introduction as the first utterance.

Client → server:

| Message | Meaning |
|---|---|
| binary | PCM16 little-endian, mono, 16 kHz, 1–16000 samples per frame. Other frames get the non-fatal error `bad_frame` |
| `{"type":"end_turn"}` | The user finished speaking (client VAD or push-to-talk release). The server finalizes ASR and replies |
| `{"type":"interrupt"}` | Barge-in. The server stops the current utterance and sends `clear` |
| `{"type":"playback_ack","utterance_id":"…"}` | The client finished playing that utterance |
| `{"type":"stop"}` | End the call |

Server → client:

| Message | Meaning |
|---|---|
| `{"type":"ready","session_id","channel":"browser","channel_label","input_sample_rate":16000,"max_frame_samples":16000,"recording":bool,"max_duration_seconds"}` | The call is live |
| `{"type":"partial","turn_id","text","committed","tentative"}` | Streaming ASR hypothesis; `committed` is stable text, `tentative` is revisable, and `text` is their concatenation |
| `{"type":"final","turn_id","text"}` | Finalized user turn. `turn_id` is null and `text` is empty if no audio was sent |
| `{"type":"agent_text","utterance_id","text"}` | The sentence about to be spoken (captions) |
| `{"type":"audio","utterance_id","seq","sample_rate","pcm"}` | base64 PCM16 LE mono at `sample_rate` (24000 with the bundled model). Queue by `seq` |
| `{"type":"agent_done","utterance_id"}` | All audio for the utterance has been sent. Reply with `playback_ack` once it has played |
| `{"type":"clear","utterance_id"}` | Drop all queued or playing audio for that utterance right away |
| `{"type":"error","code","message","fatal"}` | Non-fatal codes: `bad_frame`, `bad_message`, `audio_overflow`, `reply_failed`. Fatal codes: `asr_failed`, `server_error`, plus the auth codes above |
| `{"type":"ended","reason"}` | Followed by close 1000. Reasons: `client_stop`, `disconnect`, `max_duration`, `speech_error`, `error` |

Client loop:

1. Capture mic audio with an AudioWorklet, resample to 16 kHz Int16, and send frames of about
   100–200 ms.
2. Run VAD locally. When speech starts while agent audio is playing, send `interrupt` and flush
   playback on `clear`. When speech stops, send `end_turn`.
3. Play `audio` chunks in order through Web Audio. After `agent_done`, once the last chunk has
   played, send `playback_ack`.

Limits:

- Each user turn is capped at 30 s. At the cap the server finalizes the turn itself (a limit, not a VAD).
- The ASR backlog holds at most 20 s of audio. Beyond that, frames are refused with `audio_overflow`.
- Replies are at most about 800 characters.
- A call lasts at most the agent's `max_duration_seconds`.

### Transcript and consent

A transcript is written only if `recording_consent` is true. Otherwise nothing is written, both
during and after the call, and `/sessions/{id}` returns `transcript: []` with a `transcript_note`.

Each turn is stored as `{id, role: "user"|"agent", text, status, at}`. User turns have status
`final`. Agent turns use these statuses:

| Status | Meaning |
|---|---|
| `played` | The client acknowledged playback |
| `interrupted` | Barge-in after some audio was sent |
| `unplayed` | Cancelled before any audio was sent |
| `unconfirmed` | Audio was sent but no ack arrived before the call ended |
| `failed` | The language model or TTS failed |

For an interrupted turn, `text` is what had been generated up to that point, not what the person
actually heard.

`proposed_outcome` is filled in after a recorded call if the person spoke:
`{review_status: "proposed", source: "model", note, summary, stated_interest, follow_ups}`. It is
never applied to company records: seller intent, emails and calls are left untouched.
