import time
import logging
from pathlib import Path
from typing import Optional
import numpy as np

from app.models.schemas import TranscriptionResult
from app.config import settings

logger = logging.getLogger(__name__)


class WhisperService:
    def __init__(self):
        self.model = None
        self._load_model()

    def _load_model(self):
        try:
            from faster_whisper import WhisperModel
            model_path = Path(settings.whisper_model_path)
            if model_path.exists():
                self.model = WhisperModel(str(model_path), device="cpu", compute_type="int8")
                logger.info(f"Loaded Whisper model from {model_path}")
            else:
                logger.warning(f"Model not found at {model_path}, downloading base model...")
                self.model = WhisperModel("base", device="cpu", compute_type="int8")
        except ImportError:
            logger.error("faster-whisper not installed. Install with: pip install faster-whisper")
            raise

    def transcribe(self, audio_data: np.ndarray, sample_rate: int = 16000) -> TranscriptionResult:
        if self.model is None:
            raise RuntimeError("Whisper model not loaded")

        start_time = time.time()

        if audio_data.dtype != np.float32:
            audio_data = audio_data.astype(np.float32) / 32768.0

        segments, info = self.model.transcribe(
            audio_data,
            language="en",
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500)
        )

        text = " ".join([seg.text for seg in segments])
        processing_time = (time.time() - start_time) * 1000

        return TranscriptionResult(
            text=text.strip(),
            confidence=info.language_probability if hasattr(info, 'language_probability') else 0.9,
            language=info.language if hasattr(info, 'language') else "en",
            processing_time_ms=processing_time
        )


whisper_service = WhisperService()