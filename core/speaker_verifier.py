"""
core/speaker_verifier.py — Speaker verification and biometric voiceprint gating.

WHY THIS EXISTS
    A voice assistant in a shared room or office should only respond to its owner.
    Background chatter, TV audio, colleagues, or malicious prompts from videos
    should not trigger commands or compromise user data.

HOW IT WORKS
    1. Enrollment: The user speaks a phrase 3-5 times.
    2. Feature Extraction: Extracts 132-dimensional spectral biometric voiceprints:
       - 13 MFCCs (means + variances)
       - First-order delta cepstral dynamics (means + variances)
       - 40 Mel-filterbank energy distributions across vocal tract resonances
       - Normalized unit vector representation
    3. Verification: Incoming audio frames compute cosine similarity against the
       stored voiceprint:
           similarity = dot(sample_emb, reference_emb)
       If similarity >= threshold (default 0.94), the voice is authenticated.
    4. Persistence: Stored securely in config/speaker_profile.json.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
from scipy.fftpack import dct


_BASE_DIR = Path(__file__).resolve().parent.parent
_PROFILE_PATH = _BASE_DIR / "config" / "speaker_profile.json"
_DEFAULT_THRESHOLD = 0.94


def _extract_biometrics(audio_samples: np.ndarray, sample_rate: int = 16000) -> np.ndarray:
    """Extract a 132-dimensional normalized voice biometric vector."""
    audio = audio_samples.astype(np.float32)
    max_val = np.max(np.abs(audio))
    if max_val > 0:
        audio = audio / max_val

    # Pre-emphasis
    pre_emphasized = np.append(audio[0], audio[1:] - 0.97 * audio[:-1])

    # 25ms frame, 10ms stride
    frame_size = int(0.025 * sample_rate)
    frame_stride = int(0.010 * sample_rate)
    if len(pre_emphasized) < frame_size:
        return np.zeros(132, dtype=np.float32)

    num_frames = 1 + int(np.floor((len(pre_emphasized) - frame_size) / frame_stride))
    if num_frames < 2:
        return np.zeros(132, dtype=np.float32)

    indices = np.tile(np.arange(0, frame_size), (num_frames, 1)) + \
              np.tile(np.arange(0, num_frames * frame_stride, frame_stride), (frame_size, 1)).T
    frames = pre_emphasized[indices] * np.hamming(frame_size)

    # FFT & Power spectrum
    NFFT = 512
    mag_frames = np.absolute(np.fft.rfft(frames, NFFT))
    pow_frames = (1.0 / NFFT) * (mag_frames ** 2)

    # 40-channel Mel Filterbanks
    low_freq_mel = 0
    high_freq_mel = 2595 * np.log10(1 + (sample_rate / 2) / 700)
    mel_points = np.linspace(low_freq_mel, high_freq_mel, 42)
    hz_points = 700 * (10 ** (mel_points / 2595) - 1)
    bin_points = np.floor((NFFT + 1) * hz_points / sample_rate).astype(int)

    fbank = np.zeros((40, int(np.floor(NFFT / 2 + 1))))
    for m in range(1, 41):
        f_m_minus = bin_points[m - 1]
        f_m = bin_points[m]
        f_m_plus = bin_points[m + 1]
        for k in range(f_m_minus, f_m):
            fbank[m - 1, k] = (k - bin_points[m - 1]) / max(1, (f_m - bin_points[m - 1]))
        for k in range(f_m, f_m_plus):
            fbank[m - 1, k] = (bin_points[m + 1] - k) / max(1, (bin_points[m + 1] - f_m))

    filter_banks = np.dot(pow_frames, fbank.T)
    filter_banks = np.where(filter_banks == 0, np.finfo(float).eps, filter_banks)
    filter_banks = 20 * np.log10(filter_banks)

    # MFCC (13 coeffs) + Deltas
    mfcc = dct(filter_banks, type=2, axis=1, norm='ortho')[:, :13]
    delta = np.gradient(mfcc, axis=0)

    # Statistics across time
    mfcc_mean = np.mean(mfcc, axis=0)
    mfcc_std = np.std(mfcc, axis=0)
    delta_mean = np.mean(delta, axis=0)
    delta_std = np.std(delta, axis=0)
    fbank_mean = np.mean(filter_banks, axis=0)
    fbank_std = np.std(filter_banks, axis=0)

    vec = np.concatenate([mfcc_mean, mfcc_std, delta_mean, delta_std, fbank_mean, fbank_std])
    norm = np.linalg.norm(vec)
    return vec / (norm + 1e-9)


@dataclass
class SpeakerProfile:
    speaker_name: str = ""
    enrolled_at: float = 0.0
    num_samples: int = 0
    embedding: list[float] = None
    threshold: float = _DEFAULT_THRESHOLD
    enabled: bool = False


class SpeakerVerifier:
    """Singleton service managing speaker enrollment and verification."""

    _instance: Optional[SpeakerVerifier] = None
    _lock = threading.Lock()

    def __init__(self):
        self._profile: Optional[SpeakerProfile] = None
        self._reference_vector: Optional[np.ndarray] = None
        self._load_profile()

    @classmethod
    def get_instance(cls) -> SpeakerVerifier:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _load_profile(self) -> None:
        if not _PROFILE_PATH.exists():
            return
        try:
            data = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
            emb = data.get("embedding")
            if emb:
                self._profile = SpeakerProfile(
                    speaker_name=data.get("speaker_name", "Owner"),
                    enrolled_at=data.get("enrolled_at", 0.0),
                    num_samples=data.get("num_samples", 0),
                    embedding=emb,
                    threshold=data.get("threshold", _DEFAULT_THRESHOLD),
                    enabled=data.get("enabled", True),
                )
                self._reference_vector = np.array(emb, dtype=np.float32)
                print(f"[SpeakerVerifier] Loaded profile for '{self._profile.speaker_name}' (threshold={self._profile.threshold}).")
        except Exception as e:
            print(f"[SpeakerVerifier] Could not load profile: {e}")

    def save_profile(self) -> None:
        if not self._profile:
            return
        try:
            _PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "speaker_name": self._profile.speaker_name,
                "enrolled_at": self._profile.enrolled_at,
                "num_samples": self._profile.num_samples,
                "embedding": self._profile.embedding,
                "threshold": self._profile.threshold,
                "enabled": self._profile.enabled,
            }
            _PROFILE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[SpeakerVerifier] Error saving profile: {e}")

    def is_enrolled(self) -> bool:
        return self._reference_vector is not None

    def is_enabled(self) -> bool:
        return self.is_enrolled() and (self._profile.enabled if self._profile else False)

    def set_enabled(self, enabled: bool) -> None:
        if self._profile:
            self._profile.enabled = enabled
            self.save_profile()

    def set_threshold(self, threshold: float) -> None:
        if self._profile:
            self._profile.threshold = max(0.5, min(0.99, float(threshold)))
            self.save_profile()

    def enroll_samples(self, samples: list[np.ndarray], speaker_name: str = "Owner", sample_rate: int = 16000) -> bool:
        """Enroll from a list of 16kHz audio sample arrays."""
        if not samples:
            return False

        vectors = []
        for s in samples:
            vec = _extract_biometrics(s, sample_rate)
            if np.linalg.norm(vec) > 0.1:
                vectors.append(vec)

        if not vectors:
            return False

        # Compute centroid voiceprint
        mean_vec = np.mean(vectors, axis=0)
        norm_vec = mean_vec / (np.linalg.norm(mean_vec) + 1e-9)

        self._profile = SpeakerProfile(
            speaker_name=speaker_name,
            enrolled_at=time.time(),
            num_samples=len(vectors),
            embedding=norm_vec.tolist(),
            threshold=_DEFAULT_THRESHOLD,
            enabled=True,
        )
        self._reference_vector = norm_vec
        self.save_profile()
        print(f"[SpeakerVerifier] Enrolled '{speaker_name}' with {len(vectors)} samples.")
        return True

    def verify(self, audio_data: np.ndarray | bytes, sample_rate: int = 16000) -> Tuple[bool, float]:
        """
        Verify if the given audio matches the enrolled speaker.
        Returns (is_verified, score).
        If speaker verification is disabled or not enrolled, returns (True, 1.0).
        """
        if not self.is_enabled():
            return True, 1.0

        if isinstance(audio_data, bytes):
            audio_samples = np.frombuffer(audio_data, dtype=np.int16)
        else:
            audio_samples = audio_data

        if len(audio_samples) < int(0.3 * sample_rate): # Minimum 300ms
            return True, 1.0

        sample_vec = _extract_biometrics(audio_samples, sample_rate)
        if np.linalg.norm(sample_vec) < 0.1:
            return False, 0.0

        score = float(np.dot(sample_vec, self._reference_vector))
        verified = score >= self._profile.threshold
        return verified, score


def get_speaker_verifier() -> SpeakerVerifier:
    return SpeakerVerifier.get_instance()
