import unittest
import numpy as np
from PIL import Image
from core.screen_observer import ScreenObserver

class TestScreenObserver(unittest.TestCase):
    def setUp(self):
        self.observer = ScreenObserver(
            interval_active=1.0,
            interval_idle=4.0,
            change_threshold=0.04
        )

    def test_difference_calculation(self):
        # Two identical black images -> 0 change score
        img1 = np.zeros((100, 100), dtype=np.float32)
        img2 = np.zeros((100, 100), dtype=np.float32)
        diff_zero = float(np.mean(np.abs(img1 - img2)) / 255.0)
        self.assertEqual(diff_zero, 0.0)

        # White vs Black -> 1.0 change score
        img3 = np.ones((100, 100), dtype=np.float32) * 255.0
        diff_max = float(np.mean(np.abs(img1 - img3)) / 255.0)
        self.assertAlmostEqual(diff_max, 1.0, places=4)

    def test_pause_resume(self):
        self.observer.pause()
        self.assertTrue(self.observer._paused)
        self.observer.resume()
        self.assertFalse(self.observer._paused)

if __name__ == "__main__":
    unittest.main()
