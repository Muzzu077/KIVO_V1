"""Focused and adversarial tests for the telemetry readers and models.

Every reader is exercised against deterministic fake filesystem trees built in
tmpdir.  No real ``/proc``, ``/sys`` or GPU driver is touched.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from kivo.telemetry.models import (
    SCHEMA_VERSION,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    STATUS_UNSUPPORTED,
    STATUS_WARMING_UP,
    BatteryMetrics,
    CpuFrequency,
    CpuMetrics,
    CpuUtilization,
    GpuMetrics,
    Metric,
    MemoryMetrics,
    StorageMetrics,
    TelemetrySnapshot,
    TemperatureReading,
)
from kivo.telemetry.readers import (
    BatteryReader,
    CpuReader,
    FrequencyReader,
    MemoryReader,
    StorageReader,
    TelemetryPaths,
    TemperatureReader,
    read_cpu_info,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _paths_in(root: Path) -> TelemetryPaths:
    return TelemetryPaths(
        proc_stat=root / "proc" / "stat",
        proc_meminfo=root / "proc" / "meminfo",
        proc_cpuinfo=root / "proc" / "cpuinfo",
        sys_cpu=root / "sys" / "devices" / "system" / "cpu",
        hwmon=root / "sys" / "class" / "hwmon",
        thermal=root / "sys" / "class" / "thermal",
        power_supply=root / "sys" / "class" / "power_supply",
        root_mount="/",
    )


# ╶── CPU ──────────────────────────────────────────────────────────────────

PROC_STAT_SAMPLE_1 = (
    "cpu  10000 200 3000 50000 400 100 50 0 0 0\n"
    "cpu0 5000 100 1500 25000 200 50 25 0 0 0\n"
)
PROC_STAT_SAMPLE_2 = (
    "cpu  10500 210 3100 50500 410 110 55 0 0 0\n"
    "cpu0 5250 105 1550 25250 205 55 27 0 0 0\n"
)


class CpuReaderTests(unittest.TestCase):
    # ── T01: two valid samples ────────────────────────────────────────────
    def test_two_valid_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths_in(root)
            _write(paths.proc_stat, PROC_STAT_SAMPLE_1)
            reader = CpuReader(paths)
            first = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertEqual(first.status, STATUS_WARMING_UP)
            self.assertIsNone(first.percent)

            _write(paths.proc_stat, PROC_STAT_SAMPLE_2)
            second = reader.sample(sampled_at=2.0, sampled_monotonic=101.0)
            self.assertEqual(second.status, STATUS_OK)
            self.assertIsNotNone(second.percent)
            self.assertGreaterEqual(second.percent, 0.0)
            self.assertLessEqual(second.percent, 100.0)
            # Sample 1: total=63750 idle=50400; Sample 2: total=64885 idle=50910
            # delta_total=1135, delta_idle=510, percent=100*(1135-510)/1135≈55.07
            self.assertAlmostEqual(second.percent, 55.07, delta=0.5)

    # ── T02: first sample warming_up ──────────────────────────────────────
    def test_first_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths_in(root)
            _write(paths.proc_stat, PROC_STAT_SAMPLE_1)
            reader = CpuReader(paths)
            result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertEqual(result.status, STATUS_WARMING_UP)
            self.assertIsNone(result.percent)

    # ── T03: counter reset ────────────────────────────────────────────────
    def test_counter_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths_in(root)
            _write(paths.proc_stat, PROC_STAT_SAMPLE_2)
            reader = CpuReader(paths)
            reader.sample(sampled_at=1.0, sampled_monotonic=100.0)

            # Counters go backward
            _write(paths.proc_stat, PROC_STAT_SAMPLE_1)
            result = reader.sample(sampled_at=2.0, sampled_monotonic=101.0)
            self.assertEqual(result.status, STATUS_WARMING_UP)
            self.assertEqual(result.error, "counter_reset")
            self.assertIsNone(result.percent)

            # Next normal sample recovers
            _write(paths.proc_stat, PROC_STAT_SAMPLE_2)
            third = reader.sample(sampled_at=3.0, sampled_monotonic=102.0)
            self.assertEqual(third.status, STATUS_OK)
            self.assertIsNotNone(third.percent)

    # ── T04: zero delta ───────────────────────────────────────────────────
    def test_zero_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths_in(root)
            _write(paths.proc_stat, PROC_STAT_SAMPLE_1)
            reader = CpuReader(paths)
            reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            result = reader.sample(sampled_at=2.0, sampled_monotonic=101.0)
            self.assertEqual(result.status, STATUS_WARMING_UP)
            self.assertIsNone(result.percent)

    # ── T05: malformed /proc/stat ─────────────────────────────────────────
    def test_malformed_proc_stat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths_in(root)
            _write(paths.proc_stat, "cpu  abc def\nother stuff\n")
            reader = CpuReader(paths)
            result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertEqual(result.status, STATUS_ERROR)

    # ── T06: missing /proc/stat ───────────────────────────────────────────
    def test_missing_proc_stat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths_in(root)
            reader = CpuReader(paths)
            result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertEqual(result.status, STATUS_UNAVAILABLE)

    # ── T05b: idle CPU valid zero ─────────────────────────────────────────
    def test_idle_cpu_zero_utilization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths_in(root)
            _write(paths.proc_stat, "cpu  100 0 0 50000 0 0 0 0 0 0\n")
            reader = CpuReader(paths)
            reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            _write(paths.proc_stat, "cpu  100 0 0 51000 0 0 0 0 0 0\n")
            result = reader.sample(sampled_at=2.0, sampled_monotonic=101.0)
            self.assertEqual(result.status, STATUS_OK)
            self.assertEqual(result.percent, 0.0)


# ╶── Memory ───────────────────────────────────────────────────────────────

MEMINFO_NORMAL = """\
MemTotal:       16076116 kB
MemFree:         3200000 kB
MemAvailable:   10476272 kB
Buffers:          654321 kB
Cached:          5432100 kB
"""


class MemoryReaderTests(unittest.TestCase):
    # ── T09: valid readings ───────────────────────────────────────────────
    def test_normal_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths_in(root)
            _write(paths.proc_meminfo, MEMINFO_NORMAL)
            reader = MemoryReader(paths)
            result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertEqual(result.status, STATUS_OK)
            self.assertEqual(result.total_bytes, 16076116 * 1024)
            self.assertEqual(result.available_bytes, 10476272 * 1024)
            used = 16076116 * 1024 - 10476272 * 1024
            self.assertEqual(result.used_bytes, used)
            self.assertIsNotNone(result.percent)
            self.assertGreaterEqual(result.percent, 0.0)
            self.assertLessEqual(result.percent, 100.0)

    # ── T10: MemAvailable missing, fallback ───────────────────────────────
    def test_memavailable_fallback(self):
        data = (
            "MemTotal:       16000000 kB\n"
            "MemFree:        4000000 kB\n"
            "Buffers:        1000000 kB\n"
            "Cached:         3000000 kB\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths_in(root)
            _write(paths.proc_meminfo, data)
            reader = MemoryReader(paths)
            result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertEqual(result.status, STATUS_OK)
            # free+buffers+cached = 8000000 kB
            self.assertEqual(result.available_bytes, 8000000 * 1024)

    # ── T11: total 0 / malformed ──────────────────────────────────────────
    def test_total_zero(self):
        data = "MemTotal:       0 kB\nMemAvailable:   0 kB\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths_in(root)
            _write(paths.proc_meminfo, data)
            reader = MemoryReader(paths)
            result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertIn(result.status, (STATUS_ERROR,))

    def test_malformed_meminfo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths_in(root)
            _write(paths.proc_meminfo, "garbage data here\n")
            reader = MemoryReader(paths)
            result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertIn(result.status, (STATUS_ERROR,))

    def test_missing_meminfo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths_in(root)
            reader = MemoryReader(paths)
            result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertEqual(result.status, STATUS_UNAVAILABLE)


# ╶── CPU Frequency ────────────────────────────────────────────────────────


class FrequencyReaderTests(unittest.TestCase):
    # ── T07: all cores present ────────────────────────────────────────────
    def test_multiple_policies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths_in(root)
            for index in range(4):
                cpu = paths.sys_cpu / f"cpu{index}" / "cpufreq"
                _write(cpu / "scaling_cur_freq", f"{2400000 + index * 100000}\n")
                _write(cpu / "cpuinfo_max_freq", "4800000\n")
            reader = FrequencyReader(paths)
            result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertEqual(result.status, STATUS_OK)
            self.assertIsNotNone(result.mhz)
            self.assertAlmostEqual(result.mhz, 2550.0, places=0)
            self.assertEqual(result.max_mhz, 4800.0)

    # ── T08: missing/partial/invalid ──────────────────────────────────────
    def test_missing_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths_in(root)
            reader = FrequencyReader(paths)
            result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertEqual(result.status, STATUS_UNAVAILABLE)

    def test_invalid_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths_in(root)
            cpu = paths.sys_cpu / "cpu0" / "cpufreq"
            _write(cpu / "scaling_cur_freq", "not_a_number\n")
            _write(cpu / "cpuinfo_max_freq", "4800000\n")
            reader = FrequencyReader(paths)
            result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertIn(result.status, (STATUS_ERROR, STATUS_UNAVAILABLE))

    def test_partial_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths_in(root)
            _write(
                paths.sys_cpu / "cpu0" / "cpufreq" / "scaling_cur_freq",
                "2400000\n",
            )
            _write(
                paths.sys_cpu / "cpu1" / "cpufreq" / "scaling_cur_freq",
                "garbage\n",
            )
            reader = FrequencyReader(paths)
            result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertEqual(result.status, STATUS_OK)
            self.assertAlmostEqual(result.mhz, 2400.0, places=0)


# ╶── Temperatures ─────────────────────────────────────────────────────────


class TemperatureReaderTests(unittest.TestCase):
    def _make_hwmon(self, root: Path, name: str, temps: dict[str, tuple[str, int]]):
        base = root / "sys" / "class" / "hwmon"
        existing = list(base.glob("hwmon*")) if base.exists() else []
        index = len(existing)
        hwmon_dir = base / f"hwmon{index}"
        _write(hwmon_dir / "name", name)
        for filename, (label, millidegrees) in temps.items():
            _write(hwmon_dir / f"{filename}_input", str(millidegrees))
            if label:
                _write(hwmon_dir / f"{filename}_label", label)

    def _make_thermal(self, root: Path, zone_type: str, millidegrees: int, index: int = 0):
        zone = root / "sys" / "class" / "thermal" / f"thermal_zone{index}"
        _write(zone / "type", zone_type)
        _write(zone / "temp", str(millidegrees))

    # ── T12: coretemp package priority ────────────────────────────────────
    def test_coretemp_package_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_hwmon(root, "coretemp", {
                "temp1": ("Package id 0", 55000),
                "temp2": ("Core 0", 52000),
            })
            paths = _paths_in(root)
            reader = TemperatureReader(paths)
            readings = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            cpu = next((r for r in readings if r.id == "cpu_package"), None)
            self.assertIsNotNone(cpu)
            self.assertEqual(cpu.status, STATUS_OK)
            self.assertAlmostEqual(cpu.celsius, 55.0)

    # ── T13: thermal fallback with correct priority ───────────────────────
    def test_thermal_fallback_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_thermal(root, "x86_pkg_temp", 60000, 0)
            self._make_thermal(root, "acpitz", 45000, 1)
            paths = _paths_in(root)
            reader = TemperatureReader(paths)
            readings = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            cpu = next((r for r in readings if r.id == "cpu_package"), None)
            self.assertIsNotNone(cpu)
            self.assertAlmostEqual(cpu.celsius, 60.0)

    # ── T14: NVMe/WiFi discovery ──────────────────────────────────────────
    def test_nvme_and_wifi_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_hwmon(root, "coretemp", {"temp1": ("Package id 0", 55000)})
            self._make_hwmon(root, "nvme", {"temp1": ("Composite", 39850)})
            self._make_hwmon(root, "iwlwifi_1", {"temp1": ("", 44000)})
            paths = _paths_in(root)
            reader = TemperatureReader(paths)
            readings = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            ids = [r.id for r in readings if r.status == STATUS_OK]
            self.assertIn("cpu_package", ids)
            self.assertIn("nvme_composite", ids)
            self.assertIn("wifi", ids)

    # ── T15: unlabelled acer/spd5118 excluded ─────────────────────────────
    def test_acer_spd5118_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_hwmon(root, "acer", {"temp1": ("", 42000)})
            self._make_hwmon(root, "spd5118", {"temp1": ("", 38000)})
            paths = _paths_in(root)
            reader = TemperatureReader(paths)
            readings = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            ids = [r.id for r in readings if r.status == STATUS_OK]
            self.assertNotIn("acer", ids)
            self.assertNotIn("spd5118", ids)

    # ── T16: out-of-range temperature ─────────────────────────────────────
    def test_out_of_range_temperature(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_hwmon(root, "coretemp", {"temp1": ("Package id 0", 200000)})
            paths = _paths_in(root)
            reader = TemperatureReader(paths)
            readings = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            cpu = next((r for r in readings if r.id == "cpu_package"), None)
            self.assertIsNotNone(cpu)
            self.assertEqual(cpu.status, STATUS_ERROR)
            self.assertIsNone(cpu.celsius)

    # ── Sensor disappearance and rediscovery ──────────────────────────────
    def test_sensor_disappearance_triggers_rediscovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_hwmon(root, "coretemp", {"temp1": ("Package id 0", 55000)})
            paths = _paths_in(root)
            reader = TemperatureReader(paths)
            readings = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertTrue(any(r.id == "cpu_package" and r.status == STATUS_OK for r in readings))

            # Delete the sensor file
            sensor_path = next(
                paths.hwmon.glob("hwmon*/temp1_input")
            )
            sensor_path.unlink()
            readings = reader.sample(sampled_at=2.0, sampled_monotonic=101.0)
            cpu = next((r for r in readings if r.id == "cpu_package"), None)
            self.assertIsNotNone(cpu)
            self.assertIn(cpu.status, (STATUS_UNAVAILABLE, STATUS_UNSUPPORTED))


# ╶── Battery ──────────────────────────────────────────────────────────────

def _make_battery(root: Path, name: str = "BAT1", fields: dict[str, str] | None = None):
    base = root / "sys" / "class" / "power_supply" / name
    _write(base / "type", "Battery")
    fields = fields or {}
    for key, value in fields.items():
        _write(base / key, value)


def _make_ac(root: Path, name: str = "AC0", online: str = "1"):
    base = root / "sys" / "class" / "power_supply" / name
    _write(base / "type", "Mains")
    _write(base / "online", online)


class BatteryReaderTests(unittest.TestCase):
    # ── T17: charge fields only ───────────────────────────────────────────
    def test_charge_fields_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_battery(root, fields={
                "present": "1",
                "status": "Discharging",
                "charge_now": "2000000",
                "charge_full": "5000000",
            })
            _make_ac(root, online="0")
            paths = _paths_in(root)
            reader = BatteryReader(paths)
            result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertEqual(result.status, STATUS_OK)
            self.assertTrue(result.present)
            self.assertEqual(result.percent, 40)
            self.assertEqual(result.state, "discharging")
            self.assertFalse(result.ac_online)

    # ── T18: energy fields only ───────────────────────────────────────────
    def test_energy_fields_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_battery(root, fields={
                "present": "1",
                "status": "Charging",
                "energy_now": "35000000",
                "energy_full": "50000000",
            })
            _make_ac(root, online="1")
            paths = _paths_in(root)
            reader = BatteryReader(paths)
            result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertEqual(result.status, STATUS_OK)
            self.assertEqual(result.percent, 70)
            self.assertEqual(result.state, "charging")
            self.assertTrue(result.ac_online)

    # ── T19: no battery ───────────────────────────────────────────────────
    def test_no_battery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths_in(root)
            reader = BatteryReader(paths)
            result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertEqual(result.status, STATUS_UNSUPPORTED)
            self.assertFalse(result.present)

    # ── T19b: present=0 means absent ─────────────────────────────────────
    def test_battery_present_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_battery(root, fields={"present": "0"})
            paths = _paths_in(root)
            reader = BatteryReader(paths)
            result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertEqual(result.status, STATUS_UNSUPPORTED)

    # ── T20: AC online/offline ────────────────────────────────────────────
    def test_ac_online_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_battery(root, fields={"present": "1", "capacity": "60", "status": "Charging"})
            _make_ac(root, online="1")
            paths = _paths_in(root)
            reader = BatteryReader(paths)
            result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertTrue(result.ac_online)

    def test_ac_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_battery(root, fields={"present": "1", "capacity": "40", "status": "Discharging"})
            _make_ac(root, online="0")
            paths = _paths_in(root)
            reader = BatteryReader(paths)
            result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertFalse(result.ac_online)

    def test_no_ac_yields_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_battery(root, fields={"present": "1", "capacity": "40", "status": "Full"})
            paths = _paths_in(root)
            reader = BatteryReader(paths)
            result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertIsNone(result.ac_online)

    # ── T21: malformed/partial ────────────────────────────────────────────
    def test_battery_no_percent_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_battery(root, fields={"present": "1", "status": "Unknown"})
            paths = _paths_in(root)
            reader = BatteryReader(paths)
            result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertEqual(result.status, STATUS_ERROR)
            self.assertTrue(result.present)

    # ── capacity field ────────────────────────────────────────────────────
    def test_capacity_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_battery(root, fields={
                "present": "1",
                "capacity": "85",
                "status": "Full",
            })
            paths = _paths_in(root)
            reader = BatteryReader(paths)
            result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
            self.assertEqual(result.status, STATUS_OK)
            self.assertEqual(result.percent, 85)
            self.assertEqual(result.state, "full")


# ╶── Storage ──────────────────────────────────────────────────────────────


class StorageReaderTests(unittest.TestCase):
    def _mock_statvfs(self, f_bsize=4096, f_frsize=4096, f_blocks=100000,
                      f_bfree=30000, f_bavail=25000, f_files=0,
                      f_ffree=0, f_favail=0, f_flag=0, f_namemax=255):
        result = MagicMock()
        result.f_bsize = f_bsize
        result.f_frsize = f_frsize
        result.f_blocks = f_blocks
        result.f_bfree = f_bfree
        result.f_bavail = f_bavail
        result.f_files = f_files
        result.f_ffree = f_ffree
        result.f_favail = f_favail
        result.f_flag = f_flag
        result.f_namemax = f_namemax
        return result

    # ── T22: normal storage ───────────────────────────────────────────────
    def test_normal_storage(self):
        mock = self._mock_statvfs()
        reader = StorageReader(statvfs=lambda _path: mock)
        result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
        self.assertEqual(result.status, STATUS_OK)
        total = 100000 * 4096
        free = 30000 * 4096
        avail = 25000 * 4096
        used = total - free
        self.assertEqual(result.total_bytes, total)
        self.assertEqual(result.used_bytes, used)
        self.assertEqual(result.available_bytes, avail)
        expected_pct = used / (used + avail) * 100.0
        self.assertAlmostEqual(result.percent, expected_pct, places=2)

    # ── T23: OSError / zero total ─────────────────────────────────────────
    def test_oserror(self):
        def failing(_path):
            raise OSError(2, "No such file")
        reader = StorageReader(statvfs=failing)
        result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
        self.assertIn(result.status, (STATUS_UNAVAILABLE, STATUS_ERROR))

    def test_zero_total(self):
        mock = self._mock_statvfs(f_blocks=0)
        reader = StorageReader(statvfs=lambda _path: mock)
        result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
        self.assertEqual(result.status, STATUS_ERROR)


# ╶── Snapshot model ───────────────────────────────────────────────────────


class SnapshotModelTests(unittest.TestCase):
    # ── T32: valid zero values ────────────────────────────────────────────
    def test_valid_zero_is_ok(self):
        utilization = CpuUtilization(
            status=STATUS_OK, percent=0.0,
            sampled_at=1.0, sampled_monotonic=100.0,
        )
        data = utilization.to_dict(100.0)
        self.assertEqual(data["percent"], 0.0)
        self.assertEqual(data["status"], STATUS_OK)
        self.assertFalse(data["stale"])

    # ── T33: missing values are null ──────────────────────────────────────
    def test_missing_values_null(self):
        utilization = CpuUtilization(status=STATUS_UNAVAILABLE)
        data = utilization.to_dict(100.0)
        self.assertIsNone(data["percent"])
        self.assertIsNone(data["sampled_at"])

    # ── T34: staleness thresholds ─────────────────────────────────────────
    def test_staleness_thresholds(self):
        utilization = CpuUtilization(
            status=STATUS_OK, percent=42.0,
            sampled_at=1.0, sampled_monotonic=100.0,
        )
        self.assertFalse(utilization.is_stale(102.0))  # 2 s < 3 s
        self.assertTrue(utilization.is_stale(104.0))   # 4 s > 3 s

        gpu = GpuMetrics(
            status=STATUS_OK, name="GPU", utilization_percent=10.0,
            temperature_c=50.0, memory_used_bytes=100, memory_total_bytes=1000,
            memory_percent=10.0, source="nvml",
            sampled_at=1.0, sampled_monotonic=100.0,
        )
        self.assertFalse(gpu.is_stale(105.0))  # 5 s < 6 s
        self.assertTrue(gpu.is_stale(107.0))    # 7 s > 6 s

    # ── T35: partial snapshot failure ─────────────────────────────────────
    def test_partial_failure_preserves_others(self):
        snapshot = TelemetrySnapshot(
            sequence=1,
            sampled_at=1.0,
            sampled_monotonic=100.0,
            cpu=CpuMetrics(
                utilization=CpuUtilization(
                    status=STATUS_OK, percent=10.0,
                    sampled_at=1.0, sampled_monotonic=100.0,
                ),
            ),
            memory=MemoryMetrics(
                status=STATUS_OK, total_bytes=16000000,
                used_bytes=8000000, available_bytes=8000000, percent=50.0,
                sampled_at=1.0, sampled_monotonic=100.0,
            ),
            gpu=GpuMetrics(
                status=STATUS_ERROR, error="driver_error",
            ),
        )
        data = snapshot.to_dict(100.0)
        self.assertEqual(data["cpu"]["utilization"]["status"], STATUS_OK)
        self.assertEqual(data["memory"]["status"], STATUS_OK)
        self.assertEqual(data["gpu"]["status"], STATUS_ERROR)
        self.assertEqual(data["service_state"], "ok")

    # ── T36: snapshot JSON size < 64 KiB ──────────────────────────────────
    def test_snapshot_json_size(self):
        temps = tuple(
            TemperatureReading(
                id=f"sensor_{i}",
                label=f"Sensor {i}" * 5,
                celsius=float(i),
                source=f"hwmon:{chr(65 + i)}/temp1_input",
                status=STATUS_OK,
                sampled_at=1.0,
                sampled_monotonic=100.0,
            )
            for i in range(20)
        )
        snapshot = TelemetrySnapshot(
            sequence=999999,
            sampled_at=9999999999.999,
            sampled_monotonic=100.0,
            cpu=CpuMetrics(
                model="A" * 128,
                logical_cores=256,
                utilization=CpuUtilization(
                    status=STATUS_OK, percent=99.99,
                    sampled_at=9999999999.999, sampled_monotonic=100.0,
                ),
                frequency=CpuFrequency(
                    status=STATUS_OK, mhz=9999.9, max_mhz=9999.9,
                    sampled_at=9999999999.999, sampled_monotonic=100.0,
                ),
            ),
            memory=MemoryMetrics(
                status=STATUS_OK,
                total_bytes=999999999999,
                used_bytes=999999999999,
                available_bytes=999999999999,
                percent=99.99,
                sampled_at=9999999999.999,
                sampled_monotonic=100.0,
            ),
            gpu=GpuMetrics(
                status=STATUS_OK,
                name="G" * 128,
                utilization_percent=99.99,
                temperature_c=149.9,
                memory_used_bytes=999999999999,
                memory_total_bytes=999999999999,
                memory_percent=99.99,
                source="nvidia-smi",
                sampled_at=9999999999.999,
                sampled_monotonic=100.0,
            ),
            temperatures=temps,
            battery=BatteryMetrics(
                status=STATUS_OK, present=True, percent=100,
                state="charging", ac_online=True,
                sampled_at=9999999999.999, sampled_monotonic=100.0,
            ),
            storage=StorageMetrics(
                status=STATUS_OK,
                total_bytes=999999999999999,
                used_bytes=999999999999999,
                available_bytes=999999999999999,
                percent=99.99,
                sampled_at=9999999999.999,
                sampled_monotonic=100.0,
            ),
        )
        encoded = json.dumps(snapshot.to_dict(100.0))
        self.assertLess(len(encoded.encode("utf-8")), 64 * 1024)

    # ── T37: schema allowlist — no sensitive metadata ─────────────────────
    def test_no_sensitive_metadata_in_snapshot(self):
        snapshot = TelemetrySnapshot(
            sequence=1,
            sampled_at=1.0,
            sampled_monotonic=100.0,
            cpu=CpuMetrics(model="Intel i7"),
        )
        encoded = json.dumps(snapshot.to_dict(100.0))
        import socket as _socket
        import getpass
        hostname = _socket.gethostname()
        username = getpass.getuser()
        self.assertNotIn(hostname, encoded)
        self.assertNotIn(username, encoded)
        # no pid, serial, uuid, mac
        self.assertNotIn("pid", encoded.lower().split("\"status\"")[0])
        for word in ("serial", "uuid", "mac_address", "ssid"):
            self.assertNotIn(word, encoded.lower())

    # ── Sequence monotonic ────────────────────────────────────────────────
    def test_sequence_monotonic(self):
        s1 = TelemetrySnapshot(sequence=1)
        s2 = TelemetrySnapshot(sequence=2)
        self.assertGreater(s2.sequence, s1.sequence)

    # ── service_state derivation ──────────────────────────────────────────
    def test_service_state_starting(self):
        snapshot = TelemetrySnapshot(sequence=0)
        self.assertEqual(snapshot.service_state_at(100.0), "starting")

    def test_service_state_ok(self):
        snapshot = TelemetrySnapshot(
            sequence=1,
            cpu=CpuMetrics(
                utilization=CpuUtilization(
                    status=STATUS_OK, percent=5.0,
                    sampled_at=1.0, sampled_monotonic=99.0,
                ),
            ),
            memory=MemoryMetrics(
                status=STATUS_OK, total_bytes=16000,
                sampled_at=1.0, sampled_monotonic=99.0,
            ),
        )
        self.assertEqual(snapshot.service_state_at(100.0), "ok")

    def test_service_state_degraded(self):
        snapshot = TelemetrySnapshot(
            sequence=1,
            cpu=CpuMetrics(
                utilization=CpuUtilization(
                    status=STATUS_OK, percent=5.0,
                    sampled_at=1.0, sampled_monotonic=95.0,
                ),
            ),
            memory=MemoryMetrics(
                status=STATUS_UNAVAILABLE,
                sampled_at=1.0, sampled_monotonic=95.0,
            ),
        )
        self.assertEqual(snapshot.service_state_at(100.0), "degraded")

    # ── CpuInfo ───────────────────────────────────────────────────────────
    def test_cpu_info_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths_in(root)
            _write(paths.proc_cpuinfo, (
                "processor\t: 0\n"
                "model name\t: 13th Gen Intel(R) Core(TM) i7-13700HX\n"
                "processor\t: 1\n"
                "model name\t: 13th Gen Intel(R) Core(TM) i7-13700HX\n"
            ))
            info = read_cpu_info(paths)
            self.assertIn("i7-13700HX", info.model)
            self.assertIsNotNone(info.logical_cores)


# ╶── GPU ──────────────────────────────────────────────────────────────────

from kivo.telemetry.gpu import GpuReader
from kivo.core.bounded_process import BoundedProcessResult


class GpuReaderTests(unittest.TestCase):
    def _fake_nvml(self, *, count=1, name="RTX 4050", temp=44, util=6,
                   mem_used=314966016, mem_total=6439305216, fail=False):
        module = MagicMock()
        module.NVML_TEMPERATURE_GPU = 0
        if fail:
            module.nvmlInit.side_effect = Exception("NVML init failed")
            return module
        module.nvmlDeviceGetCount.return_value = count
        handle = MagicMock()
        module.nvmlDeviceGetHandleByIndex.return_value = handle
        raw_name = name.encode("utf-8") if isinstance(name, str) else name
        module.nvmlDeviceGetName.return_value = raw_name
        module.nvmlDeviceGetTemperature.return_value = temp
        rates = MagicMock()
        rates.gpu = util
        module.nvmlDeviceGetUtilizationRates.return_value = rates
        mem = MagicMock()
        mem.used = mem_used
        mem.total = mem_total
        module.nvmlDeviceGetMemoryInfo.return_value = mem
        return module

    # ── T24: NVML success ─────────────────────────────────────────────────
    def test_nvml_success(self):
        nvml = self._fake_nvml()
        reader = GpuReader(nvml_module=nvml)
        result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.source, "nvml")
        self.assertIn("RTX 4050", result.name)
        self.assertAlmostEqual(result.temperature_c, 44.0)
        self.assertAlmostEqual(result.utilization_percent, 6.0)
        self.assertEqual(result.memory_used_bytes, 314966016)
        self.assertEqual(result.memory_total_bytes, 6439305216)
        reader.close()

    # ── T25: NVML zero devices ────────────────────────────────────────────
    def test_nvml_zero_devices(self):
        nvml = self._fake_nvml(count=0)
        reader = GpuReader(nvml_module=nvml)
        result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
        self.assertEqual(result.status, STATUS_UNSUPPORTED)
        reader.close()

    # ── T26: NVML driver not loaded ───────────────────────────────────────
    def test_nvml_driver_not_loaded(self):
        nvml = self._fake_nvml(fail=True)
        reader = GpuReader(nvml_module=nvml, nvidia_smi_path=None)
        result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
        self.assertIn(result.status, (STATUS_ERROR, STATUS_UNAVAILABLE, STATUS_UNSUPPORTED))
        reader.close()

    # ── T27: NVML transient error ─────────────────────────────────────────
    def test_nvml_transient_error(self):
        nvml = self._fake_nvml()
        reader = GpuReader(nvml_module=nvml)
        # First sample succeeds
        result1 = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
        self.assertEqual(result1.status, STATUS_OK)
        # Break the query
        nvml.nvmlDeviceGetTemperature.side_effect = Exception("transient")
        result2 = reader.sample(sampled_at=2.0, sampled_monotonic=101.0)
        self.assertEqual(result2.status, STATUS_ERROR)
        # Fix it
        nvml.nvmlDeviceGetTemperature.side_effect = None
        nvml.nvmlDeviceGetTemperature.return_value = 45
        result3 = reader.sample(sampled_at=3.0, sampled_monotonic=102.0)
        self.assertEqual(result3.status, STATUS_OK)
        reader.close()

    # ── T28: nvidia-smi success ───────────────────────────────────────────
    def test_nvidia_smi_success(self):
        def fake_runner(argv, *, timeout, stdout_limit, stderr_limit, env=None):
            return BoundedProcessResult(
                returncode=0,
                stdout="NVIDIA GeForce RTX 4050, 44, 6, 300, 6144\n",
                stderr="",
            )
        reader = GpuReader(enable_nvml=False, nvidia_smi_path="/usr/bin/nvidia-smi", runner=fake_runner)
        result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.source, "nvidia-smi")
        self.assertEqual(result.memory_used_bytes, 300 * 1024 * 1024)
        self.assertEqual(result.memory_total_bytes, 6144 * 1024 * 1024)
        reader.close()

    # ── T29: nvidia-smi missing / timeout / nonzero ───────────────────────
    def test_nvidia_smi_missing(self):
        reader = GpuReader(enable_nvml=False, nvidia_smi_path=None)
        result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
        self.assertEqual(result.status, STATUS_UNSUPPORTED)
        reader.close()

    def test_nvidia_smi_timeout(self):
        def fake_runner(argv, **kw):
            return BoundedProcessResult(returncode=1, stdout="", stderr="", timed_out=True)
        reader = GpuReader(enable_nvml=False, nvidia_smi_path="/usr/bin/nvidia-smi", runner=fake_runner)
        result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
        self.assertEqual(result.status, STATUS_ERROR)
        self.assertEqual(result.error, "timeout")
        reader.close()

    def test_nvidia_smi_nonzero_exit(self):
        def fake_runner(argv, **kw):
            return BoundedProcessResult(returncode=1, stdout="", stderr="error")
        reader = GpuReader(enable_nvml=False, nvidia_smi_path="/usr/bin/nvidia-smi", runner=fake_runner)
        result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
        self.assertEqual(result.status, STATUS_ERROR)
        reader.close()

    # ── T30: malformed / wrong fields / oversize ──────────────────────────
    def test_nvidia_smi_malformed_csv(self):
        def fake_runner(argv, **kw):
            return BoundedProcessResult(returncode=0, stdout="only,three,fields\n", stderr="")
        reader = GpuReader(enable_nvml=False, nvidia_smi_path="/usr/bin/nvidia-smi", runner=fake_runner)
        result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
        self.assertEqual(result.status, STATUS_ERROR)
        reader.close()

    def test_nvidia_smi_non_numeric(self):
        def fake_runner(argv, **kw):
            return BoundedProcessResult(returncode=0, stdout="GPU, abc, 6, 300, 6144\n", stderr="")
        reader = GpuReader(enable_nvml=False, nvidia_smi_path="/usr/bin/nvidia-smi", runner=fake_runner)
        result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
        self.assertEqual(result.status, STATUS_ERROR)
        reader.close()

    def test_nvidia_smi_oversize_output(self):
        def fake_runner(argv, **kw):
            return BoundedProcessResult(
                returncode=0, stdout="x" * 100000, stderr="",
                stdout_truncated=True,
            )
        reader = GpuReader(enable_nvml=False, nvidia_smi_path="/usr/bin/nvidia-smi", runner=fake_runner)
        result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
        self.assertEqual(result.status, STATUS_ERROR)
        reader.close()

    # ── T31: GPU name with comma ──────────────────────────────────────────
    def test_gpu_name_with_comma(self):
        def fake_runner(argv, **kw):
            return BoundedProcessResult(
                returncode=0,
                stdout="NVIDIA, GeForce, RTX 4050, 44, 6, 300, 6144\n",
                stderr="",
            )
        reader = GpuReader(enable_nvml=False, nvidia_smi_path="/usr/bin/nvidia-smi", runner=fake_runner)
        result = reader.sample(sampled_at=1.0, sampled_monotonic=100.0)
        self.assertEqual(result.status, STATUS_OK)
        self.assertIn("RTX 4050", result.name)
        self.assertAlmostEqual(result.temperature_c, 44.0)
        reader.close()


# ╶── Security ─────────────────────────────────────────────────────────────


class SecurityTests(unittest.TestCase):
    # ── No shell in bounded_process ───────────────────────────────────────
    def test_bounded_process_no_shell_string(self):
        from kivo.core.bounded_process import run_bounded
        with self.assertRaises(ValueError):
            run_bounded("echo hello", timeout=1.0)

    def test_bounded_process_rejects_empty_argv(self):
        from kivo.core.bounded_process import run_bounded
        with self.assertRaises(ValueError):
            run_bounded([], timeout=1.0)

    def test_bounded_process_rejects_nul_byte(self):
        from kivo.core.bounded_process import run_bounded
        with self.assertRaises(ValueError):
            run_bounded(["/bin/echo", "a\x00b"], timeout=1.0)

    # ── No process enumeration ────────────────────────────────────────────
    def test_no_proc_pid_access(self):
        """Readers never access /proc/<pid>."""
        import inspect
        from kivo.telemetry import readers
        source = inspect.getsource(readers)
        import re
        # Must not contain /proc/ followed by digits (pid)
        self.assertFalse(
            re.search(r'/proc/\d', source),
            "readers.py must not access /proc/<pid>",
        )

    # ── No sensitive fields ───────────────────────────────────────────────
    def test_snapshot_no_sensitive_keys(self):
        snapshot = TelemetrySnapshot(
            sequence=1, sampled_at=1.0, sampled_monotonic=100.0,
        )
        data = json.dumps(snapshot.to_dict(100.0))
        forbidden = [
            "hostname", "username", "serial", "uuid",
            "mac_address", "ssid", "command_line", "pid",
        ]
        for word in forbidden:
            self.assertNotIn(
                f'"{word}"', data.lower(),
                f"snapshot must not contain '{word}' as a key",
            )


if __name__ == "__main__":
    unittest.main()
