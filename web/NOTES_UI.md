# Recording notes

`NotesPanel` records browser microphone audio or uploads an existing audio file.
Each three-second microphone chunk is written to IndexedDB before uploading.
Files use byte slices smaller than the API's 8 MiB per-chunk limit. Captured chunks
remain on the device until the API acknowledges finalization.

After interruption, the operator can upload the remaining chunks and finalize
an explicitly labelled partial recording, or discard it. A new microphone session
never appends audio to an interrupted recording. The backend permits 4,802 upload
chunks and normalizes completed recordings into separate 30-second ASR segments.
Recordings are limited to four hours and 1 GiB.

The panel polls queued/processing notes, plays original audio, seeks to transcript
segments, and saves corrections separately from the original transcript. Summary
facts and actions remain proposals with transcript quotations. They never update
company records automatically.

API routes: POST/GET `/api/notes`, GET/PATCH/DELETE `/api/notes/{id}`,
PUT `/api/notes/{id}/chunks/{sequence}`, POST `/api/notes/{id}/finalize`, and
GET `/api/notes/{id}/audio`. All require the operator session; writes also require
CSRF. Audio and artifacts stay in private local storage.
