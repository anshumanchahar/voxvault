# VoxVault — API Reference

Base URL `http://localhost:8000`. All `/api/*` routes (except auth) require an `Authorization: Bearer <token>` header.

Interactive docs: FastAPI auto-serves Swagger at `/docs`.

---

## Table of Contents

- [Auth](#1-auth)
- [Sessions](#2-sessions)
- [Memory](#3-memory)
- [Actions](#4-actions)
- [Query (Q&A)](#5-query-qa)
- [TTS](#6-tts)
- [System & Health](#7-system--health)
- [WebSocket Audio Channel](#8-websocket-audio-channel)
- [Common Error Shape](#9-common-error-shape)

---

## 1. Auth

### POST `/api/auth/signup`

Create an account.

```jsonc
// request
{ "email": "me@example.com", "password": "hunter22" }
// 200 response
{ "token": "...", "user": { "id": "...", "email": "me@example.com" } }
```

- Email validated (format + ≤254 chars, lowercased); password ≥ 6 chars → `422` with detail otherwise.
- **409** if email already registered.
- **400** special case: Supabase has email confirmation ON and signup still returns a token-less user → detail explains how to fix (disable "Confirm email" or verify the link).

### POST `/api/auth/login`

```jsonc
// request
{ "email": "me@example.com", "password": "hunter22" }
// 200
{ "token": "...", "user": { "id": "...", "email": "..." } }
```

→ **401** on bad credentials.

### GET `/api/auth/me`

Validates a stored token.

```jsonc
// header: Authorization: Bearer <token>
// 200
{ "token": "...", "user": { "id": "...", "email": "..." } }
```

→ **401** if the token is missing/expired/invalid.

---

## 2. Sessions

Sessions group a recording + its memory. Records live in Qdrant `sessions` collection.

### POST `/api/sessions`

```jsonc
// request (optional)
{ "title": "Q3 Planning" }
// 200
{
  "session_id": "<uuid>",
  "title": "Meeting Aug 10, 14:30",   // or the provided title
  "started_at": "...", "updated_at": "...", "segment_count": 0
}
```

### GET `/api/sessions`

Lists the user's sessions, newest-first, with `segment_count`.

### GET `/api/sessions/{session_id}`

→ **404** if the session doesn't belong to the user.

### PATCH `/api/sessions/{session_id}`

```jsonc
{ "title": "Renamed Session" }
```

→ **404** if not found.

### DELETE `/api/sessions/{session_id}`

Deletes the session **and all its segments**.

```jsonc
{ "deleted": true }
```

**Authorization note:** session IDs are UUIDs and scoped per-user; deleting someone else's session simply 404s.

---

## 3. Memory

### GET `/api/memory?session_id=<opt>`

All segments for the user (optionally filtered to a session).

```jsonc
{
  "segments": [
    {
      "id": "<uuid>",
      "text": "Who owns the API redesign?",
      "speaker": "Speaker A",
      "timestamp": "2026-08-10T09:00:00Z",
      "session_id": "...", "user_id": "...", "title": "Meeting ...",
      "memory_type": "transcript",       // transcript | fact | action | preference
      "status": null,                     // null | open | done
      "audio_url": "/static/audio/<uuid>.wav"
    }
  ],
  "session_id": "..."
}
```

### DELETE `/api/memory?session_id=<opt>`

`{ "cleared": true }` — clears the user's memory (optionally scoped to one session).

---

## 4. Actions

### GET `/api/actions/open`

```jsonc
[
  { "id": "<uuid>", "text": "Call the vendor by Friday",
    "memory_type": "action", "status": "open",
    "session_id": "...", "title": "...", "timestamp": "..." }
]
```

The backend drives this from `memory_type == "action" && status == "open"`.

---

## 5. Query (Q&A)

### POST `/api/query`

```jsonc
// request
{ "query": "who owns the api redesign?", "top_k": 5, "session_id": "<optional>" }
// 200
{
  "answer": "Based on the meeting: Speaker A: I own the API redesign...",
  "sources": [ /* MeetingSegment[] */ ],
  "audio_url": "/static/tts/tts_<uuid>.wav"
}
```

Behavior branches:

| Input | Branch |
|-------|--------|
| `summarize` / `summary` / `summarize the meeting` | deterministic speaker-grouped concatenation, truncated to 800 chars |
| `action items` / `open actions` / `what's pending` | lists all open actions |
| parseable memory command (`action item: X`, `remember X`, `done: X`, `forget X`) | executes the command and speaks its confirmation |
| anything else | embedding search (`top_k`), top segments joined → answer |

Every branch returns a Rime TTS `audio_url` (or mock when no RIME_API_KEY).

---

## 6. TTS

### POST `/api/tts`

```jsonc
// request
{ "text": "The launch is in June.", "voice": "astra", "speed": 1.0 }
// 200
{ "audio_url": "/static/tts/tts_<uuid>.wav", "duration_ms": 812.5 }
```

- Sends `{text, speaker: voice, modelId: "coda", lang: "en"}` to `{RIME_API_URL}/rime-tts`.
- Caches each synthesis as a WAV under `app/static/tts/`.
- Without `RIME_API_KEY` returns the mock audio URL.

---

## 7. System & Health

### GET `/api/system`

```jsonc
{
  "user": { "id": "...", "email": "..." },
  "stt": { "model": "whisper-base.en" },
  "vector": {
    "qdrant_url": "http://localhost:6333",
    "collection": "meeting_memory",
    "vectors": 12, "sessions": 2,
    "available": true
  },
  "tts": { "provider": "Rime", "model": "coda", "has_api_key": true },
  "auth": { "provider": "Supabase" }   // or "local-dev"
}
```

### GET `/health`

`{ "status": "ok", "service": "voxvault" }` — no auth required.

---

## 8. WebSocket Audio Channel

```
WS /ws/audio/{session_id}?channel=mic|sys|both&token=<token>
```

- Requires a valid Bearer-style token in the `token` query param; otherwise the socket closes with **4001 (Unauthorized)**.
- Client sends binary **raw Float32 PCM, 16 kHz, mono, 4096 frames/callback**.

Server → client JSON messages:

```jsonc
// normal transcription
{ "type": "transcription",
  "text": "The deadline is Friday",
  "confidence": 0.94,
  "speaker": "You",            // You (mic) or Speaker A-D (sys, diarized)
  "channel": "mic",
  "session_id": "...",
  "segment_id": "<uuid>",
  "audio_url": "/static/audio/<uuid>.wav" }

// memory command accepted
{ "type": "memory_command",
  "action": "remember",        // remember | action | done | forget
  "memory_id": "<uuid>",
  "text": "The deadline is Friday",
  "confirm": "Remembered: The deadline is Friday" }
```

Server-side flow per 10 chunks: noise gate → whisper → command parser → (diarize on sys) → store → push.

---

## 9. Common Error Shape

Validation/data errors use FastAPI's standard shape:

```jsonc
{ "detail": "Not authenticated" }                // 401
{ "detail": "Session not found" }                // 404
{ "detail": [{ "loc": ["body","password"], "msg": "Password must be at least 6 characters", "type": "value_error" }] }  // 422
```

Auth endpoints return `{ "token": "...", "user": { "id": "...", "email": "..." } }`; memory returns arrays/objects as shown above.

---

See [ARCHITECTURE.md](ARCHITECTURE.md) for how these endpoints fit the pipeline, and [README.md](README.md) to run everything.