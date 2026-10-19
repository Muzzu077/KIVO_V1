#!/usr/bin/env python3
"""
Benchmark startup performance, module import latencies, and service memory footprint.
"""

import os
import sys
import time
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import psutil

def measure_import(name: str):
    t0 = time.perf_counter()
    __import__(name)
    elapsed = (time.perf_counter() - t0) * 1000.0
    return elapsed

def main():
    print("=" * 60)
    print(" MARK-LV STARTUP & PERFORMANCE BENCHMARK")
    print("=" * 60)

    process = psutil.Process(os.getpid())
    init_mem = process.memory_info().rss / (1024 * 1024)
    print(f"Base Python RSS Memory: {init_mem:.2f} MB\n")

    modules_to_test = [
        ("core.action_policy", "Action Policy Engine"),
        ("core.desktop_context", "Desktop Context (niri/Wayland)"),
        ("core.terminal_control", "Terminal Control Adapter"),
        ("core.speaker_verifier", "Biometric Speaker Verifier"),
        ("core.screen_observer", "Continuous Screen Observer"),
        ("core.task_orchestrator", "Multi-step Task Orchestrator"),
        ("actions.action_loader", "Action Loader Framework", "core.action_loader"),
        ("actions.browser_control", "Browser Control (Playwright)"),
    ]

    total_time = 0.0
    for item in modules_to_test:
        mod = item[2] if len(item) > 2 else item[0]
        desc = item[1]
        t = measure_import(mod)
        total_time += t
        print(f"  • {desc:<35} : {t:7.2f} ms")

    print(f"\nTotal Core Module Import Latency : {total_time:.2f} ms")

    # Benchmark Screen Capture & OCR latency
    print("\nBenchmarking Screen Capture & OCR Performance...")
    from core.screen_observer import ScreenObserver
    obs = ScreenObserver()

    t_cap_0 = time.perf_counter()
    jpeg_bytes, method = obs._capture_frame_jpeg()
    t_cap = (time.perf_counter() - t_cap_0) * 1000.0
    print(f"  • Screen Capture ({method:<10})      : {t_cap:7.2f} ms ({len(jpeg_bytes)/1024:.1f} KB)")

    if jpeg_bytes:
        from core.desktop_context import get_desktop_context
        ctx = get_desktop_context()
        t_ocr_0 = time.perf_counter()
        ocr_text = ctx.ocr_screenshot()
        t_ocr = (time.perf_counter() - t_ocr_0) * 1000.0
        print(f"  • OCR Processing (pytesseract)   : {t_ocr:7.2f} ms (extracted {len(ocr_text)} chars)")

    # Benchmark Speaker Verifier Feature Extraction Latency
    print("\nBenchmarking Biometric Voiceprint Feature Extraction...")
    import numpy as np
    from core.speaker_verifier import get_speaker_verifier
    verifier = get_speaker_verifier()
    dummy_audio = np.random.randn(16000 * 2).astype(np.float32) * 0.1 # 2 seconds of audio
    t_v0 = time.perf_counter()
    is_match, score = verifier.verify(dummy_audio, 16000)
    t_v = (time.perf_counter() - t_v0) * 1000.0
    print(f"  • 2-sec Audio Verification Latency : {t_v:7.2f} ms (match={is_match}, score={score:.3f})")

    # Final Memory
    final_mem = process.memory_info().rss / (1024 * 1024)
    print(f"\nFinal Process RSS Memory          : {final_mem:.2f} MB (delta: +{final_mem - init_mem:.2f} MB)")
    print("=" * 60)

if __name__ == "__main__":
    main()
