import logging
from typing import List

import numpy as np

logger = logging.getLogger(__name__)

MAX_SPEAKERS = 4
# Feature components have magnitudes up to ~4 (zero-crossing terms), so a
# distance threshold of ~0.2 was far too tight and spawned a new speaker for
# every buffer. Use a relaxed matching threshold (merge unless clearly distinct)
# plus inertia so a steady speaker keeps their label across short buffers.
NEW_SPEAKER_THRESHOLD = 1.4
INERTIA_WEIGHT = 0.9
SPEAKER_LABELS = ["Speaker A", "Speaker B", "Speaker C", "Speaker D"]


def _extract_features(audio: np.ndarray, sample_rate: int = 16000) -> np.ndarray:
    """Map a raw audio buffer to a small timbre/energy feature vector."""
    audio = np.asarray(audio, dtype=np.float32)
    if audio.size == 0:
        return np.zeros(5, dtype=np.float32)

    frame = max(1, int(0.025 * sample_rate))
    n_frames = max(1, audio.size // frame)
    frames = audio[: n_frames * frame].reshape(n_frames, frame)
    win = np.hanning(frame)
    freqs = np.fft.rfftfreq(frame, d=1.0 / sample_rate)

    centroids: List[float] = []
    spreads: List[float] = []
    zero_cross: List[float] = []
    for f in frames:
        spec = np.abs(np.fft.rfft(f * win)) ** 2
        energy = float(np.sum(spec))
        if energy <= 1e-9:
            continue
        cg = float(np.sum(freqs * spec) / energy)
        centroids.append(cg)
        spreads.append(float(np.sqrt(np.sum(((freqs - cg) ** 2) * spec) / energy)))
        zero_cross.append(float(np.mean(np.abs(np.diff(f)))))
        if len(centroids) >= 60:
            break

    if not centroids:
        return np.zeros(5, dtype=np.float32)

    zc = np.asarray(zero_cross, dtype=np.float32)
    rms_val = float(np.sqrt(np.mean(audio ** 2)) + 1e-6)

    return np.array([
        float(np.mean(centroids)) / 4000.0,
        float(np.mean(spreads)) / 2000.0,
        float(np.mean(zc)) * 4.0,
        float(np.max(zc)) * 2.5,
        float(np.log1p(rms_val * 2.0)),
    ], dtype=np.float32)


class DiarizeService:
    """Online speaker clustering over spectral timbre features.

    Each transcribed audio buffer is assigned to the nearest learned speaker
    centroid (cosine-free L2). Unknown timbres create new speakers up to
    MAX_SPEAKERS, after which inputs merge into the closest existing speaker.
    """

    def __init__(self):
        self._centroids: dict[str, List[np.ndarray]] = {}
        self._counts: dict[str, List[int]] = {}
        self._last_label: dict[str, str] = {}

    def reset(self, channel: str):
        self._centroids[channel] = []
        self._counts[channel] = []
        self._last_label[channel] = None

    def assign(self, audio: np.ndarray, channel: str = "unitai", sample_rate: int = 16000) -> str:
        feature = _extract_features(audio, sample_rate)
        centroids = self._centroids.setdefault(channel, [])
        counts = self._counts.setdefault(channel, [])
        last_label = self._last_label.get(channel)

        if not centroids:
            centroids.append(feature)
            counts.append(1)
            self._last_label[channel] = SPEAKER_LABELS[0]
            return SPEAKER_LABELS[0]

        dists = [float(np.linalg.norm(feature - c)) for c in centroids]
        best_idx = int(np.argmin(dists))
        best_dist = dists[best_idx]

        # Inertia: if the previous speaker is still a close match to this new
        # audio, keep their label even if another speaker is marginally nearer.
        if last_label and (best_dist < NEW_SPEAKER_THRESHOLD or len(centroids) >= MAX_SPEAKERS):
            if last_label in SPEAKER_LABELS and SPEAKER_LABELS.index(last_label) < len(centroids):
                inertia_idx = SPEAKER_LABELS.index(last_label)
                inertia_dist = dists[inertia_idx]
                if inertia_dist <= best_dist * 1.35:
                    best_idx = inertia_idx

        if best_dist < NEW_SPEAKER_THRESHOLD or len(centroids) >= MAX_SPEAKERS:
            alpha = 0.7
            centroids[best_idx] = (alpha * centroids[best_idx]) + ((1 - alpha) * feature)
            counts[best_idx] += 1
            label = SPEAKER_LABELS[best_idx]
            self._last_label[channel] = label
            return label

        centroids.append(feature)
        counts.append(1)
        label = SPEAKER_LABELS[len(centroids) - 1]
        self._last_label[channel] = label
        return label


diarize_service = DiarizeService()