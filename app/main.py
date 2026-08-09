import asyncio
import logging
import uuid
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Header
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.models.schemas import (
    AudioChunk, TranscriptionResult, MeetingSegment,
    QueryRequest, QueryResponse, TTSRequest, TTSResponse,
    AuthRequest, AuthResponse, SessionCreateRequest, SessionUpdateRequest, SessionSummary,
    OpenAction,
)
from app.services.whisper_service import whisper_service
from app.services.qdrant_service import qdrant_service
from app.services.rime_service import rime_service
from app.services.diarize_service import diarize_service
from app.services.auth_service import auth_service
from app.services.command_service import parse_command

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

active_connections: dict[str, WebSocket] = {}
audio_buffers: dict[str, list] = {}
conn_meta: dict[str, dict] = {}

RMS_FLOOR = 0.01  # below this mean amplitude, treat as silence/noise
BUFFER_PROCESS_THRESHOLD = 10  # number of chunks before running whisper
AUDIO_DIR = Path("app/static/audio")


def truncate_text(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    split_at = text.rfind(" ", 0, max_len)
    return text[:split_at].rstrip("., ") + "..."


def save_audio_wav(audio_data, segment_id: str) -> Optional[str]:
    """Persist the raw utterance as a 16 kHz mono WAV for replay."""
    import wave
    try:
        import numpy as np
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        pcm = (np.clip(audio_data, -1.0, 1.0) * 32767).astype(np.int16)
        path = AUDIO_DIR / f"{segment_id}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm.tobytes())
        return f"/static/audio/{segment_id}.wav"
    except Exception as e:
        logger.error(f"Failed to save audio wav: {e}")
        return None


async def _current_user(authorization: Optional[str] = Header(None)) -> Optional[dict]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:].strip()
    return await auth_service.get_user(token)


def _require_user(user: Optional[dict]) -> dict:
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting VoxVault...")
    yield
    logger.info("Shutting down...")
    await rime_service.close()
    await auth_service.close()


app = FastAPI(title="VoxVault", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "supabase_enabled": auth_service._supabase_enabled,
    })


@app.websocket("/ws/audio/{session_id}")
async def websocket_audio(websocket: WebSocket, session_id: str):
    channel = websocket.query_params.get("channel", "mic")
    token = websocket.query_params.get("token", "")
    user = await auth_service.get_user(token)
    if not user:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    conn_key = f"{user['id']}:{session_id}:{channel}"
    await websocket.accept()
    active_connections[conn_key] = websocket
    audio_buffers[conn_key] = []
    conn_meta[conn_key] = {"user_id": user["id"], "session_id": session_id}
    diarize_service.reset(conn_key)
    logger.info(f"Client connected: user={user.get('email')} session={session_id} channel={channel}")

    try:
        while True:
            data = await websocket.receive_bytes()
            audio_buffers[conn_key].append(data)

            if len(audio_buffers[conn_key]) >= BUFFER_PROCESS_THRESHOLD:
                await process_audio_buffer(conn_key, channel)

    except WebSocketDisconnect:
        logger.info(f"Client disconnected: session={session_id} channel={channel}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        active_connections.pop(conn_key, None)
        audio_buffers.pop(conn_key, None)
        conn_meta.pop(conn_key, None)


async def process_audio_buffer(conn_key: str, channel: str = "mic"):
    if conn_key not in audio_buffers or not audio_buffers[conn_key]:
        return
    meta = conn_meta.get(conn_key)
    if not meta:
        return
    user_id = meta["user_id"]
    session_id = meta["session_id"]

    import numpy as np
    raw_audio = b"".join(audio_buffers[conn_key])
    audio_buffers[conn_key] = []

    audio_data = np.frombuffer(raw_audio, dtype=np.float32)

    # Noise gate: ignore buffers that are mostly background noise/silence.
    rms = float(np.sqrt(np.mean(audio_data ** 2))) if audio_data.size else 0.0
    if rms < RMS_FLOOR:
        logger.info(f"Noise gate: dropped {channel} buffer rms={rms:.4f}")
        return

    try:
        result: TranscriptionResult = whisper_service.transcribe(audio_data)
        text = result.text.strip()
        if not text:
            return

        title = _session_title(user_id, session_id)

        # Commands apply only to what the user says on the mic channel.
        cmd = parse_command(text) if channel == "mic" else None

        if cmd and cmd.action in ("remember", "action"):
            memory_type_map = {"remember": "fact", "action": "action", "preference": "preference"}
            segment = MeetingSegment(
                text=cmd.text or text,
                speaker="You",
                timestamp=datetime.utcnow(),
                session_id=session_id,
                user_id=user_id,
                title=title,
                memory_type=memory_type_map.get(cmd.memory_type, "fact"),
                status="open" if cmd.memory_type == "action" else None,
                audio_url=save_audio_wav(audio_data, str(uuid.uuid4())),
            )
            stored = qdrant_service.store_segment(segment, user_id=user_id, session_id=session_id, title=title)
            qdrant_service.touch_session(user_id, session_id, title=title)
            ws = active_connections.get(conn_key)
            if ws and stored:
                await ws.send_json({
                    "type": "memory_command",
                    "action": cmd.action,
                    **({"memory_id": str(segment.id)} if stored else {}),
                    "text": segment.text,
                    "confirm": cmd.confirm,
                })
            logger.info(f"Command stored: {cmd.action} / {cmd.memory_type} / {segment.text}")
            return

        if cmd and cmd.action == "done":
            ws = active_connections.get(conn_key)
            if cmd.text:
                hits = qdrant_service.search_similar(cmd.text, 1, user_id, session_id=None, memory_type="action")
            else:
                hits = qdrant_service.get_all_segments(user_id=user_id, memory_type="action", status="open")
            if hits:
                qdrant_service.update_segment_status(user_id, str(hits[0].id), "done")
                logger.info(f"Marked action done: {hits[0].text}")
                if ws:
                    await ws.send_json({"type": "memory_command", "action": "done", "text": hits[0].text, "confirm": cmd.confirm})
            return

        if cmd and cmd.action == "forget":
            ws = active_connections.get(conn_key)
            if cmd.text:
                hits = qdrant_service.search_similar(cmd.text, 1, user_id, session_id=None)
            else:
                recent = qdrant_service.get_all_segments(user_id=user_id)
                recent = [s for s in recent if s.memory_type != "transcript"]
                recent.sort(key=lambda s: s.timestamp, reverse=True)
                hits = recent[:1]
            if hits:
                qdrant_service.delete_segment(user_id, str(hits[0].id))
                logger.info(f"Forgot memory: {hits[0].text}")
                if ws:
                    await ws.send_json({"type": "memory_command", "action": "forget", "text": hits[0].text, "confirm": cmd.confirm})
            return

        # Speaker separation: mic channel is "you", system channel is
        # diarized into timbre clusters (other meeting participants).
        # Diarization clusters are scoped to each connection (user+session+channel).
        if channel == "mic":
            speaker = "You"
        else:
            speaker = diarize_service.assign(audio_data, channel=conn_key)

        segment = MeetingSegment(
            text=text,
            speaker=speaker,
            timestamp=datetime.utcnow(),
            session_id=session_id,
            user_id=user_id,
            title=title,
            memory_type="transcript",
            audio_url=save_audio_wav(audio_data, str(uuid.uuid4())),
        )
        qdrant_service.touch_session(user_id, session_id, title=title)
        qdrant_service.store_segment(segment, user_id=user_id, session_id=session_id, title=title)

        ws = active_connections.get(conn_key)
        if ws:
            await ws.send_json({
                "type": "transcription",
                "text": text,
                "confidence": result.confidence,
                "speaker": speaker,
                "channel": channel,
                "session_id": session_id,
                "segment_id": str(segment.id),
                "audio_url": segment.audio_url,
            })

    except Exception as e:
        logger.error(f"Transcription error (channel={channel}): {e}")


def _session_title(user_id: str, session_id: str) -> str:
    session = qdrant_service.get_session(user_id, session_id)
    if session and session.title:
        return session.title
    return "Untitled"


# ------------------- auth -------------------
@app.post("/api/auth/signup", response_model=AuthResponse)
async def signup(request: AuthRequest):
    result = await auth_service.signup(request.email, request.password)
    if not result:
        raise HTTPException(status_code=409, detail="Signup failed (email may already be registered)")
    if result.get("confirm_required"):
        # Supabase has email confirmation enabled: the account was created but
        # cannot be used until the email link is verified.
        raise HTTPException(
            status_code=400,
            detail=f"Account created. Confirm your email ({result['email']}) "
                   "to sign in — or disable 'Confirm email' in Supabase > "
                   "Authentication > Providers > Email for instant access.",
        )
    return AuthResponse(token=result["token"], user=result["user"])


@app.post("/api/auth/login", response_model=AuthResponse)
async def login(request: AuthRequest):
    result = await auth_service.login(request.email, request.password)
    if not result:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return AuthResponse(token=result["token"], user=result["user"])


@app.get("/api/auth/me")
async def me(authorization: Optional[str] = Header(None)):
    user = _require_user(await _current_user(authorization))
    return {"token": authorization[7:].strip(), "user": user}


# ------------------- sessions -------------------
@app.post("/api/sessions", response_model=SessionSummary)
async def create_session(request: Optional[SessionCreateRequest] = None,
                         authorization: Optional[str] = Header(None)):
    user = _require_user(await _current_user(authorization))
    session_id = str(uuid.uuid4())
    title = (request.title if request and request.title
             else datetime.now().strftime("Meeting %b %d, %H:%M"))
    qdrant_service.touch_session(
        user["id"], session_id, title=title, started_at=datetime.now().isoformat(),
        increment=False,
    )
    return SessionSummary(
        session_id=session_id,
        title=title,
        started_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        segment_count=0,
    )


@app.get("/api/sessions", response_model=List[SessionSummary])
async def list_sessions(authorization: Optional[str] = Header(None)):
    user = _require_user(await _current_user(authorization))
    sessions = qdrant_service.get_sessions(user["id"])
    for s in sessions:
        segments = qdrant_service.get_all_segments(user["id"], s.session_id)
        s.segment_count = len(segments)
    return sessions


@app.get("/api/sessions/{session_id}", response_model=SessionSummary)
async def get_session(session_id: str, authorization: Optional[str] = Header(None)):
    user = _require_user(await _current_user(authorization))
    session = qdrant_service.get_session(user["id"], session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    session.segment_count = len(qdrant_service.get_all_segments(user["id"], session_id))
    return session


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, authorization: Optional[str] = Header(None)):
    user = _require_user(await _current_user(authorization))
    qdrant_service.delete_session(user["id"], session_id)
    return {"deleted": True}


@app.patch("/api/sessions/{session_id}", response_model=SessionSummary)
async def rename_session(session_id: str, request: SessionUpdateRequest,
                         authorization: Optional[str] = Header(None)):
    user = _require_user(await _current_user(authorization))
    session = qdrant_service.get_session(user["id"], session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    title = request.title.strip() or session.title
    qdrant_service.rename_session(user["id"], session_id, title)
    updated = qdrant_service.get_session(user["id"], session_id)
    return updated or session


@app.get("/api/actions/open", response_model=List[OpenAction])
async def open_actions(authorization: Optional[str] = Header(None)):
    user = _require_user(await _current_user(authorization))
    return qdrant_service.get_open_actions(user["id"])


@app.post("/api/query", response_model=QueryResponse)
async def query_memory(request: QueryRequest, authorization: Optional[str] = Header(None)):
    user = _require_user(await _current_user(authorization))
    q = request.query.strip()

    # --- typed memory commands (same engine as voice) ---
    cmd = parse_command(q) if q.strip().lower() not in ("summarize", "summary", "summarise", "summarize the meeting", "summary of the meeting", "action items", "decisions") else None
    if cmd:
        if cmd.action in ("remember", "action", "preference"):
            memory_type = command_type(cmd.memory_type)
            segment = MeetingSegment(
                text=cmd.text or q,
                speaker="You",
                timestamp=datetime.utcnow(),
                session_id=request.session_id,
                user_id=user["id"],
                memory_type=memory_type,
                status="open" if memory_type == "action" else None,
            )
            qdrant_service.store_segment(segment, user_id=user["id"], session_id=request.session_id)
            if request.session_id:
                qdrant_service.touch_session(user["id"], request.session_id)
            answer = cmd.confirm
            sources: List[MeetingSegment] = [segment]
        elif cmd.action == "done":
            if cmd.text:
                hits = qdrant_service.search_similar(cmd.text, 1, user["id"], memory_type="action")
            else:
                hits = qdrant_service.get_all_segments(user_id=user["id"], memory_type="action", status="open")
            done_text = ""
            if hits:
                qdrant_service.update_segment_status(user["id"], str(hits[0].id), "done")
                done_text = hits[0].text
            answer = f"Marked done: {done_text}" if done_text else "No matching action item found."
            sources = hits[:1]
        else:  # forget
            if cmd.text:
                hits = qdrant_service.search_similar(cmd.text, 1, user["id"])
            else:
                recent = [s for s in qdrant_service.get_all_segments(user_id=user["id"]) if s.memory_type != "transcript"]
                recent.sort(key=lambda s: s.timestamp, reverse=True)
                hits = recent[:1]
            forgotten = ""
            if hits:
                qdrant_service.delete_segment(user["id"], str(hits[0].id))
                forgotten = hits[0].text
            answer = f"Forgot: {forgotten}" if forgotten else "I don't record that memory."
            sources = hits[:1]

        tts_req = TTSRequest(text=answer)
        tts_resp = await rime_service.synthesize(tts_req)
        return QueryResponse(answer=answer, sources=sources, audio_url=tts_resp.audio_url if tts_resp else None)

    ql = q.strip().lower()
    all_segments = qdrant_service.get_all_segments(user["id"], request.session_id)

    if not all_segments:
        answer = "I don't have any meeting memories yet. Start speaking to build the memory."
        sources: List[MeetingSegment] = []
    elif ql in ("summarize", "summary", "summarise", "summarize the meeting", "summary of the meeting"):
        grouped: dict[str, list[str]] = {}
        speakers = ["You", "Speaker A", "Speaker B", "Speaker C", "Speaker D"]
        for s in all_segments:
            grouped.setdefault(s.speaker, []).append(s.text)
        parts = []
        for sp in speakers:
            if sp in grouped and grouped[sp]:
                parts.append(f"{sp}: {' '.join(grouped[sp])}")
        if not parts:
            parts = [f"{s.speaker}: {s.text}" for s in all_segments]
        text = " ".join(parts)
        answer = truncate_text(text, 800)
        sources = all_segments
    elif ql in ("action items", "open actions", "what's pending", "pending tasks", "action items?"):
        actions = qdrant_service.get_open_actions(user["id"])
        if not actions:
            answer = "You have no open action items."
            sources = []
        else:
            answer = "Open action items: " + truncate_text(" ".join(f"#{i+1} {a.text}" for i, a in enumerate(actions)), 800)
            sources = actions  # type: ignore
    else:
        sources = qdrant_service.search_similar(request.query, request.top_k, user["id"], request.session_id)
        if not sources:
            answer = "I don't have any meeting memories that match that question."
        else:
            context = "\n".join([f"{s.speaker}: {s.text}" for s in sources])
            answer = f"Based on the meeting: {truncate_text(context, 800)}"

    tts_req = TTSRequest(text=answer)
    tts_resp = await rime_service.synthesize(tts_req)

    return QueryResponse(
        answer=answer,
        sources=sources,
        audio_url=tts_resp.audio_url if tts_resp else None
    )


def command_type(memory_type: str) -> str:
    if memory_type in ("action", "preference", "fact"):
        return memory_type
    return "fact"


@app.post("/api/tts", response_model=TTSResponse)
async def text_to_speech(request: TTSRequest):
    result = await rime_service.synthesize(request)
    if not result:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail="TTS synthesis failed")
    return result


@app.get("/api/memory")
async def get_memory(session_id: Optional[str] = None, authorization: Optional[str] = Header(None)):
    user = _require_user(await _current_user(authorization))
    segments = qdrant_service.get_all_segments(user["id"], session_id)
    return {"segments": segments, "session_id": session_id}


@app.delete("/api/memory")
async def clear_memory(session_id: Optional[str] = None, authorization: Optional[str] = Header(None)):
    user = _require_user(await _current_user(authorization))
    cleared = qdrant_service.clear_all(user["id"], session_id)
    return {"cleared": cleared}


@app.get("/api/system")
async def system_info(authorization: Optional[str] = Header(None)):
    user = _require_user(await _current_user(authorization))
    sessions = qdrant_service.get_sessions(user["id"])
    segments = []
    for s in sessions:
        segments.extend(qdrant_service.get_all_segments(user["id"], s.session_id))
    return {
        "user": user,
        "stt": {"model": "whisper-base.en"},
        "vector": {
            "qdrant_url": settings.qdrant_url,
            "collection": "meeting_memory",
            "vectors": len(segments),
            "sessions": len(sessions),
            "available": qdrant_service._available,
        },
        "tts": {
            "provider": "Rime",
            "model": "coda",
            "has_api_key": bool(settings.rime_api_key)
        },
        "auth": {
            "provider": "Supabase" if auth_service._supabase_enabled else "local-dev"
        }
    }


@app.get("/health")
async def health():
    return {"status": "ok", "service": "voxvault"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)