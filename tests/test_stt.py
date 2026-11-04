"""Unit tests for the KIVO STT subsystem."""
from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from kivo.audio.audio_ingress import AudioIngress
from kivo.audio.audio_models import AudioCapture
from kivo.stt.engine import STTEngine
from kivo.stt.exceptions import STTAudioValidationError, STTInferenceError
from kivo.stt.local_engine import FasterWhisperEngine
from kivo.stt.models import STTStatus, TranscriptionResult
from kivo.stt.stt_config import load_stt_config, save_stt_config
from kivo.stt.transcription_manager import TranscriptionManager


class STTModelsTest(unittest.TestCase):
    def test_transcription_result_to_dict_caps_text_and_excludes_raw_audio(self):
        long_text = "word " * 3000  # 15,000 characters
        res = TranscriptionResult(
            transcription_id="t-1",
            capture_id="c-1",
            text=long_text,
            language="en",
            language_probability=0.98,
            engine="faster-whisper",
            model="base",
            duration=3.5,
            processing_time=0.42,
        )
        d = res.to_dict()
        self.assertLessEqual(len(d["text"]), 8000)
        self.assertNotIn("audio_bytes", d)
        self.assertEqual(d["transcription_id"], "t-1")
        self.assertEqual(d["language"], "en")
        self.assertEqual(d["duration"], 3.5)

    def test_stt_status_to_dict(self):
        status = STTStatus(
            state="transcribing",
            engine="faster-whisper",
            model="base",
            language="auto",
            available=True,
            queue_size=2,
        )
        d = status.to_dict()
        self.assertEqual(d["state"], "transcribing")
        self.assertEqual(d["queue_size"], 2)
        self.assertTrue(d["available"])


class STTConfigTest(unittest.TestCase):
    def test_load_and_save_config(self):
        cfg = load_stt_config()
        self.assertIn("engine", cfg)
        self.assertIn("model", cfg)
        self.assertIn("language", cfg)

        updated = save_stt_config(language="hi", model="small")
        self.assertEqual(updated["language"], "hi")
        self.assertEqual(updated["model"], "small")

        # Restore default
        save_stt_config(language="auto", model="base")


class FasterWhisperEngineTest(unittest.TestCase):
    def test_audio_capture_validation(self):
        engine = FasterWhisperEngine(model_name="base")
        with self.assertRaises(STTAudioValidationError):
            engine.transcribe("not-an-audio-capture")  # type: ignore

    def test_empty_audio_capture_returns_empty_result(self):
        engine = FasterWhisperEngine(model_name="base")
        empty_capture = AudioCapture(capture_id="c-empty", audio_bytes=b"", duration=0.0)
        result = engine.transcribe(empty_capture)
        self.assertEqual(result.status, "empty")
        self.assertEqual(result.text, "")

    def test_sub_quarter_second_audio_dropped_as_silence(self):
        engine = FasterWhisperEngine(model_name="base")
        tiny_capture = AudioCapture(
            capture_id="c-tiny",
            audio_bytes=b"\x00\x00" * 800,  # 0.05s
            duration=0.05,
        )
        result = engine.transcribe(tiny_capture)
        self.assertEqual(result.status, "empty")
        self.assertEqual(result.text, "")


class TranscriptionManagerTest(unittest.TestCase):
    def setUp(self):
        self.ingress = AudioIngress(maxsize=5)
        self.mock_engine = MagicMock(spec=STTEngine)
        self.mock_engine.get_model_name.return_value = "base"
        self.mock_engine.get_device.return_value = "cpu"

    def test_manager_processes_capture_from_ingress(self):
        completed_results = []
        statuses = []

        def on_completed(res):
            completed_results.append(res)

        def on_status(st):
            statuses.append(st.state)

        manager = TranscriptionManager(
            self.ingress,
            engine=self.mock_engine,
            on_status_change=on_status,
            on_transcription_completed=on_completed,
        )

        expected_result = TranscriptionResult(
            capture_id="cap-1",
            text="Hello KIVO",
            language="en",
            status="completed",
        )
        self.mock_engine.transcribe.return_value = expected_result

        manager.start()

        # Submit capture into ingress
        capture = AudioCapture(
            capture_id="cap-1",
            audio_bytes=b"\x10\x20" * 8000,
            duration=1.0,
        )
        self.ingress.submit(capture)

        # Wait for worker to consume
        time.sleep(0.4)

        self.mock_engine.transcribe.assert_called_once()
        self.assertEqual(len(completed_results), 1)
        self.assertEqual(completed_results[0].text, "Hello KIVO")
        self.assertEqual(manager.get_latest_result().text, "Hello KIVO")

        # History recorded
        history = manager.get_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["text"], "Hello KIVO")

        manager.stop()

    def test_manager_handles_transcription_error_gracefully(self):
        failed_errors = []
        manager = TranscriptionManager(
            self.ingress,
            engine=self.mock_engine,
            on_transcription_failed=failed_errors.append,
        )

        self.mock_engine.transcribe.side_effect = STTInferenceError("Out of memory")

        manager.start()
        capture = AudioCapture(
            capture_id="cap-err",
            audio_bytes=b"\x10\x20" * 8000,
            duration=1.0,
        )
        self.ingress.submit(capture)
        time.sleep(0.4)

        self.assertEqual(len(failed_errors), 1)
        self.assertIn("Out of memory", failed_errors[0])
        self.assertEqual(manager.get_latest_result().status, "failed")

        manager.stop()


if __name__ == "__main__":
    unittest.main()
