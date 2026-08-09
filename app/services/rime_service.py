import logging
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx

from app.models.schemas import TTSRequest, TTSResponse
from app.config import settings

logger = logging.getLogger(__name__)

TTS_DIR = Path("app/static/tts")


class RimeService:
    def __init__(self):
        self.api_key = settings.rime_api_key
        self.base_url = settings.rime_api_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=60.0)

    async def synthesize(self, request: TTSRequest) -> Optional[TTSResponse]:
        if not self.api_key:
            logger.warning("Rime API key not configured, returning mock response")
            return TTSResponse(
                audio_url="/static/mock_audio.wav",
                duration_ms=len(request.text) * 50
            )

        try:
            TTS_DIR.mkdir(parents=True, exist_ok=True)
            start_time = time.time()
            response = await self.client.post(
                f"{self.base_url}/rime-tts",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "audio/wav",
                },
                json={
                    "text": request.text,
                    "speaker": request.voice,
                    "modelId": "coda",
                    "lang": "en",
                }
            )
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if not content_type.startswith("audio/"):
                logger.error(f"Unexpected Rime response: {response.text[:500]}")
                return None

            filename = f"tts_{uuid.uuid4().hex}.wav"
            dst = TTS_DIR / filename
            dst.write_bytes(response.content)

            duration = round((time.time() - start_time) * 1000, 2)
            return TTSResponse(audio_url=f"/static/tts/{filename}", duration_ms=duration)

        except httpx.HTTPStatusError as e:
            logger.error(f"Rime API error: {e.response.status_code} - {e.response.text[:500]}")
            return None
        except Exception as e:
            logger.error(f"TTS synthesis failed: {e}")
            return None

    async def close(self):
        await self.client.aclose()


rime_service = RimeService()