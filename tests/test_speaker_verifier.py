import unittest
import numpy as np
from core.speaker_verifier import _extract_biometrics, SpeakerVerifier

class TestSpeakerVerifier(unittest.TestCase):
    def test_feature_extraction(self):
        sr = 16000
        # 1.5 seconds of synthetic audio
        t = np.linspace(0, 1.5, int(sr * 1.5), endpoint=False)
        audio = 0.5 * np.sin(2 * np.pi * 440 * t) + 0.25 * np.sin(2 * np.pi * 880 * t)

        vec = _extract_biometrics(audio, sample_rate=sr)
        self.assertEqual(len(vec), 132)
        norm = np.linalg.norm(vec)
        self.assertAlmostEqual(norm, 1.0, places=4)

    def test_verification_similarity(self):
        sr = 16000
        t = np.linspace(0, 1.5, int(sr * 1.5), endpoint=False)
        # Same fundamental harmonic speaker
        voice_a1 = 0.5 * np.sin(2 * np.pi * 300 * t) + 0.2 * np.sin(2 * np.pi * 600 * t)
        voice_a2 = 0.48 * np.sin(2 * np.pi * 300 * t) + 0.22 * np.sin(2 * np.pi * 600 * t)
        # Completely different speaker profile
        voice_b = 0.5 * np.sin(2 * np.pi * 1200 * t) + 0.3 * np.sin(2 * np.pi * 2400 * t)

        vec_a1 = _extract_biometrics(voice_a1, sr)
        vec_a2 = _extract_biometrics(voice_a2, sr)
        vec_b = _extract_biometrics(voice_b, sr)

        sim_same = float(np.dot(vec_a1, vec_a2))
        sim_diff = float(np.dot(vec_a1, vec_b))

        self.assertGreater(sim_same, sim_diff)
        self.assertGreater(sim_same, 0.95)

if __name__ == "__main__":
    unittest.main()
