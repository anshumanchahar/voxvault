# VoxVault — Architecture

This document explains how VoxVault works: components, data model, request flows, and key design decisions.

---

## 1. System Overview

VoxVault is a **real-time meeting-memory voice agent** built for the STARFORGE 2026 VoxForge track. The core loop:

1. **Browser captures audio** (mic and/or system/tab audio) via the Web Audio API.
2. **Raw PCM flows over WebSocket** to FastAPI.
3. **faster-whisper** transcribes it locally on CPU.
4. Transcriptions and voice-issued **memory commands** are stored in **Qdrant** as semantic vectors (per user + session).
5. Questions retrieve the right memories via **embedding search in Qdrant**.
6. Every answer / confirmation is spoken with **Rime TTS**.

```
┌────────────────────────────────────────────────────────────────────┐
│                         BROWSER (index.html)                       │
│                                                                    │
│   Web Audio API                 app/static/app.js                 │
│   ┌───────────┐  Float32 PCM   ┌───────────────────────────────┐  │
│   │ mic       │ ──────────────▶│ AudioContext (16 kHz)        │  │
│   │ sys/tab   │   4096 frames  │ ScriptProcessor → WebSocket  │  │
│   └───────────┘  per callback  └──────────────┬────────────────┘  │
│        ▲                                     │ AudioContext       │
│        │                                     │ analyser → bars   │
│        └───── playback: /static/tts/*.wav    │ (waveform viz)    │
└──────────────────────────────────────────────┼─────────────────────┘
                                              │ ws://host/ws/audio/{session}?channel=mic
                                              ▼
┌──────────────────────────  FASTAPI (app/main.py)  ──────────────────────────┐
│                                                                             │
│  /ws/audio/{session_id}                     REST: /api/*                    │
│  ┌──────────────────────────┐               ┌────────────────────────────┐ │
│  │ buffer 10×4096 chunks    │               │ /api/auth  /api/sessions   │ │
│  │ → noise gate (RMS floor) │               │ /api/memory /api/query     │ │
│  │ → whisper_service        │               │ /api/actions /api/system   │ │
│  │ → command parser         │               │ /api/tts                   │ │
│  └──────────┬───────────────┘               └───────────┬────────────────┘ │
│             ▼                                          │                  │
│     store segment in Qdrant ─────  meeting_memory collection  ──────┐     │
│             │                                                       │     │
│             ▼                                                       ▼     │
│  send live: {type:transcription} / {type:memory_command}      semantic   │
│             │                                                  search     │
│             │                                                       │     │
└─────────────┼───────────────────────────────────────────────────────┼─────┘
              │                                                       │
              ▼                                                       ▼
    ┌───────────────────┐                                  ┌───────────────┐
    │ RimeService       │                                  │ Qdrant        │
    │ POST /rime-tts    │                                  │ C3 cosine     │
    │ modelId: coda     │  audio/wav → /static/tts/id.wav  │ 384-dims      │
    └───────────────────┘                                  └───────────────┘
```

### Component matrix

| Component | Responsibility | Where |
|-----------|----------------|-------|
| `index.html` | SPA shell, views (Dashboard / Meetings / Workspace / Settings) | `app/templates/index.html` |
| `app.js` | Web Audio capture, WS framing, live transcript render, Q&A, auth UI, theme | `app/static/app.js` |
| `main.py` | HTTP routes + WebSocket endpoint + pipeline orchestrator | `app/main.py` |
| `whisper_service` | STT via fast-whisper (`base`, int8, CPU) | `app/services/whisper_service.py` |
| `command_service` | Rule-based parser: remember / action / done / forget / preference | `app/services/command_service.py` |
| `diarize_service` | Online timbre clustering → Speaker A/B/C/D | `app/services/diarize_service.py` |
| `qdrant_service` | Embeddings (fastembed `all-MiniLM-L6-v2`), upsert, search, session records | `app/services/qdrant_service.py` |
| `rime_service` | Rime TTS synthesis + WAV caching | `app/services/rime_service.py` |
| `auth_service` | Supabase Auth or local-dev fallback, token issue/verify | `app/services/auth_service.py` |

---

## 2. Audio Pipeline (real-time)

### 2.1 Browser capture

- `navigator.mediaDevices.getUserMedia` → mic stream (echo cancellation + noise suppression on).
- `navigator.mediaDevices.getDisplayMedia` → system/tab share; only the **audio track** is kept, the tiny video track is stopped immediately.
- The AudioContext runs at **16 kHz**, forcing the browser to resample.
- Each `ScriptProcessorNode` callback with 4096-frame buffers sends a raw `Float32Array` over WebSocket. This raw PCM is chosen over `MediaRecorder`/WebM because WebM is compressed and Whisper cannot decode it.

### 2.2 Server-side processing

Per connection (`user:session:channel`):

1. Buffers are accumulated; Whisper runs every **10 chunks** (~0.5 s of audio).
2. **Noise gate** (`RMS_FLOOR = 0.01`): silent buffers are dropped before STT.
3. `whisper_service.transcribe()` returns text + confidence.
4. **Command parser** runs first:
   - A matched command (e.g. "action item: call the vendor") becomes a *typed memory*, not a transcript segment.
5. Otherwise the text becomes a *transcript segment* with a speaker label:
   - `mic` channel → **"You"**
   - `sys` channel → **diarize_service.assign()** labels Speaker A–D via timbre centroids.
6. Each utterance is saved as a replay WAV (`/static/audio/*.wav`) and upserted into Qdrant.
7. A JSON message is pushed back to the browser: `{type: "transcription", text, confidence, speaker}` or `{type: "memory_command", action, confirm}`.

### 2.3 System-audio watchdog

When capturing system audio, `sysWatchdog()` monitors the shared surface's RMS. If 5 s pass with no audio and 4 s of continuous silence, the UI shows a hint about enabling **"Share tab audio"** in Chrome's picker — a common real-world capture failure.

---

## 3. Data Model (Qdrant)

Two collections under `COLLECTION_NAME = "meeting_memory"` and `SESSIONS_COLLECTION = "sessions"`:

### meeting_memory (vector size 384, cosine)

| Payload field | Type | Purpose |
|---------------|------|---------|
| `text` | str | the utterance content |
| `speaker` | str | You / Speaker A–D |
| `timestamp` | ISO str | when it happened |
| `user_id` | str | **tenant key** — every query is scoped to user |
| `session_id` | str | session isolation |
| `title` | str | session title snapshot |
| `memory_type` | `transcript` \| `fact` \| `action` \| `preference` | semantic category |
| `status` | `open` \| `done` (actions only) | open-action tracking |
| `audio_url` | str | replay WAV path |

Vector = fastembed `all-MiniLM-L6-v2` embedding of `text`. If fastembed is unavailable, a deterministic `hash_embedding` fallback keeps the system functional (weaker semantics).

### sessions (vector size 1)

Payload: `user_id`, `session_id`, `title`, `started_at`, `updated_at`, `segment_count`, `summary`. Kept as a point so sessions can be listed/filtered per user without scanning the heavy memory collection.

### Key invariants

- **Every** search/scroll carries a `user_id` filter (and optional `session_id`) — multi-tenant isolation.
- `delete_segment` / `update_segment_status` first retrieve the point and verify `payload.user_id` before mutating (ownership check).
- `delete_session` removes the session record **and** all its segments.

---

## 4. Registration & Tagging

VoxVault maps voice output onto the problem statement's required axes:

| Requirement | Where VoxVault delivers |
|-------------|-------------------------|
| **Rime is essential** | All assistant answers, summaries, action confirmations are synthesized with Rime (`modelId: coda`) and played back. Without TTS the hands-free loop doesn't exist. |
| **Qdrant has a meaningful role** | Semantic Q&A ("who owns the API redesign?") depends on embedding retrieval; open-action status (`status == "open"`) and per-user session filtering are Qdrant metadata filters. |
| **Real problem** | Meeting memory: capture → durable, queryable, speakable record. |
| **Challenging production problem** | Memory & continuity across sessions; real-time low-latency STT; system-audio capture watchdog. |
| **Working proof** | Live voice→memory→Q&A, plus `test_ws_channels.py` exercising the WS path end-to-end. |

---

## 5. REST Request Flows

### 5.1 Auth

```
POST /api/auth/signup | login      → AuthResponse {token, user}
GET  /api/auth/me                  → validate stored token, returns user
```

- Supabase mode: proxy to `/auth/v1/signup`, `/auth/v1/token?grant_type=password`, `/auth/v1/user`.
- Fallback: password is salted (`secrets.token_hex(8)`) and hashed (SHA-256 `salt:password`); the token is an HMAC-signed payload (`{uid, exp}`) valid 7 days. Plaintext passwords are never stored.

### 5.2 Sessions

```
POST   /api/sessions                      → create (title default "Meeting <date>")
GET    /api/sessions                      → list for user, newest first
GET    /api/sessions/{id}                 → detail + segment_count
PATCH  /api/sessions/{id}                 → rename
DELETE /api/sessions/{id}                 → delete session + its segments
```

### 5.3 Memory Q&A

```
GET  /api/memory?session_id=... → all segments for user (optionally per session)
POST /api/query                 → ask a question, get answer + Rime audio
```

`/api/query` behavior:
- Recognized keyword intents (`summarize`, `action items`, `decisions`) → deterministic aggregation.
- A **parseable memory command** (e.g. typed "action item: X") → executes that command and speaks its confirmation.
- Otherwise → embedding search over the user's segments (`top_k=5`), context joined and truncated to 800 chars, spoken via Rime.

### 5.4 System + TTS

```
GET  /api/system  → model versions, vector count, Qdrant availability, auth provider, Rime key presence
POST /api/tts     → raw synthesis (text → /static/tts/*.wav)
GET  /health      → liveness
```

---

## 6. WebSocket Protocol

```
WS /ws/audio/{session_id}?channel=mic|sys|both&token=<jwt-or-local-token>
```

- Client sends binary **Float32 PCM @ 16 kHz mono**.
- Server replies JSON:

```jsonc
// transcription (normal speech)
{ "type": "transcription", "text": "...", "confidence": 0.93,
  "speaker": "Speaker A", "channel": "sys",
  "session_id": "...", "segment_id": "...", "audio_url": "/static/audio/x.wav" }

// memory command accepted
{ "type": "memory_command", "action": "remember|action|done|forget",
  "memory_id": "...", "text": "...", "confirm": "Noted as an open action item: ..." }
```

- Close code `4001` = unauthorized (bad/missing token).

---

## 7. QoS, Recovery & Edge Handling

| Concern | Mechanism |
|---------|-----------|
| Multi-tenant isolation | `user_id` filter on every Qdrant operation + ownership checks on delete/update |
| Qdrant downtime | `_available` flag; API returns empty results with clear label ("Offline") instead of crashing |
| No Rime key | `rime_service` returns a mock WAV; UI shows "mock (no key)" |
| Invalid session | `currentSessionId` guard on the client; WS closes on unauthorized token |
| Voice command false positives | Parser ordered most-specific-first; only runs on **mic** channel (system audio can't trigger commands) |
| STT silence | VAD filter (`min_silence_duration_ms=500`) + server noise gate |
| Latency budget | Whisper `base` int8 on CPU with 4096-frame (~0.5 s) batching |

---

## 8. Frontend Views

- **Dashboard** — live transcript feed (oldest→newest), speaker chips, memory rail, query box, quick chips, Rime audio player.
- **Recent Meetings** — session list with rename/delete/open; re-opening restores memory + transcript.
- **Workspace** — pipeline status: vector count, STT/TTS models, Qdrant availability.
- **Settings** — theme toggle, speaker/retention/language prefs, "Clear All Memories".
- **Auth overlay** — login/signup with Supabase-or-local badge; theme persists in `localStorage`.

---

## 9. Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Raw Float32 PCM over WS instead of MediaRecorder/WebM | WebM is compressed; Whisper needs raw PCM (16 kHz mono) |
| `MeetingSegment.id = Field(default_factory=uuid4)` | In Pydantic v2 a default UUID is evaluated once — a constant would collide in Qdrant |
| fastembed `all-MiniLM-L6-v2` (384-d, cosine) | Real semantic similarity; hash fallback only for degraded mode |
| Whisper GPU-free (`base`, int8) | Runs on a laptop CPU for a live demo; no CUDA dependency |
| per-user+session payload tagging | Multi-tenant memory that survives logout/login |
| Commands on mic channel only | Prevents a *meeting* (system audio) from mutating your memory store |
| Diarization as timbre centroids | Zero heavy deps; online; good enough for A/B/C/D on clean audio |

See [README.md](README.md) for setup, [API.md](API.md) for the protocol reference, and [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for build milestones and validation.