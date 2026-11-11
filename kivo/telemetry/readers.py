"""Read-only Linux metric readers for KIVO telemetry.

Every reader takes explicit paths so tests can use a fake filesystem tree.
No reader writes to procfs, sysfs, or a device, and no reader inspects another
process.  Sensor names and labels are treated as untrusted display strings and
are sanitized and bounded before they enter a snapshot.
"""
from __future__ import annotations

import errno
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from kivo.telemetry.models import (
    ERROR_INVALID_VALUE,
    ERROR_NOT_AVAILABLE,
    ERROR_NOT_SUPPORTED,
    ERROR_NO_SENSOR,
    ERROR_PARSE_FAILED,
    ERROR_READ_FAILED,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    STATUS_UNSUPPORTED,
    STATUS_WARMING_UP,
    BatteryMetrics,
    CpuFrequency,
    CpuUtilization,
    MemoryMetrics,
    StorageMetrics,
    TemperatureReading,
)

_PROC_READ_LIMIT = 256 * 1024
_SYSFS_READ_LIMIT = 8192
_LABEL_LIMIT = 64
_MODEL_LIMIT = 128

_BATTERY_STATES = {
    "charging": "charging",
    "discharging": "discharging",
    "full": "full",
    "not charging": "not_charging",
}
_CPU_THERMAL_PRIORITY = ("x86_pkg_temp", "tcpu", "tcpu_pci", "acpitz")


@dataclass(frozen=True)
class TelemetryPaths:
    """Filesystem roots used by telemetry readers."""

    proc_stat: Path = Path("/proc/stat")
    proc_meminfo: Path = Path("/proc/meminfo")
    proc_cpuinfo: Path = Path("/proc/cpuinfo")
    sys_cpu: Path = Path("/sys/devices/system/cpu")
    hwmon: Path = Path("/sys/class/hwmon")
    thermal: Path = Path("/sys/class/thermal")
    power_supply: Path = Path("/sys/class/power_supply")
    root_mount: str = "/"


@dataclass(frozen=True)
class CpuInfo:
    model: str | None
    logical_cores: int | None


@dataclass(frozen=True)
class SensorTarget:
    id: str
    label: str
    path: Path
    source: str


def _read_text(path: Path, limit: int) -> str:
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file too large")
    return data.decode("utf-8", errors="replace")


def _sanitize_text(value: str, limit: int) -> str:
    printable = "".join(
        character if character.isprintable() else " " for character in value
    )
    return re.sub(r"\s+", " ", printable).strip()[:limit]


def _sanitize_identifier(value: str, limit: int = _LABEL_LIMIT) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "", value)
    return cleaned[:limit]


def _parse_int(value: str) -> int:
    return int(value.strip(), 10)


def _parse_float(value: str) -> float:
    return float(value.strip())


def _numeric_name_key(path: Path) -> tuple[str, int]:
    match = re.search(r"(\d+)$", path.name)
    return path.name.rstrip("0123456789"), int(match.group(1)) if match else -1


def read_cpu_info(paths: TelemetryPaths) -> CpuInfo:
    """Read the CPU model once; never enumerate per-core sensitive details."""
    model: str | None = None
    processors = 0
    try:
        text = _read_text(paths.proc_cpuinfo, 1024 * 1024)
    except (OSError, ValueError):
        text = ""
    for line in text.splitlines():
        if line.startswith("processor") and ":" in line:
            processors += 1
        elif line.startswith("model name") and ":" in line and model is None:
            model = _sanitize_text(line.split(":", 1)[1], _MODEL_LIMIT) or None
    logical = os.cpu_count() or processors or None
    return CpuInfo(model=model, logical_cores=logical)


class CpuReader:
    """Aggregate CPU utilization from successive ``/proc/stat`` samples."""

    def __init__(self, paths: TelemetryPaths | None = None):
        self._paths = paths or TelemetryPaths()
        self._previous_total: int | None = None
        self._previous_idle: int | None = None
        self._last_sampled_at: float | None = None
        self._last_sampled_monotonic: float | None = None

    def reset(self) -> None:
        self._previous_total = None
        self._previous_idle = None

    def sample(
        self, *, sampled_at: float, sampled_monotonic: float
    ) -> CpuUtilization:
        try:
            text = _read_text(self._paths.proc_stat, _PROC_READ_LIMIT)
        except (FileNotFoundError, PermissionError, OSError, ValueError):
            return CpuUtilization(
                status=STATUS_UNAVAILABLE,
                sampled_at=self._last_sampled_at,
                sampled_monotonic=self._last_sampled_monotonic,
                error=ERROR_READ_FAILED,
            )

        counters = self._parse_counters(text)
        if counters is None:
            return CpuUtilization(
                status=STATUS_ERROR,
                sampled_at=self._last_sampled_at,
                sampled_monotonic=self._last_sampled_monotonic,
                error=ERROR_PARSE_FAILED,
            )

        total, idle = counters
        if self._previous_total is None or self._previous_idle is None:
            self._previous_total = total
            self._previous_idle = idle
            self._remember(sampled_at, sampled_monotonic)
            return CpuUtilization(
                status=STATUS_WARMING_UP,
                sampled_at=sampled_at,
                sampled_monotonic=sampled_monotonic,
                error=None,
            )

        if total < self._previous_total or idle < self._previous_idle:
            self._previous_total = total
            self._previous_idle = idle
            self._remember(sampled_at, sampled_monotonic)
            return CpuUtilization(
                status=STATUS_WARMING_UP,
                sampled_at=sampled_at,
                sampled_monotonic=sampled_monotonic,
                error="counter_reset",
            )

        delta_total = total - self._previous_total
        delta_idle = idle - self._previous_idle
        self._previous_total = total
        self._previous_idle = idle
        if delta_total <= 0:
            self._remember(sampled_at, sampled_monotonic)
            return CpuUtilization(
                status=STATUS_WARMING_UP,
                sampled_at=sampled_at,
                sampled_monotonic=sampled_monotonic,
                error=None,
            )

        delta_idle = min(delta_idle, delta_total)
        percent = 100.0 * (delta_total - delta_idle) / delta_total
        percent = min(100.0, max(0.0, percent))
        self._remember(sampled_at, sampled_monotonic)
        return CpuUtilization(
            status=STATUS_OK,
            percent=percent,
            sampled_at=sampled_at,
            sampled_monotonic=sampled_monotonic,
        )

    def _remember(self, sampled_at: float, sampled_monotonic: float) -> None:
        self._last_sampled_at = sampled_at
        self._last_sampled_monotonic = sampled_monotonic

    @staticmethod
    def _parse_counters(text: str) -> tuple[int, int] | None:
        for line in text.splitlines():
            if not line.startswith("cpu "):
                continue
            fields = line.split()
            if len(fields) < 5:
                return None
            try:
                values = [_parse_int(field) for field in fields[1:9]]
            except (TypeError, ValueError):
                return None
            if len(values) < 4 or any(value < 0 for value in values):
                return None
            # Fields 0-7 only: guest and guest_nice are already included in
            # user and nice, so adding them would double-count.
            total = sum(values)
            idle = values[3] + values[4] if len(values) > 4 else values[3]
            return total, idle
        return None


class MemoryReader:
    """Memory totals from ``/proc/meminfo`` using MemAvailable."""

    def __init__(self, paths: TelemetryPaths | None = None):
        self._paths = paths or TelemetryPaths()
        self._last_sampled_at: float | None = None
        self._last_sampled_monotonic: float | None = None

    def sample(
        self, *, sampled_at: float, sampled_monotonic: float
    ) -> MemoryMetrics:
        try:
            text = _read_text(self._paths.proc_meminfo, _PROC_READ_LIMIT)
        except (FileNotFoundError, PermissionError, OSError, ValueError):
            return MemoryMetrics(
                status=STATUS_UNAVAILABLE,
                sampled_at=self._last_sampled_at,
                sampled_monotonic=self._last_sampled_monotonic,
                error=ERROR_READ_FAILED,
            )

        fields: dict[str, int] = {}
        try:
            for line in text.splitlines():
                if ":" not in line:
                    continue
                key, raw = line.split(":", 1)
                parts = raw.split()
                if not parts:
                    continue
                fields[key.strip()] = _parse_int(parts[0])
        except (TypeError, ValueError):
            return self._error(sampled_at, sampled_monotonic, ERROR_PARSE_FAILED)

        if "MemTotal" not in fields:
            return self._error(sampled_at, sampled_monotonic, ERROR_PARSE_FAILED)

        total = fields["MemTotal"] * 1024
        available: int | None = None
        if "MemAvailable" in fields:
            available = fields["MemAvailable"] * 1024
        else:
            fallback_keys = ("MemFree", "Buffers", "Cached")
            if all(key in fields for key in fallback_keys):
                available = sum(fields[key] for key in fallback_keys) * 1024

        if available is None:
            return self._error(sampled_at, sampled_monotonic, ERROR_NOT_AVAILABLE)
        if total <= 0 or available < 0:
            return self._error(sampled_at, sampled_monotonic, ERROR_INVALID_VALUE)

        used = max(0, total - available)
        percent = min(100.0, max(0.0, used / total * 100.0))
        self._last_sampled_at = sampled_at
        self._last_sampled_monotonic = sampled_monotonic
        return MemoryMetrics(
            status=STATUS_OK,
            total_bytes=total,
            used_bytes=used,
            available_bytes=available,
            percent=percent,
            sampled_at=sampled_at,
            sampled_monotonic=sampled_monotonic,
        )

    def _error(
        self, sampled_at: float, sampled_monotonic: float, error: str
    ) -> MemoryMetrics:
        return MemoryMetrics(
            status=STATUS_ERROR,
            sampled_at=self._last_sampled_at,
            sampled_monotonic=self._last_sampled_monotonic,
            error=error,
        )


class FrequencyReader:
    """Average the cached per-CPU ``scaling_cur_freq`` values."""

    def __init__(self, paths: TelemetryPaths | None = None):
        self._paths = paths or TelemetryPaths()
        self._frequency_paths: list[Path] = []
        self._max_khz: int | None = None
        self._discovered = False
        self._last_sampled_at: float | None = None
        self._last_sampled_monotonic: float | None = None

    def discover(self) -> None:
        self._discovered = True
        frequency_paths: list[Path] = []
        max_khz: int | None = None
        try:
            cpu_dirs = sorted(
                self._paths.sys_cpu.glob("cpu[0-9]*"), key=_numeric_name_key
            )[:256]
        except OSError:
            cpu_dirs = []

        for cpu_dir in cpu_dirs:
            cpufreq = cpu_dir / "cpufreq"
            current = cpufreq / "scaling_cur_freq"
            if current.is_file():
                frequency_paths.append(current)
            maximum = cpufreq / "cpuinfo_max_freq"
            if maximum.is_file():
                try:
                    value = _parse_int(_read_text(maximum, _SYSFS_READ_LIMIT))
                except (OSError, ValueError):
                    continue
                if value > 0 and (max_khz is None or value > max_khz):
                    max_khz = value

        self._frequency_paths = frequency_paths
        self._max_khz = max_khz

    def sample(
        self, *, sampled_at: float, sampled_monotonic: float
    ) -> CpuFrequency:
        if not self._discovered:
            self.discover()

        values: list[int] = []
        failures = 0
        for path in self._frequency_paths:
            try:
                value = _parse_int(_read_text(path, _SYSFS_READ_LIMIT))
            except (OSError, ValueError):
                failures += 1
                continue
            if value <= 0:
                failures += 1
                continue
            values.append(value)

        max_mhz = (
            round(self._max_khz / 1000.0, 1)
            if self._max_khz is not None
            else None
        )
        if not values:
            if self._frequency_paths and failures:
                status = STATUS_ERROR
                error = ERROR_PARSE_FAILED
            else:
                status = STATUS_UNAVAILABLE
                error = ERROR_NOT_AVAILABLE
            return CpuFrequency(
                status=status,
                max_mhz=max_mhz,
                sampled_at=self._last_sampled_at,
                sampled_monotonic=self._last_sampled_monotonic,
                error=error,
            )

        self._last_sampled_at = sampled_at
        self._last_sampled_monotonic = sampled_monotonic
        average_khz = sum(values) / len(values)
        return CpuFrequency(
            status=STATUS_OK,
            mhz=average_khz / 1000.0,
            max_mhz=max_mhz,
            sampled_at=sampled_at,
            sampled_monotonic=sampled_monotonic,
        )


class TemperatureReader:
    """Discover and read the small fixed sensor set in the v1 contract."""

    def __init__(self, paths: TelemetryPaths | None = None):
        self._paths = paths or TelemetryPaths()
        self._targets: list[SensorTarget] = []
        self._discovered = False
        self._needs_rediscovery = False
        self._last_success: dict[str, tuple[float, float]] = {}

    def discover(self) -> None:
        self._discovered = True
        self._needs_rediscovery = False
        hwmon = self._discover_hwmon()
        thermal = self._discover_thermal()
        targets: list[SensorTarget] = []

        cpu_hw = [entry for entry in hwmon if entry[0].lower() == "coretemp"]
        cpu_target = self._prefer_label(cpu_hw, "package id 0")
        if cpu_target is not None:
            targets.append(cpu_target)
        else:
            thermal_target = self._thermal_by_type(thermal, _CPU_THERMAL_PRIORITY)
            if thermal_target is not None:
                targets.append(thermal_target)

        nvme = [entry for entry in hwmon if entry[0].lower() == "nvme"]
        nvme_target = self._prefer_label(nvme, "composite")
        if nvme_target is not None:
            targets.append(nvme_target)

        wifi = [
            entry
            for entry in hwmon
            if entry[0].lower() in ("iwlwifi_1", "iwlwifi")
        ]
        wifi_target: SensorTarget | None = None
        if wifi:
            _chip, _label, path, source = wifi[0]
            wifi_target = SensorTarget(
                id="wifi", label="Wi-Fi", path=path, source=source
            )
        if wifi_target is None:
            wifi_target = self._thermal_by_type(thermal, ("iwlwifi_1",))
        if wifi_target is not None:
            targets.append(wifi_target)

        self._targets = targets

    def sample(
        self, *, sampled_at: float, sampled_monotonic: float
    ) -> tuple[TemperatureReading, ...]:
        if not self._discovered or self._needs_rediscovery:
            self.discover()

        readings, disappeared = self._read_targets(
            sampled_at, sampled_monotonic
        )
        if disappeared:
            self.discover()
            readings, _ = self._read_targets(sampled_at, sampled_monotonic)
        return readings

    def _read_targets(
        self, sampled_at: float, sampled_monotonic: float
    ) -> tuple[tuple[TemperatureReading, ...], bool]:
        readings: list[TemperatureReading] = []
        disappeared = False
        by_id = {target.id: target for target in self._targets}

        cpu = by_id.get("cpu_package")
        if cpu is None:
            readings.append(
                TemperatureReading(
                    id="cpu_package",
                    label="CPU Package",
                    status=STATUS_UNSUPPORTED,
                    error=ERROR_NO_SENSOR,
                )
            )
        else:
            reading, missing = self._read_target(
                cpu, sampled_at, sampled_monotonic
            )
            readings.append(reading)
            disappeared = disappeared or missing

        for sensor_id, label in (
            ("nvme_composite", "NVMe"),
            ("wifi", "Wi-Fi"),
        ):
            target = by_id.get(sensor_id)
            if target is None:
                continue
            reading, missing = self._read_target(
                target, sampled_at, sampled_monotonic
            )
            readings.append(reading)
            disappeared = disappeared or missing

        return tuple(readings), disappeared

    def _read_target(
        self,
        target: SensorTarget,
        sampled_at: float,
        sampled_monotonic: float,
    ) -> tuple[TemperatureReading, bool]:
        try:
            raw = _read_text(target.path, _SYSFS_READ_LIMIT)
            millidegrees = _parse_float(raw)
        except FileNotFoundError:
            self._needs_rediscovery = True
            return (
                TemperatureReading(
                    id=target.id,
                    label=target.label,
                    source=target.source,
                    status=STATUS_UNAVAILABLE,
                    sampled_at=self._last_timestamp(target.id, 0),
                    sampled_monotonic=self._last_timestamp(target.id, 1),
                    error=ERROR_READ_FAILED,
                ),
                True,
            )
        except (OSError, ValueError):
            return (
                TemperatureReading(
                    id=target.id,
                    label=target.label,
                    source=target.source,
                    status=STATUS_ERROR,
                    sampled_at=self._last_timestamp(target.id, 0),
                    sampled_monotonic=self._last_timestamp(target.id, 1),
                    error=ERROR_PARSE_FAILED,
                ),
                False,
            )

        celsius = millidegrees / 1000.0
        if celsius < -40.0 or celsius > 150.0:
            return (
                TemperatureReading(
                    id=target.id,
                    label=target.label,
                    source=target.source,
                    status=STATUS_ERROR,
                    sampled_at=self._last_timestamp(target.id, 0),
                    sampled_monotonic=self._last_timestamp(target.id, 1),
                    error=ERROR_INVALID_VALUE,
                ),
                False,
            )

        self._last_success[target.id] = (sampled_at, sampled_monotonic)
        return (
            TemperatureReading(
                id=target.id,
                label=target.label,
                celsius=celsius,
                source=target.source,
                status=STATUS_OK,
                sampled_at=sampled_at,
                sampled_monotonic=sampled_monotonic,
            ),
            False,
        )

    def _last_timestamp(self, sensor_id: str, index: int) -> float | None:
        remembered = self._last_success.get(sensor_id)
        return None if remembered is None else remembered[index]

    def _discover_hwmon(self) -> list[tuple[str, str, Path, str]]:
        entries: list[tuple[str, str, Path, str]] = []
        total_inputs = 0
        try:
            hwmon_dirs = sorted(
                self._paths.hwmon.glob("hwmon*"), key=_numeric_name_key
            )[:64]
        except OSError:
            return entries

        for hwmon_dir in hwmon_dirs:
            try:
                chip = _sanitize_identifier(
                    _read_text(hwmon_dir / "name", _SYSFS_READ_LIMIT)
                )
            except (OSError, ValueError):
                continue
            if not chip:
                continue
            try:
                inputs = sorted(
                    hwmon_dir.glob("temp*_input"), key=_numeric_name_key
                )
            except OSError:
                continue
            for input_path in inputs:
                if total_inputs >= 128:
                    break
                total_inputs += 1
                label_path = input_path.with_name(
                    input_path.name.replace("_input", "_label")
                )
                try:
                    label = _sanitize_text(
                        _read_text(label_path, _SYSFS_READ_LIMIT), _LABEL_LIMIT
                    )
                except (OSError, ValueError):
                    label = ""
                source = f"hwmon:{chip}/{input_path.name}"
                entries.append((chip, label, input_path, source))
        return entries

    def _discover_thermal(self) -> list[tuple[str, str, Path, str]]:
        entries: list[tuple[str, str, Path, str]] = []
        try:
            zones = sorted(
                self._paths.thermal.glob("thermal_zone*"), key=_numeric_name_key
            )[:64]
        except OSError:
            return entries
        for zone in zones:
            try:
                zone_type = _sanitize_identifier(
                    _read_text(zone / "type", _SYSFS_READ_LIMIT)
                )
            except (OSError, ValueError):
                continue
            temp_path = zone / "temp"
            if not zone_type or not temp_path.is_file():
                continue
            entries.append(
                (
                    zone_type,
                    "",
                    temp_path,
                    f"thermal:{zone_type}",
                )
            )
        return entries

    @staticmethod
    def _prefer_label(
        candidates: list[tuple[str, str, Path, str]], wanted: str
    ) -> SensorTarget | None:
        if not candidates:
            return None
        chosen = candidates[0]
        for candidate in candidates:
            if candidate[1].strip().lower() == wanted:
                chosen = candidate
                break
        chip, label, path, source = chosen
        sensor_id = {
            "coretemp": "cpu_package",
            "nvme": "nvme_composite",
        }.get(chip.lower(), "sensor")
        display_label = {
            "cpu_package": "CPU Package",
            "nvme_composite": "NVMe",
        }.get(sensor_id, label or chip)
        return SensorTarget(
            id=sensor_id,
            label=display_label,
            path=path,
            source=source,
        )

    @staticmethod
    def _thermal_by_type(
        candidates: list[tuple[str, str, Path, str]],
        wanted_types: tuple[str, ...],
    ) -> SensorTarget | None:
        by_type = {entry[0].lower(): entry for entry in candidates}
        for wanted in wanted_types:
            entry = by_type.get(wanted)
            if entry is None:
                continue
            zone_type, _unused, path, source = entry
            if wanted == "iwlwifi_1":
                sensor_id, label = "wifi", "Wi-Fi"
            else:
                sensor_id, label = "cpu_package", "CPU Package"
            return SensorTarget(
                id=sensor_id,
                label=label,
                path=path,
                source=source,
            )
        return None


class BatteryReader:
    """Battery and AC state from ``/sys/class/power_supply``."""

    def __init__(self, paths: TelemetryPaths | None = None):
        self._paths = paths or TelemetryPaths()
        self._last_sampled_at: float | None = None
        self._last_sampled_monotonic: float | None = None

    def sample(
        self, *, sampled_at: float, sampled_monotonic: float
    ) -> BatteryMetrics:
        entries = self._entries()
        battery_dir: Path | None = None
        ac_online: bool | None = None

        for entry in entries:
            supply_type = self._read(entry / "type").lower()
            if supply_type == "battery" and battery_dir is None:
                present = self._read(entry / "present")
                if present == "":
                    battery_dir = entry
                else:
                    try:
                        if _parse_int(present) != 0:
                            battery_dir = entry
                    except ValueError:
                        battery_dir = entry
            elif supply_type == "mains" and ac_online is None:
                online = self._read(entry / "online")
                if online == "1":
                    ac_online = True
                elif online == "0":
                    ac_online = False

        if battery_dir is None:
            return BatteryMetrics(
                status=STATUS_UNSUPPORTED,
                present=False,
                ac_online=ac_online,
                error=ERROR_NOT_SUPPORTED,
            )

        percent = self._percent(battery_dir)
        if percent is None:
            return BatteryMetrics(
                status=STATUS_ERROR,
                present=True,
                state=self._state(battery_dir),
                ac_online=ac_online,
                sampled_at=self._last_sampled_at,
                sampled_monotonic=self._last_sampled_monotonic,
                error=ERROR_PARSE_FAILED,
            )

        self._last_sampled_at = sampled_at
        self._last_sampled_monotonic = sampled_monotonic
        return BatteryMetrics(
            status=STATUS_OK,
            present=True,
            percent=max(0, min(100, percent)),
            state=self._state(battery_dir),
            ac_online=ac_online,
            sampled_at=sampled_at,
            sampled_monotonic=sampled_monotonic,
        )

    def _entries(self) -> list[Path]:
        try:
            return sorted(
                self._paths.power_supply.glob("*"), key=_numeric_name_key
            )[:32]
        except OSError:
            return []

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return _read_text(path, _SYSFS_READ_LIMIT).strip()
        except (OSError, ValueError):
            return ""

    def _percent(self, battery_dir: Path) -> int | None:
        capacity = self._read(battery_dir / "capacity")
        if capacity:
            try:
                value = _parse_int(capacity)
            except ValueError:
                value = -1
            if 0 <= value <= 100:
                return value

        for prefix in ("charge", "energy"):
            now = self._read(battery_dir / f"{prefix}_now")
            full = self._read(battery_dir / f"{prefix}_full")
            if not now or not full:
                continue
            try:
                now_value = _parse_int(now)
                full_value = _parse_int(full)
            except ValueError:
                continue
            if full_value > 0 and now_value >= 0:
                return round(now_value / full_value * 100)
        return None

    def _state(self, battery_dir: Path) -> str:
        raw = self._read(battery_dir / "status").lower()
        return _BATTERY_STATES.get(raw, "unknown")


class StorageReader:
    """Filesystem usage for the fixed root mount."""

    def __init__(
        self,
        paths: TelemetryPaths | None = None,
        statvfs: Callable[[str], os.statvfs_result] = os.statvfs,
    ):
        self._paths = paths or TelemetryPaths()
        self._statvfs = statvfs
        self._last_sampled_at: float | None = None
        self._last_sampled_monotonic: float | None = None

    def sample(
        self, *, sampled_at: float, sampled_monotonic: float
    ) -> StorageMetrics:
        try:
            stats = self._statvfs(self._paths.root_mount)
        except OSError as exc:
            status = (
                STATUS_UNAVAILABLE
                if exc.errno in (errno.ENOENT, errno.EACCES)
                else STATUS_ERROR
            )
            return StorageMetrics(
                mount=self._paths.root_mount,
                status=status,
                sampled_at=self._last_sampled_at,
                sampled_monotonic=self._last_sampled_monotonic,
                error=ERROR_READ_FAILED,
            )

        fragment = stats.f_frsize or stats.f_bsize
        total = int(stats.f_blocks * fragment)
        free = int(stats.f_bfree * fragment)
        available = int(stats.f_bavail * fragment)
        used = max(0, total - free)
        if total <= 0:
            return StorageMetrics(
                mount=self._paths.root_mount,
                status=STATUS_ERROR,
                sampled_at=self._last_sampled_at,
                sampled_monotonic=self._last_sampled_monotonic,
                error=ERROR_INVALID_VALUE,
            )

        denominator = used + available
        percent = (
            min(100.0, max(0.0, used / denominator * 100.0))
            if denominator > 0
            else None
        )
        self._last_sampled_at = sampled_at
        self._last_sampled_monotonic = sampled_monotonic
        return StorageMetrics(
            mount=self._paths.root_mount,
            status=STATUS_OK,
            total_bytes=total,
            used_bytes=used,
            available_bytes=available,
            percent=percent,
            sampled_at=sampled_at,
            sampled_monotonic=sampled_monotonic,
        )


__all__ = [
    "TelemetryPaths",
    "CpuInfo",
    "SensorTarget",
    "read_cpu_info",
    "CpuReader",
    "MemoryReader",
    "FrequencyReader",
    "TemperatureReader",
    "BatteryReader",
    "StorageReader",
]
