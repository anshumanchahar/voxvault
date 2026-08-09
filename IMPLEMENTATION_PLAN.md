# VoxVault — Implementation Plan

Build log, milestones, validation, and honest limits for the STARFORGE 2026 (VoxForge) submission.

---

## 1. Track & Goal

**Track 1 — VoxForge.** Build a voice experience where voice is essential and Qdrant plays a meaningful retrieval role.

**Chosen route:** *Real-time voice agents* combined with *Voice beyond the screen* (hands-free operation).
**Problem chosen:** meetings produce information but no durable, queryable, speakable memory.

**Goal statement:** a working voice agent that (1) captures mic/system audio live, (2) transcribes and stores it as semantic meeting memory in Qdrant, (3) answers questions from that memory, (4) speaks every answer with Rime — all hands-free.

---

## 2. Milestones

### M0 — Decision & scaffold
- Selected VoxForge over DragonForge; picked the meeting-memory copilot idea.
- Set up monorepo layout (`app/`, `services/`, `models/`, `static/`, `templates/`), `.env.example`, `docker-compose.yml`, `requirements.txt`.
- Documented constraints (no secrets in the repo; AI-assisted coding disclosure).

### M1 — Core backend (REST + WS)
- FastAPI app: session CRUD, auth, memory read/clear, Q&A, system info, health.
- WebSocket `/ws/audio/{session_id}` accepting raw Float32 PCM.
- `whisper_service` (faster-whisper, `base`, int8, CPU + VAD).
- `qdrant_service`: auto-create collections, fastembed embeddings, per-user/persession filters, open-action metadata search.

### M2 — Voice control layer
- `command_service`: rule-based parser for natural memory commands —
  `remember / note / action item / todo / done / forget / i prefer`.
- Voice commands only fire on the **mic** channel (system audio is conversation, not control).
- Command confirmations are spoken.

### M3 — Speaker separation
- `diarize_service`: online timbre clustering (spectral centroid, spread, zero-crossing) → Speaker A/B/C/D on the system channel.
- Mic channel is always "You".

### M4 — Frontend single-page app
- Web Audio capture (16 kHz AudioContext, ScriptProcessor, analyser waveform).
- `getDisplayMedia` system capture with an audio-signal watchdog + "Share tab audio" hint.
- Live transcript cards, memory rail with replay buttons, query box, quick chips (Summarize / Action Items / Decisions).
- Views: Dashboard, Recent Meetings, Workspace, Settings; auth overlay; theme toggle.

### M5 — Rime TTS integration
- `rime_service` → `POST {RIME_API_URL}/rime-tts` (`modelId: coda`, `Accept: audio/wav`), cached to `app/static/tts/`.
- Wired into `/api/query` answers, `/api/tts`, and voice-command confirmations.
- No-key fallback serves `mock_audio.wav` so the demo doesn't hard-fail.

### M6 — Auth
- Supabase Auth mode (signup / password / /user) when `SUPABASE_URL` + `SUPABASE_ANON_KEY` are set.
- Deterministic local-dev fallback (`data/users.json`, salted SHA-256, HMAC tokens, 7-day TTL) for zero-dependency demos.

### M7 — Hardening & docs
- Ownership checks on delete/status-update; `user_id` filter on every Qdrant call.
- Client-side stale-cache note, WS close-code 4001 on bad tokens.
- This documentation set (README, ARCHITECTURE, API, IMPLEMENTATION_PLAN).

---

## 3. Validation Performed

| Check | What we ran | Result |
|-------|-------------|--------|
| Server boot | `uvicorn app.main:app` | FastAPI starts; lifespan boots services |
| REST smoke | signup → login → create session → ask query → system info | 200s; session + memory round-trip works |
| Static assets | `/static/styles.css`, `/static/app.js`, `/`, favicon | all 200 |
| Template render | Jinja flag `supabase_enabled` → `window.APP_CONFIG.supabaseEnabled` | renders true/false correctly |
| WebSocket channel | `python test_ws_channels.py` | signs up/logs in, creates a session, connects `/ws/audio/{session}` for `mic` + `sys` with a valid token; server accepts + streams fixture audio (a 440 Hz tone, so VAD suppresses any transcript — this proves auth + transport, not ASR) |
| Negative auth | run WS without a token | server rejects with close code **4001 (Unauthorized)** |
| Mock audio | `create_mock_audio.py` + `app/static/` fixtures | pipeline can be driven testably without a live mic |
| JS integrity | brace balance / no leftover Jinja in extracted `app.js` | clean after the CSS/JS-split refactor |

**To reproduce the automated WS check:**

```bash
docker compose up -d            # Qdrant
pip install -r requirements.txt
cp .env.example .env            # add RIME_API_KEY to hear TTS
uvicorn app.main:app --port 8000
# in a second terminal:
python test_ws_channels.py
```

> The strongest proof is the live web demo: sign up, start a session, speak, then ask a question and hear the Rime answer — no keyboard beyond sign-in.

---

## 4. AI-Assisted Coding Disclosure

Per the problem-statement rules, AI tooling (opencode) was used for:

- Scaffolding and structuring the codebase,
- Reviewing logic and catching edge cases (e.g. Pydantic v2 UUID default collision),
- Extracting inline HTML/CSS/JS into maintainable static assets,
- Producing this documentation.

**Human responsibility:** the team reviewed, tested, and can fully explain every component — code is not treated as a black box. Components are deliberately small and readable (single-purpose services, no magic).

---

## 5. What This Implementation Proves

- Voice can be the *primary* I/O for a meeting assistant (capture + speak answers).
- STT (local) → embedding → vector memory → semantic retrieval → TTS is a workable low-budget pipeline.
- Qdrant metadata filters and per-user scoping enable real multi-tenant memory.
- Voice-issued memory commands (remember/action/done/forget) are usable in practice.

## 6. What It Does NOT Prove (Limitations)

| Limit | Why it exists | Path beyond hackathon |
|-------|---------------|----------------------|
| No LLM reasoning | Keep answers deterministic, free, low-latency | swap `answer` builder for an LLM with a source-grounded prompt |
| Summaries are concatenations | deterministic + truncation to Rime-safe length | LLM summarizer |
| Timbre diarization only | no neural speaker models / heavy deps | pyannote.audio or cloud diarization |
| Local-dev auth fallback | demo without external services | use Supabase/SSO; drop `data/users.json` |
| Replay WAVs stored on local FS | simplest for a demo | object storage + signed URLs |
| Whisper `base` (EN, int8, CPU) | laptop-friendly latency | larger model / GPU or hosted STT for accuracy |
| No interruptions / barge-in | out of scope for the demo | half-duplex barge-in on query stream |

---

## 7. Suggested Next Steps (if time allowed)

1. Barge-in: cut Rime playback when voice activity resumes mid-answer.
2. LLM-grounded summarization of each session (store `summary` on the session record).
3. Hybrid Qdrant search (`BM25 + Dense`) for exact-name lookups.
4. Streaming STT per web-socket chunk instead of 10-chunk batches (lower latency).
5. E2E browser test (Playwright) recording the full voice→memory→Q&A story.

---

## 8. Links

- Live-ish demo docs: [README.md](README.md)
- Component/design detail: [ARCHITECTURE.md](ARCHITECTURE.md)
- Protocol reference: [API.md](API.md)