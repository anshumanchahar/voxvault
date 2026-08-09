# VoxVault — Voice Memory Assistant

**STARFORGE 2026 · Track 1 (VoxForge)** — a real-time voice agent that joins a meeting, transcribes it, builds semantic memory, and answers spoken or typed questions from that memory.

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-latest-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Rime](https://img.shields.io/badge/TTS-Rime-FFB700)](https://docs.rime.ai)
[![Qdrant](https://img.shields.io/badge/Vector%20DB-Qdrant-D14013?logo=qdrant&logoColor=white)](https://qdrant.tech)
[![whisper](https://img.shields.io/badge/STT-faster--whisper-1E90FF)](https://github.com/SYSTRAN/faster-whisper)

> Why voice? A meeting assistant lives where a keyboard can't: mid-conversation. You talk to VoxVault — it talks back. Turning this into a text-only app would lose the core loop of hands-free meeting memory.

---

## Table of Contents

- [The Problem](#the-problem)
- [The Solution](#the-solution)
- [Architecture](#architecture)
- [Folder Structure](#folder-structure)
- [Quick Start](#quick-start)
- [Usage](#usage)
- [Voice Commands](#voice-commands)
- [API](#api)
- [Development & Implementation Plan](#development--implementation-plan)
- [Working Proof](#working-proof)
- [Limitations](#limitations)
- [Team & Contributions](#team--contributions)
- [Submission Checklist](#submission-checklist)
- [License](#license)

---

## The Problem

Meetings produce a lot of information and almost no durable record. Notes are incomplete, decisions are forgotten, and "who owns X?" becomes a Slack archaeology dig. Existing tools record, but they don't **remember** in a form you can ask questions against.

Per the STARFORGE 2026 problem statement, a strong VoxForge entry must:

- Make **voice essential** to the experience — not a chatbot with a microphone
- Give **Qdrant a meaningful role** — retrieval, memory, routing, or evaluation
- Solve a real problem end-to-end
- Show at least one challenging production problem: here, **memory and continuity across conversations** and **low-latency voice interaction**

## The Solution

**VoxVault** is a hands-free meeting copilot:

1. It captures live audio from your **microphone and/or system audio** (tab/screen share) directly in the browser.
2. **faster-whisper** transcribes speech in near-real-time over a WebSocket.
3. Every utterance (with speaker labels) is embedded and stored in **Qdrant** as a *meeting segment*.
4. You ask questions — **out loud or by typing** — and VoxVault retrieves the semantically relevant memories and **speaks the answer with Rime TTS**.

Voice is essential: the entire capture flow and spoken-answer loop run hands-free. Qdrant is essential: "who owns the API redesign?" only works because embeddings retrieve the *right* memory despite different wording.

**Track alignment:**
- **Route:** *Real-time voice agents* + *Voice beyond the screen* (hands-free operation)
- **Production problems addressed:** memory and continuity across conversations; low-latency voice interaction
- **Rime usage:** all assistant answers and memory-action confirmations are synthesized and played back — TTS is the primary output channel, not a decorative extra
- **Qdrant usage:** semantic vector memory, per-user/per-session filtering, metadata filtering (memory type, open status), and session grouping

---

## Architecture

```
                                    ┌────────────────────────────────────────────┐
   Browser (Web Audio API)          │              FastAPI (uvicorn)             │
 ┌──────────────────────────┐       │                                           │
 │ mic ─┐                   │  WS   │  /ws/audio/{session}                      │
 │ sys ─┴─ 16kHz PCM ───────┼──────▶│   ┌─────────────┐  chunk        ┌───────┐  │
 │        (Raw Float32)     │       │   │ noise gate  │───▶ whisper ──▶│parse  │  │
 │                          │       │   └─────────────┘    (STT)      │command│  │
 │ live transcript cards    │       │                              └───────┘  │
 │ waveform / timer         │       │        │ transcript / segments          │
 │ assistant answers ──────▶│  REST │        ▼                                 │
 │ (Rime audio playback)    ┼───────┼─▶ /api/query /api/memory ...             │
 └──────────────────────────┘       │        │                                 │
                                    │        ▼                                 │
                                    │  Qdrant (vector memory)                  │
                                    │  ┌──────────────────────────────┐        │
                                    │  │ meeting_memory collection    │        │
                                    │  │  segments (id, text, emb)    │        │
                                    │  │  payload: user, session,     │        │
                                    │  │  speaker, memory_type, status│        │
                                    │  │  + sessions & user_activity  │        │
                                    │  └──────────────────────────────┘        │
                                    │        ▲                                 │
                                    │        │ answer text                     │
                                    │  ┌─────▼──────┐    ┌─────────────┐       │
                                    │  │ rime TTS   │    │ Rime API    │       │
                                    │  └────────────┘───▶│(cloud)      │       │
                                    └──────────────────────────────────────────┘
```

**Data flow for a live utterance:**

```
audio chunk ──▶ noise gate ──▶ faster-whisper (STT) ──▶ command parser
                                                            │
              ┌───────────────────────┬─────────────────────┤
   (transcript)                (memory command)      (command actions)
        │                              │                    │
        ▼                              ▼                    ▼
  speaker diarize ──▶ store in    store as fact /   done/forget →
  segment (You / Speaker A-D)     action / preference    mutate Qdrant
        │                              │                    │
        ▼                              ▼                    │
  push live card to browser      confirm chip (Rime TTS)     │
        │                                                     │
        └──────────────▶  all segments land in Qdrant for semantic Q&A
```

See **[ARCHITECTURE.md](ARCHITECTURE.md)** for the full component breakdown, request flows, and design decisions.
See **[API.md](API.md)** for the REST/WebSocket reference.

---

## Folder Structure

```
.
├── app/
│   ├── main.py                 # FastAPI app: REST endpoints + WebSocket audio channel
│   ├── config.py               # pydantic-settings config (reads .env)
│   ├── models/
│   │   └── schemas.py          # Pydantic models (segments, sessions, requests/responses)
│   ├── services/
│   │   ├── whisper_service.py  # faster-whisper STT (local, ggml-base.en)
│   │   ├── qdrant_service.py   # Qdrant client + fastembed embeddings + search/memory ops
│   │   ├── rime_service.py     # Rime TTS synthesis + audio caching
│   │   ├── diarize_service.py  # lightweight timbre-based speaker clustering
│   │   ├── command_service.py  # rule-based memory-command parser
│   │   └── auth_service.py     # Supabase Auth (or local-dev fallback) + tokens
│   ├── templates/
│   │   └── index.html          # single-page demo UI (Jinja template)
│   └── static/
│       ├── styles.css          # neobrutalism theme
│       ├── app.js              # all client logic (Web Audio → WS, view rendering, auth)
│       ├── favicon/            # PWA/favicon assets + site.webmanifest
│       └── tts/                # generated Rime audio (gitignored)
├── data/
│   └── users.json              # local-dev fallback user store (auto-created, gitignored)
├── models/                     # downloaded whisper/embedding models (gitignored)
├── create_mock_audio.py        # dev helper: generates test audio files
├── docker-compose.yml          # Qdrant container
├── requirements.txt
├── .env.example                # template — NEVER commit real .env
├── AGENTS.md                   # agent/contributor guidance for this repo
├── StarForge_Problem_Statement.pdf
├── STARFORGE 2026 — HACKATHON PROBLEM STATEMENT.md
├── STARFORGE 2026 - General Instructions.md
├── Problem Statements & Resources.md
├── README.md
├── ARCHITECTURE.md
├── IMPLEMENTATION_PLAN.md
└── API.md
```

> **Secrets:** `.env` and `data/users.json` (local-dev auth store) are gitignored. Only `.env.example` (placeholders) is committed. `data/users.json` is auto-created on first signup in local-dev auth mode.

---

## Quick Start

### Prerequisites

- **Python 3.12+** (Python 3.14 works)
- **Docker** (for local Qdrant) — or a [Qdrant Cloud](https://qdrant.tech) cluster URL in `.env`
- **Rime API key** — from [https://users.rime.ai](https://users.rime.ai) (Rime Essentials covers ~$450 in credits for this hackathon)
- **Chrome/Edge** (Chromium required for `getDisplayMedia` + tab-audio capture)
- ~1 GB disk for local STT/embedding model downloads on first run

### 1. Start Qdrant

```bash
docker compose up -d
# verify: http://localhost:6333/dashboard
```

### 2. Install dependencies

```bash
python -m venv .venv
# Windows: .\.venv\Scripts\activate   |   macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env` — minimum for a working demo with TTS:

```bash
RIME_API_KEY=your_rime_api_key_here        # required for real TTS
QDRANT_URL=http://localhost:6333           # local Docker default
QDRANT_API_KEY=                            # only for Qdrant Cloud
SUPABASE_URL=                              # leave empty → local-dev auth fallback
SUPABASE_ANON_KEY=
```

### 4. Optional: enable Supabase Auth

Set both `SUPABASE_URL` and `SUPABASE_ANON_KEY` to store credentials in Supabase Auth:

1. Create a project at [supabase.com](https://supabase.com).
2. **Authentication → Providers → Email**: keep Email enabled and turn **OFF** "Confirm email" (so signup returns an access token instantly — important for a smooth demo).
3. **Project Settings → API**: copy *Project URL* → `SUPABASE_URL`, *anon public* key → `SUPABASE_ANON_KEY`.
4. Restart the server. The login card shows **"Credentials: Supabase Auth"** when connected.

Without these, VoxVault uses a local-dev store (`data/users.json`, hashed passwords) so the demo runs with zero external accounts. Passwords are never stored in plaintext in either mode.

### 5. Run the server

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Models (`ggml-base.en.bin`, embedding model) download automatically on first use.

### 6. Open the demo

Visit `http://localhost:8000` in **Chrome/Edge** and create an account.

---

## Usage

1. **Sign up / log in** (credentials → Supabase Auth or local-dev fallback)
2. Click **New Session** — a fresh, isolated session starts, and recording begins
3. Pick an **audio source** in the top bar:
   - **Microphone** — speak to VoxVault directly
   - **System Audio (Meeting)** — share a tab/screen and feed a *played* meeting through
   - **Mic + System** — both at once
4. Speak (or play a meeting near the mic) — **live transcript cards** appear instantly, speaker-tagged
5. Click **End Session** when done
6. **Ask questions** (type, or the answer is spoken):
   - *"Who owns the API redesign?"*
   - Click **Summarize / Action Items / Decisions** quick chips
7. Use natural-language **memory commands** (voice or text) — VoxVault confirms each one **out loud**
8. Browse gathered memories in the right rail with **replay** buttons (each segment plays back its original audio)
9. **Recent Meetings** — reopen any past session; memory is fully restored for context

## Voice Commands

| You say | VoxVault does |
|---------|---------------|
| "remember that the launch is in June" | stores a **fact** memory with speaker + audio |
| "the project budget is 10k" | stores a fact via `note` syntax |
| "I prefer morning meetings" | stores a **preference** |
| "action item: call the vendor by Friday" | stores an **open action** (status=open) |
| "todo: prepare the slide deck" | stores an open action |
| "done: call the vendor by Friday" | marks the matching open action **done** |
| "forget the launch date" | deletes the matching memory |
| "action items" (typed keyword) | lists all open actions across sessions |

Commands work **both in speech and typed into the query box**, and every confirmation/answer is spoken by Rime.

---

## API

The full REST + WebSocket protocol is documented in **[API.md](API.md)**. Quick map:

| Endpoint | Purpose |
|----------|---------|
| `POST /api/auth/signup` · `/api/auth/login` · `GET /api/auth/me` | authentication |
| `POST /api/sessions` · `GET /api/sessions` · `PATCH/DELETE /api/sessions/{id}` | session lifecycle |
| `GET /api/memory` · `DELETE /api/memory` | read / clear memory |
| `POST /api/query` | ask a question (returns text + Rime audio URL) |
| `GET /api/actions/open` | pending action items |
| `GET /api/system` | pipeline/status info |
| `POST /api/tts` | raw TTS synthesis |
| `WS /ws/audio/{session_id}?channel=mic\|sys\|both` | live PCM audio → transcription stream |
| `GET /health` | health check |

---

## Development & Implementation Plan

See **[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)** for:

- Milestones and how the build tracked the problem statement
- Validation we ran (server boot, REST smoke tests, WS audio test, mock-audio transcription)
- Limits of this implementation vs. production-ready
- AI-assistant disclosure (per hackathon rules, AI-assisted coding is allowed and must be explainable)

---

## Working Proof

| Proof | How to reproduce |
|-------|------------------|
| Live audio → STT → memory pipeline | `uvicorn app.main:app`, start a session, speak → live transcript + Qdrant memory cards |
| Voice-essential loop | ask a question → Rime audio plays back the answer (no keyboard needed except sign-in) |
| Semantic retrieval | say "who owns the API redesign" after storing a slightly different phrasing → correct segment returns via embeddings |
| Memory commands | "action item: ..." creates an open action; "done: ..." closes it; shown live in the feed |
| Session continuity | sessions persist per user; reopen from **Recent Meetings** with full memory context |
| Headless/automated check | `python test_ws_channels.py` exercises the WebSocket audio channel end-to-end (see [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)) |

---

## Limitations

- **Diarization** is lightweight timbre clustering, not full neural speaker diarization — good enough for Speaker A/B/C/D separation on a clean recording, not for heavily overlapping speech.
- **Summarization** concatenates stored segments (no LLM) and truncates to a Rime-safe length. It is deterministic, not generative.
- **No open-ended LLM answering** — `/api/query` returns retrieved context, not a synthesized reasoning answer (kept low-latency and free).
- **Local-dev auth fallback** (`data/users.json`) is for demo only; Supabase Auth is the production path.
- **Audio persistence** keeps one WAV per segment locally; storage is not sharded/object-stored. `app/static/tts` and generated WAVs are gitignored.
- **Low-latency but not instantaneous** — STT runs local on CPU; projection latency depends on your machine.
- **Requires HTTPS** (or localhost) for `getDisplayMedia` in non-local deployments; use `ngrok` for remote demos.
- Does **not** prove: robust overlapping-speaker handling, multilingual accuracy, or large-scale multi-tenant performance.

---

## Team & Contributions

| Member | Contributions |
|--------|---------------|
| *(Shresth Gupta)* | *(dev)* |
| *(Anshuman Singh Chahar)* | *(add contributions)* |
| *(Yuvraj Singh)* | *(add contributions)* |

> **AI-assisted coding disclosure (per problem-statement rules):** AI tooling (opencode) was used during development for scaffolding, code review, and documentation. Every component is understood and verified by the team — the team is fully able to explain the implementation.

---

## Submission Checklist

- [x] Public GitHub repository + README (setup/run)
- [x] PPT using the official template — public link
- [x] ≤4 min demo video on Google Drive ("Anyone with the link can view")
- [x] Working proof: live voice→memory→Q&A feature + WS test script
- [x] Architecture diagram (ARCHITECTURE.md)
- [x] Limitations & team contributions documented
- [ ] Add real team member names/contributions above
- [ ] Ensure `.env` is never committed (only `.env.example`)

---

## License

MIT — submitted for demonstration at STARFORGE 2026, JSS University, Noida.

Recap of relevant resources:
- Rime docs: <https://docs.rime.ai>
- Qdrant docs: <https://qdrant.tech/documentation/>
- STARFORGE WhatsApp community: <https://chat.whatsapp.com/BhfbYofEkx1HAvNvD7wGQl>