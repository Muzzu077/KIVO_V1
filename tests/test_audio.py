"""Unit tests for KIVO audio subsystem: device management, capture engine, and ingress."""
from __future__ import annotations

import collections
import time
import unittest
from unittest.mock import MagicMock, patch

from kivo.audio.audio_config import get_selected_device, save_selected_device
from kivo.audio.audio_ingress import AudioIngress
from kivo.audio.audio_models import AudioCapture, AudioDevice, CaptureStatus
from kivo.audio.capture_manager import AudioCaptureManager
from kivo.audio.device_manager import AudioDeviceManager


class AudioModelsTest(unittest.TestCase):
    def test_audio_device_to_dict(self):
        dev = AudioDevice(
            name="Test Mic",
            hostapi="ALSA",
            max_input_channels=2,
            default_samplerate=48000.0,
            is_default=True,
            is_available=True,
            index=3,
        )
        d = dev.to_dict()
        self.assertEqual(d["name"], "Test Mic")
        self.assertEqual(d["hostapi"], "ALSA")
        self.assertEqual(d["max_input_channels"], 2)
        self.assertEqual(d["default_samplerate"], 48000.0)
        self.assertTrue(d["is_default"])
        self.assertTrue(d["is_available"])
        self.assertEqual(d["index"], 3)

    def test_audio_capture_metadata_never_contains_raw_bytes(self):
        raw = b"\x00\x01" * 16000  # 1 second of 16-bit mono PCM
        capture = AudioCapture(
            capture_id="test-cap-123",
            audio_bytes=raw,
            samplerate=16000,
            channels=1,
            encoding="pcm_s16le",
            duration=1.0,
            timestamp=1234567.89,
            device_name="USB Microphone",
        )
        meta = capture.metadata()
        self.assertNotIn("audio_bytes", meta)
        self.assertEqual(meta["capture_id"], "test-cap-123")
        self.assertEqual(meta["byte_length"], len(raw))
        self.assertEqual(meta["duration"], 1.0)
        self.assertEqual(meta["channels"], 1)
        self.assertEqual(meta["samplerate"], 16000)
        self.assertEqual(meta["encoding"], "pcm_s16le")
        self.assertEqual(meta["device_name"], "USB Microphone")

    def test_capture_status_to_dict(self):
        status = CaptureStatus(
            state="capturing",
            duration=2.5,
            device_name="Headset Mic",
            peak_amplitude=0.75,
            rms_amplitude=0.45,
            error_message="",
        )
        d = status.to_dict()
        self.assertEqual(d["state"], "capturing")
        self.assertEqual(d["duration"], 2.5)
        self.assertEqual(d["device_name"], "Headset Mic")
        self.assertEqual(d["peak_amplitude"], 0.75)


class AudioDeviceManagerTest(unittest.TestCase):
    def setUp(self):
        self._cfg = {}
        self.dm = AudioDeviceManager(
            config_getter=lambda: self._cfg.get("input_device", ""),
            config_setter=lambda val: self._cfg.__setitem__("input_device", val),
        )

    @patch("sounddevice.query_devices")
    @patch("sounddevice.query_hostapis")
    def test_device_enumeration_filters_output_and_pseudo_devices(self, mock_hostapis, mock_devices):
        mock_devices.return_value = [
            {"name": "Speakers", "max_input_channels": 0, "max_output_channels": 2, "hostapi": 0},
            {"name": "null", "max_input_channels": 2, "max_output_channels": 0, "hostapi": 0},
            {"name": "dmix", "max_input_channels": 2, "max_output_channels": 0, "hostapi": 0},
            {"name": "Built-in Microphone", "max_input_channels": 2, "max_output_channels": 0, "hostapi": 0, "default_samplerate": 48000.0},
            {"name": "USB Headset Mic", "max_input_channels": 1, "max_output_channels": 0, "hostapi": 0, "default_samplerate": 16000.0},
        ]
        mock_hostapis.return_value = [{"name": "ALSA"}]

        devs = self.dm.list_devices(refresh=True)
        names = [d.name for d in devs]
        self.assertNotIn("Speakers", names)
        self.assertNotIn("null", names)
        self.assertNotIn("dmix", names)
        self.assertIn("Built-in Microphone", names)
        self.assertIn("USB Headset Mic", names)

    @patch("sounddevice.query_devices")
    @patch("sounddevice.query_hostapis")
    def test_device_selection_and_persistence(self, mock_hostapis, mock_devices):
        mock_devices.return_value = [
            {"name": "Mic A", "max_input_channels": 1, "max_output_channels": 0, "hostapi": 0, "default_samplerate": 16000.0},
            {"name": "Mic B", "max_input_channels": 1, "max_output_channels": 0, "hostapi": 0, "default_samplerate": 16000.0},
        ]
        mock_hostapis.return_value = [{"name": "ALSA"}]

        selected = self.dm.select_device("Mic B")
        self.assertEqual(selected.name, "Mic B")
        self.assertEqual(self.dm.get_selected_device_name(), "Mic B")
        self.assertEqual(self._cfg.get("input_device"), "Mic B")

    @patch("sounddevice.query_devices")
    @patch("sounddevice.query_hostapis")
    def test_invalid_device_selection_raises_value_error(self, mock_hostapis, mock_devices):
        mock_devices.return_value = [
            {"name": "Mic A", "max_input_channels": 1, "max_output_channels": 0, "hostapi": 0, "default_samplerate": 16000.0},
        ]
        mock_hostapis.return_value = [{"name": "ALSA"}]

        with self.assertRaises(ValueError):
            self.dm.select_device("Nonexistent Mic")

    @patch("sounddevice.query_devices")
    @patch("sounddevice.query_hostapis")
    def test_disconnected_device_detected_as_unavailable(self, mock_hostapis, mock_devices):
        self._cfg["input_device"] = "Old Disconnected Mic"
        mock_devices.return_value = [
            {"name": "Other Mic", "max_input_channels": 1, "max_output_channels": 0, "hostapi": 0, "default_samplerate": 16000.0},
        ]
        mock_hostapis.return_value = [{"name": "ALSA"}]

        selected = self.dm.get_selected_device()
        self.assertIsNotNone(selected)
        self.assertEqual(selected.name, "Old Disconnected Mic")
        self.assertFalse(selected.is_available)


class AudioIngressTest(unittest.TestCase):
    def test_ingress_queue_fifo_order(self):
        ingress = AudioIngress(maxsize=3)
        c1 = AudioCapture(capture_id="c1", audio_bytes=b"1111", duration=0.1)
        c2 = AudioCapture(capture_id="c2", audio_bytes=b"2222", duration=0.2)
        ingress.submit(c1)
        ingress.submit(c2)
        self.assertEqual(ingress.queue_size(), 2)

        out1 = ingress.get_next(timeout=0.1)
        self.assertEqual(out1.capture_id, "c1")
        out2 = ingress.get_next(timeout=0.1)
        self.assertEqual(out2.capture_id, "c2")
        self.assertEqual(ingress.queue_size(), 0)

    def test_ingress_queue_bounded_drops_oldest(self):
        ingress = AudioIngress(maxsize=2)
        c1 = AudioCapture(capture_id="c1", audio_bytes=b"1111", duration=0.1)
        c2 = AudioCapture(capture_id="c2", audio_bytes=b"2222", duration=0.2)
        c3 = AudioCapture(capture_id="c3", audio_bytes=b"3333", duration=0.3)
        ingress.submit(c1)
        ingress.submit(c2)
        ingress.submit(c3)  # Over capacity, drops c1

        self.assertEqual(ingress.queue_size(), 2)
        out = ingress.get_next(timeout=0.1)
        self.assertEqual(out.capture_id, "c2")

    def test_ingress_clear_drops_all_buffers(self):
        ingress = AudioIngress(maxsize=5)
        ingress.submit(AudioCapture(capture_id="c1", audio_bytes=b"1111", duration=0.1))
        self.assertEqual(ingress.queue_size(), 1)
        ingress.clear()
        self.assertEqual(ingress.queue_size(), 0)
        self.assertIsNone(ingress.get_next(timeout=0.01))


class AudioCaptureManagerTest(unittest.TestCase):
    def setUp(self):
        self.dm = MagicMock(spec=AudioDeviceManager)
        self.dm.get_selected_device_name.return_value = "Mock Mic"
        self.dm.resolve_device_index.return_value = 0

    @patch("sounddevice.InputStream")
    def test_start_and_stop_capture(self, mock_stream_cls):
        mock_stream = MagicMock()
        mock_stream_cls.return_value = mock_stream

        cm = AudioCaptureManager(self.dm)
        self.assertEqual(cm.get_status().state, "idle")

        started = cm.start_capture()
        self.assertTrue(started)
        self.assertEqual(cm.get_status().state, "capturing")
        mock_stream.start.assert_called_once()

        # Cannot start again while capturing
        self.assertFalse(cm.start_capture())

        # Simulate audio chunk
        cm._audio_callback(indata=b"\x00\x00" * 512, frames=512, time_info={}, status=None)

        capture = cm.stop_capture()
        self.assertIsNotNone(capture)
        self.assertEqual(cm.get_status().state, "idle")
        mock_stream.stop.assert_called_once()
        self.assertGreaterEqual(mock_stream.close.call_count, 1)

    @patch("sounddevice.InputStream")
    def test_cancel_capture_discards_audio(self, mock_stream_cls):
        mock_stream = MagicMock()
        mock_stream_cls.return_value = mock_stream

        completed_captures = []
        cm = AudioCaptureManager(self.dm, on_complete=completed_captures.append)
        cm.start_capture()
        cm._audio_callback(indata=b"\x10\x20" * 512, frames=512, time_info={}, status=None)

        cancelled = cm.cancel_capture()
        self.assertTrue(cancelled)
        self.assertEqual(cm.get_status().state, "idle")
        self.assertEqual(len(completed_captures), 0)

    @patch("sounddevice.InputStream")
    def test_stop_while_idle_returns_none(self, mock_stream_cls):
        cm = AudioCaptureManager(self.dm)
        self.assertIsNone(cm.stop_capture())

    @patch("sounddevice.InputStream")
    def test_close_during_recording_cleans_up(self, mock_stream_cls):
        mock_stream = MagicMock()
        mock_stream_cls.return_value = mock_stream

        cm = AudioCaptureManager(self.dm)
        cm.start_capture()
        cm.close()
        self.assertEqual(cm.get_status().state, "idle")
        self.assertGreaterEqual(mock_stream.close.call_count, 1)
        # Cannot start after close
        self.assertFalse(cm.start_capture())


if __name__ == "__main__":
    unittest.main()
