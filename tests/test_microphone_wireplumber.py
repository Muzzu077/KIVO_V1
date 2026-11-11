"""Unit and adversarial tests for WpctlRunner and WirePlumber output parsing.

Every test uses mocked subprocess runners or fixture strings; no actual audio
commands are executed against the host.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from kivo.core.bounded_process import BoundedProcessResult
from kivo.microphone.models import (
    ERROR_COMMAND_FAILED,
    ERROR_COMMAND_TIMEOUT,
    ERROR_INVALID_OUTPUT,
    ERROR_NO_DEFAULT_SOURCE,
    ERROR_WPCTL_MISSING,
    MicSource,
)
from kivo.microphone.wireplumber import VolumeReading, WpctlRunner


def _make_runner(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
):
    def runner(argv, **kw):
        return BoundedProcessResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
        )

    return runner


class WpctlSourceDiscoveryTests(unittest.TestCase):
    # ── Default source available ──────────────────────────────────────────
    def test_default_source_available(self):
        output = (
            "84\talsa_input.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__Mic1__source\taudio/source\t*\n"
            "85\tsink_input.monitor\taudio/sink\n"
        )
        wp = WpctlRunner(
            wpctl_path="/usr/bin/wpctl",
            runner=_make_runner(stdout=output),
        )
        source, err_code, err_msg = wp.list_default_source()
        self.assertIsNotNone(source)
        self.assertIsNone(err_code)
        self.assertEqual(source.id, 84)
        self.assertTrue(source.is_default)
        self.assertIn("alsa_input", source.name)
        self.assertEqual(source.description, "Internal Microphone")

    # ── Default source missing ────────────────────────────────────────────
    def test_no_default_marked_source(self):
        output = (
            "84\talsa_input.mic\taudio/source\n"
            "85\tsink_input.monitor\taudio/source\n"
        )
        wp = WpctlRunner(
            wpctl_path="/usr/bin/wpctl",
            runner=_make_runner(stdout=output),
        )
        source, err_code, err_msg = wp.list_default_source()
        self.assertIsNone(source)
        self.assertEqual(err_code, ERROR_NO_DEFAULT_SOURCE)

    # ── Multiple default sources marked ───────────────────────────────────
    def test_multiple_defaults_marked_rejected(self):
        output = (
            "84\tmic1\taudio/source\t*\n"
            "85\tmic2\taudio/source\t*\n"
        )
        wp = WpctlRunner(
            wpctl_path="/usr/bin/wpctl",
            runner=_make_runner(stdout=output),
        )
        source, err_code, err_msg = wp.list_default_source()
        self.assertIsNone(source)
        self.assertEqual(err_code, ERROR_INVALID_OUTPUT)

    # ── wpctl missing ─────────────────────────────────────────────────────
    def test_wpctl_missing(self):
        wp = WpctlRunner(wpctl_path=None)
        source, err_code, err_msg = wp.list_default_source()
        self.assertIsNone(source)
        self.assertEqual(err_code, ERROR_WPCTL_MISSING)

    # ── Nonzero exit / timeout ────────────────────────────────────────────
    def test_command_timeout(self):
        wp = WpctlRunner(
            wpctl_path="/usr/bin/wpctl",
            runner=_make_runner(timed_out=True),
        )
        source, err_code, err_msg = wp.list_default_source()
        self.assertIsNone(source)
        self.assertEqual(err_code, ERROR_COMMAND_TIMEOUT)

    def test_command_failed(self):
        wp = WpctlRunner(
            wpctl_path="/usr/bin/wpctl",
            runner=_make_runner(returncode=1, stderr="WirePlumber not running"),
        )
        source, err_code, err_msg = wp.list_default_source()
        self.assertIsNone(source)
        self.assertEqual(err_code, ERROR_COMMAND_FAILED)

    # ── Malformed source listings ─────────────────────────────────────────
    def test_empty_stdout(self):
        wp = WpctlRunner(
            wpctl_path="/usr/bin/wpctl",
            runner=_make_runner(stdout=""),
        )
        source, err_code, err_msg = wp.list_default_source()
        self.assertIsNone(source)
        self.assertEqual(err_code, ERROR_INVALID_OUTPUT)

    def test_invalid_node_id(self):
        output = "not_an_id\tmic_name\taudio/source\t*\n"
        wp = WpctlRunner(
            wpctl_path="/usr/bin/wpctl",
            runner=_make_runner(stdout=output),
        )
        source, err_code, err_msg = wp.list_default_source()
        self.assertIsNone(source)
        self.assertEqual(err_code, ERROR_NO_DEFAULT_SOURCE)

    def test_monitor_source_rejected(self):
        # Media class is audio/sink or audio/video, not audio/source
        output = "84\tsink_monitor\taudio/sink\t*\n"
        wp = WpctlRunner(
            wpctl_path="/usr/bin/wpctl",
            runner=_make_runner(stdout=output),
        )
        source, err_code, err_msg = wp.list_default_source()
        self.assertIsNone(source)
        self.assertEqual(err_code, ERROR_NO_DEFAULT_SOURCE)

    def test_unusual_characters_sanitized(self):
        # Bidi override and control characters in name
        bidi_name = "mic\u202e_reversed\x07_control"
        output = f"84\t{bidi_name}\taudio/source\t*\n"
        # Since _NODE_NAME_PATTERN requires [A-Za-z0-9_.:-], bidi names are rejected by pattern
        wp = WpctlRunner(
            wpctl_path="/usr/bin/wpctl",
            runner=_make_runner(stdout=output),
        )
        source, err_code, err_msg = wp.list_default_source()
        # Rejected because node.name doesn't match safe regex
        self.assertIsNone(source)


class WpctlVolumeParsingTests(unittest.TestCase):
    # ── Normal unmuted output ─────────────────────────────────────────────
    def test_normal_unmuted(self):
        wp = WpctlRunner(
            wpctl_path="/usr/bin/wpctl",
            runner=_make_runner(stdout="Volume: 0.38\n"),
        )
        reading, err_code, err_msg = wp.get_volume(84)
        self.assertIsNotNone(reading)
        self.assertIsNone(err_code)
        self.assertAlmostEqual(reading.volume, 0.38)
        self.assertFalse(reading.muted)

    # ── Muted output ──────────────────────────────────────────────────────
    def test_normal_muted(self):
        wp = WpctlRunner(
            wpctl_path="/usr/bin/wpctl",
            runner=_make_runner(stdout="Volume: 0.38 [MUTED]\n"),
        )
        reading, err_code, err_msg = wp.get_volume(84)
        self.assertIsNotNone(reading)
        self.assertIsNone(err_code)
        self.assertAlmostEqual(reading.volume, 0.38)
        self.assertTrue(reading.muted)

    # ── Amplification > 1.0 ───────────────────────────────────────────────
    def test_volume_above_100_percent(self):
        wp = WpctlRunner(
            wpctl_path="/usr/bin/wpctl",
            runner=_make_runner(stdout="Volume: 1.45 [MUTED]\n"),
        )
        reading, err_code, err_msg = wp.get_volume(84)
        self.assertIsNotNone(reading)
        self.assertAlmostEqual(reading.volume, 1.45)
        self.assertTrue(reading.muted)

    # ── Exit 0 with empty stdout (Node not found) ─────────────────────────
    def test_exit_zero_empty_stdout_with_stderr(self):
        wp = WpctlRunner(
            wpctl_path="/usr/bin/wpctl",
            runner=_make_runner(
                returncode=0,
                stdout="",
                stderr="Node '99999' not found\n",
            ),
        )
        reading, err_code, err_msg = wp.get_volume(99999)
        self.assertIsNone(reading)
        self.assertEqual(err_code, ERROR_INVALID_OUTPUT)
        self.assertIn("not found", err_msg.lower())

    # ── Empty stdout with exit 0 and no stderr ─────────────────────────────
    def test_exit_zero_completely_empty(self):
        wp = WpctlRunner(
            wpctl_path="/usr/bin/wpctl",
            runner=_make_runner(returncode=0, stdout="", stderr=""),
        )
        reading, err_code, err_msg = wp.get_volume(84)
        self.assertIsNone(reading)
        self.assertEqual(err_code, ERROR_INVALID_OUTPUT)

    # ── Nonzero exit ──────────────────────────────────────────────────────
    def test_nonzero_exit(self):
        wp = WpctlRunner(
            wpctl_path="/usr/bin/wpctl",
            runner=_make_runner(returncode=1, stderr="Connection refused"),
        )
        reading, err_code, err_msg = wp.get_volume(84)
        self.assertIsNone(reading)
        self.assertEqual(err_code, ERROR_COMMAND_FAILED)

    # ── Timeout ───────────────────────────────────────────────────────────
    def test_timeout(self):
        wp = WpctlRunner(
            wpctl_path="/usr/bin/wpctl",
            runner=_make_runner(timed_out=True),
        )
        reading, err_code, err_msg = wp.get_volume(84)
        self.assertIsNone(reading)
        self.assertEqual(err_code, ERROR_COMMAND_TIMEOUT)

    # ── Malformed volume formats ──────────────────────────────────────────
    def test_malformed_text(self):
        for bad in ("Volume:", "Invalid line", "Volume: abc", "Volume: -0.5"):
            wp = WpctlRunner(
                wpctl_path="/usr/bin/wpctl",
                runner=_make_runner(stdout=bad + "\n"),
            )
            reading, err_code, err_msg = wp.get_volume(84)
            self.assertIsNone(reading, f"Failed to reject: {bad!r}")
            self.assertEqual(err_code, ERROR_INVALID_OUTPUT)


class WpctlSetMuteTests(unittest.TestCase):
    # ── Explicit mute (1) and unmute (0) ──────────────────────────────────
    def test_explicit_mute_args(self):
        captured_argv = []

        def capture_runner(argv, **kw):
            captured_argv.append(list(argv))
            return BoundedProcessResult(returncode=0, stdout="", stderr="")

        wp = WpctlRunner(
            wpctl_path="/usr/bin/wpctl",
            runner=capture_runner,
        )
        ok, err_code, _ = wp.set_mute(84, True)
        self.assertTrue(ok)
        self.assertEqual(captured_argv[-1], ["/usr/bin/wpctl", "set-mute", "84", "1"])

        ok, err_code, _ = wp.set_mute(84, False)
        self.assertTrue(ok)
        self.assertEqual(captured_argv[-1], ["/usr/bin/wpctl", "set-mute", "84", "0"])

    # ── Invalid node IDs rejected ─────────────────────────────────────────
    def test_invalid_node_id_rejected(self):
        wp = WpctlRunner(wpctl_path="/usr/bin/wpctl")
        for bad_id in (0, -1, 99999999999999, "not_int"):
            ok, err_code, _ = wp.set_mute(bad_id, True)
            self.assertFalse(ok)
            self.assertEqual(err_code, ERROR_INVALID_OUTPUT)


if __name__ == "__main__":
    unittest.main()
