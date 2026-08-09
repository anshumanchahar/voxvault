import asyncio
import sys
import time
import wave
from pathlib import Path
from typing import List

import httpx
import numpy as np
import websockets

BASE = "http://localhost:8000"
MOCK = Path(__file__).resolve().parent / "app" / "static" / "mock_audio.wav"

# Unique-ish test account so repeat runs don't 409 on signup.
EMAIL = f"wstest-{int(time.time())}@test.com"
PASSWORD = "hunter22"


def auth_setup() -> tuple[str, str]:
    """Sign up (or log in) and create a session. Returns (token, session_id)."""
    with httpx.Client(timeout=30) as c:
        r = c.post(f"{BASE}/api/auth/signup", json={"email": EMAIL, "password": PASSWORD})
        if r.status_code == 200:
            data = r.json()
            token = data["token"]
        elif r.status_code == 409:  # already registered -> log in
            r = c.post(f"{BASE}/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
            r.raise_for_status()
            token = r.json()["token"]
        else:
            r.raise_for_status()
            raise RuntimeError(f"signup failed: {r.text}")

        r = c.post(
            f"{BASE}/api/sessions",
            headers={"Authorization": f"Bearer {token}"},
            json={"title": "WS channel test"},
        )
        r.raise_for_status()
        return token, r.json()["session_id"]


async def send_channel(channel: str, token: str, session_id: str, seconds: float = 1.0) -> List[str]:
    uri = f"ws://localhost:8000/ws/audio/{session_id}?channel={channel}&token={token}"
    async with websockets.connect(uri, open_timeout=10) as ws:
        with wave.open(str(MOCK), "rb") as w:
            raw = w.readframes(w.getnframes())
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        # repeat enough to exceed the 10-chunk buffer
        reps = max(11, int(seconds * 16000 / 4096))
        msgs: List[str] = []

        async def recv_loop():
            try:
                while True:
                    m = await asyncio.wait_for(ws.recv(), timeout=12)
                    msgs.append(m)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        recv_task = asyncio.create_task(recv_loop())
        for i in range(reps):
            seg = data[i * 4096:(i + 1) * 4096]
            if seg.size < 4096:
                seg = np.resize(seg, 4096)
            await ws.send(seg.astype(np.float32).tobytes())
        await asyncio.sleep(4.5)
        recv_task.cancel()
        return msgs


async def main():
    token, session_id = auth_setup()
    print(f"Authenticated as {EMAIL} (session {session_id})")

    print("--- MIC channel ---")
    for m in await send_channel("mic", token, session_id):
        print("mic:", m)

    print("--- SYS channel ---")
    for m in await send_channel("sys", token, session_id):
        print("sys:", m)


if __name__ == "__main__":
    asyncio.run(main())