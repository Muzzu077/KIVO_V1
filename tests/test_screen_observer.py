import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from core.desktop_context import WindowInfo
from core.screen_observer import ScreenObserver, ScreenObservation


class TestScreenObserver(unittest.TestCase):
    def setUp(self):
        self.observer = ScreenObserver(
            interval_active=1.0,
            interval_idle=4.0,
            change_threshold=0.04,
        )

    def test_difference_calculation(self):
        img1 = np.zeros((100, 100), dtype=np.float32)
        img2 = np.zeros((100, 100), dtype=np.float32)
        self.assertEqual(float(np.mean(np.abs(img1 - img2)) / 255.0), 0.0)

        img3 = np.ones((100, 100), dtype=np.float32) * 255.0
        self.assertAlmostEqual(float(np.mean(np.abs(img1 - img3)) / 255.0), 1.0, places=4)

    def test_pause_resume(self):
        self.observer.pause()
        self.assertTrue(self.observer._paused)
        self.observer.resume()
        self.assertFalse(self.observer._paused)

    def test_pause_clears_cached_frame(self):
        self.observer._last_observation = ScreenObservation(
            timestamp=0.0, frame_jpeg=b"old-frame", ocr_text_preview="secret"
        )
        self.observer.pause()
        self.assertIsNone(self.observer._last_observation)
        with self.assertRaises(RuntimeError):
            self.observer.get_latest_frame()

    def test_capture_observation_refuses_while_paused(self):
        self.observer.pause()
        with patch.object(self.observer, "_grab_screen_pil") as grab:
            self.assertIsNone(self.observer.capture_observation())
            grab.assert_not_called()

    def test_ocr_crops_only_when_position_is_known(self):
        img = Image.new("RGB", (400, 300), "white")

        with patch("pytesseract.image_to_string", return_value="text") as ocr:
            # niri: size known, position unknown -> full frame, no crop
            unknown = WindowInfo(title="x", size=(100, 100), position=None)
            self.observer._run_ocr(img, unknown)
            first = ocr.call_args[0][0]
            self.assertEqual(first.size, img.size)

            # X11/Hyprland: position known -> crop to the window
            known = WindowInfo(title="x", size=(100, 100), position=(50, 40))
            self.observer._run_ocr(img, known)
            second = ocr.call_args[0][0]
            self.assertEqual(second.size, (100, 100))


if __name__ == "__main__":
    unittest.main()
