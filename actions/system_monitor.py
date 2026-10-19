"""
System Monitor — background metric checks with voice alert support.
Provides real-time telemetry for CPU, GPU (NVIDIA / AMD / Intel),
laptop chassis/motherboard temperatures, fan speeds, battery, and RAM.
"""
from __future__ import annotations

import ctypes
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

DEFAULT_THRESHOLDS = {
    "cpu":  90.0,
    "ram":  90.0,
    "temp": 85.0,
    "gpu":  95.0,
}

_COOLDOWN   = 300
_CPU_STREAK = 3

# ── NVML DLL / library cache ──────────────────────────────────────────────────
_nvml_lib: object = None
_nvml_ok: object = None   # None=untested, True=works, False=unavailable


def _get_gpu_info() -> dict[str, Any]:
    """
    Returns GPU hardware metrics (name, temp_c, util_percent, memory)
    using pynvml / nvidia-ml-py or nvidia-smi fallback.
    """
    # 1. Try nvidia_ml_py / pynvml
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import pynvml  # type: ignore
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        raw_name = pynvml.nvmlDeviceGetName(h)
        name = raw_name.decode("utf-8") if isinstance(raw_name, bytes) else str(raw_name)
        temp = float(pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU))
        u = pynvml.nvmlDeviceGetUtilizationRates(h)
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        return {
            "name": name,
            "temp_c": round(temp, 1),
            "util_percent": float(u.gpu),
            "mem_used_mb": round(mem.used / (1024 ** 2), 1),
            "mem_total_mb": round(mem.total / (1024 ** 2), 1),
        }
    except Exception:
        pass

    # 2. Try nvidia-smi CLI
    if _OS != "Windows" or os.path.exists(r"C:\Windows\System32\nvidia-smi.exe"):
        try:
            res = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=1.5,
            )
            if res.returncode == 0 and res.stdout.strip():
                parts = [p.strip() for p in res.stdout.strip().split(",")]
                if len(parts) >= 5:
                    return {
                        "name": parts[0],
                        "temp_c": float(parts[1]),
                        "util_percent": float(parts[2]),
                        "mem_used_mb": float(parts[3]),
                        "mem_total_mb": float(parts[4]),
                    }
        except Exception:
            pass

    # 3. Linux DRM / hwmon fallback (AMD / Intel iGPU)
    if _OS == "Linux":
        try:
            for drm_path in Path("/sys/class/drm").glob("card*/device/hwmon/hwmon*"):
                temp_file = drm_path / "temp1_input"
                if temp_file.exists():
                    val = float(temp_file.read_text().strip()) / 1000.0
                    return {
                        "name": "Integrated GPU",
                        "temp_c": round(val, 1),
                        "util_percent": -1.0,
                        "mem_used_mb": -1.0,
                        "mem_total_mb": -1.0,
                    }
        except Exception:
            pass

    return {
        "name": None,
        "temp_c": None,
        "util_percent": -1.0,
        "mem_used_mb": None,
        "mem_total_mb": None,
    }


def _get_gpu_usage() -> float:
    info = _get_gpu_info()
    return info["util_percent"] if info["util_percent"] is not None else -1.0


def _get_cpu_temp() -> float:
    """Accurately resolve the CPU package / core temperature."""
    # 1. psutil sensors_temperatures
    try:
        temps = psutil.sensors_temperatures()
        # Look for dedicated CPU temperature blocks
        for name in ["coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "it8688", "acpitz"]:
            if name in temps and temps[name]:
                # Look for package id or tctl first
                for entry in temps[name]:
                    lbl = (entry.label or "").lower()
                    if any(k in lbl for k in ("package", "tctl", "die", "cpu")):
                        return float(entry.current)
                # Fallback to the maximum of any core
                core_vals = [e.current for e in temps[name] if e.current and e.current > 0]
                if core_vals:
                    return float(max(core_vals))
                return float(temps[name][0].current)

        for entries in temps.values():
            if entries:
                return float(entries[0].current)
    except Exception:
        pass

    # 2. Linux sysfs fallback
    if _OS == "Linux":
        try:
            for p in Path("/sys/class/thermal").glob("thermal_zone*"):
                type_file = p / "type"
                temp_file = p / "temp"
                if type_file.exists() and temp_file.exists():
                    t_type = type_file.read_text().strip().lower()
                    if any(k in t_type for k in ("x86_pkg_temp", "tcpu", "cpu", "core", "acpitz")):
                        val = float(temp_file.read_text().strip())
                        return val / 1000.0 if val > 1000 else val
        except Exception:
            pass

    # 3. Windows WMI fallback
    if _OS == "Windows":
        try:
            import wmi  # type: ignore
            w = wmi.WMI(namespace="root/wmi")
            tz = w.MSAcpi_ThermalZoneTemperature()
            if tz:
                return (tz[0].CurrentTemperature / 10.0) - 273.15
        except Exception:
            pass

    return -1.0


def _get_laptop_telemetry() -> dict[str, Any]:
    """
    Extracts laptop chassis/motherboard thermal zones, fan speeds (RPM),
    and battery status.
    """
    sensors: dict[str, float] = {}
    chassis_temps: list[float] = []

    try:
        temps = psutil.sensors_temperatures()
        laptop_drivers = ("acer", "thinkpad", "asus", "dell_smm", "hp", "fujitsu", "acpitz", "spd5118", "nvme")
        for driver in laptop_drivers:
            if driver in temps:
                for idx, entry in enumerate(temps[driver]):
                    lbl = entry.label.strip() if entry.label else f"sensor_{idx + 1}"
                    key = f"{driver}_{lbl.lower().replace(' ', '_')}"
                    cur = round(float(entry.current), 1)
                    sensors[key] = cur
                    if driver in ("acer", "thinkpad", "asus", "dell_smm", "hp", "acpitz"):
                        chassis_temps.append(cur)
    except Exception:
        pass

    # Sysfs thermal zones fallback if no dedicated driver
    if not sensors and _OS == "Linux":
        try:
            for p in Path("/sys/class/thermal").glob("thermal_zone*"):
                type_file = p / "type"
                temp_file = p / "temp"
                if type_file.exists() and temp_file.exists():
                    t_name = type_file.read_text().strip()
                    val = float(temp_file.read_text().strip())
                    val_c = round(val / 1000.0 if val > 1000 else val, 1)
                    sensors[t_name.lower().replace(" ", "_")] = val_c
                    if any(k in t_name.lower() for k in ("sen", "acpi", "chassis", "ambient")):
                        chassis_temps.append(val_c)
        except Exception:
            pass

    laptop_temp_c = round(sum(chassis_temps) / len(chassis_temps), 1) if chassis_temps else None

    # Fans
    fan_list: list[dict[str, Any]] = []
    try:
        fans = psutil.sensors_fans()
        for dev, fan_entries in fans.items():
            for idx, f in enumerate(fan_entries):
                fan_list.append({
                    "device": dev,
                    "label": f.label or f"Fan {idx + 1}",
                    "rpm": f.current,
                })
    except Exception:
        pass

    # Battery
    battery_info = None
    try:
        bat = psutil.sensors_battery()
        if bat:
            battery_info = {
                "percent": round(bat.percent, 1),
                "power_plugged": bat.power_plugged,
            }
    except Exception:
        pass

    return {
        "laptop_temp_c": laptop_temp_c,
        "sensors": sensors,
        "fans": fan_list,
        "battery": battery_info,
    }


def get_system_status() -> dict[str, Any]:
    """Snapshot of current system metrics for the system_status tool."""
    cpu = psutil.cpu_percent(interval=0.2)
    ram = psutil.virtual_memory()
    cpu_temp = _get_cpu_temp()
    gpu_info = _get_gpu_info()
    laptop_data = _get_laptop_telemetry()

    boot_time   = psutil.boot_time()
    uptime_secs = time.time() - boot_time
    uptime_h    = int(uptime_secs // 3600)
    uptime_m    = int((uptime_secs % 3600) // 60)

    # Build human-readable summary
    summary_parts = []
    if cpu_temp > 0:
        summary_parts.append(f"CPU: {cpu_temp:.0f}°C ({cpu:.0f}% load)")
    else:
        summary_parts.append(f"CPU: {cpu:.0f}% load")

    if gpu_info.get("temp_c") is not None:
        gpu_name = gpu_info.get("name") or "GPU"
        gpu_load = f"{gpu_info['util_percent']:.0f}%" if gpu_info['util_percent'] >= 0 else "active"
        summary_parts.append(f"{gpu_name}: {gpu_info['temp_c']:.0f}°C ({gpu_load})")

    if laptop_data.get("laptop_temp_c") is not None:
        summary_parts.append(f"Laptop Chassis: {laptop_data['laptop_temp_c']:.0f}°C")

    if laptop_data.get("fans"):
        rpm_strs = [f"{f['rpm']} RPM" for f in laptop_data["fans"] if f.get("rpm", 0) > 0]
        if rpm_strs:
            summary_parts.append(f"Fans: {', '.join(rpm_strs)}")

    if laptop_data.get("battery"):
        b = laptop_data["battery"]
        state = "plugged in" if b.get("power_plugged") else "on battery"
        summary_parts.append(f"Battery: {b['percent']:.0f}% ({state})")

    ram_used = round(ram.used / 1024 ** 3, 1)
    ram_total = round(ram.total / 1024 ** 3, 1)
    summary_parts.append(f"RAM: {ram_used}/{ram_total} GB ({ram.percent:.0f}%)")

    return {
        "cpu_percent":    round(cpu, 1),
        "cpu_temp_c":     round(cpu_temp, 1) if cpu_temp > 0 else None,
        "gpu_name":       gpu_info.get("name"),
        "gpu_temp_c":     gpu_info.get("temp_c"),
        "gpu_percent":    round(gpu_info["util_percent"], 1) if gpu_info["util_percent"] >= 0 else None,
        "gpu_mem_used_mb": gpu_info.get("mem_used_mb"),
        "gpu_mem_total_mb": gpu_info.get("mem_total_mb"),
        "laptop_temp_c":  laptop_data.get("laptop_temp_c"),
        "laptop_sensors": laptop_data.get("sensors", {}),
        "fans":           laptop_data.get("fans", []),
        "battery":        laptop_data.get("battery"),
        "ram_percent":    round(ram.percent, 1),
        "ram_used_gb":    ram_used,
        "ram_total_gb":   ram_total,
        "uptime":         f"{uptime_h}h {uptime_m}m",
        "process_count":  len(psutil.pids()),
        "summary":        " | ".join(summary_parts),
    }


class SystemMonitor:
    """
    Stateful monitor — cooldown state persists across session reconnections.
    Call check() periodically; returns a [SYSTEM_ALERT] string or None.
    """

    def __init__(self, thresholds: dict | None = None):
        self.thresholds   = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        self._last_alert: dict[str, float] = {}
        self._cpu_streak  = 0

    def _can_alert(self, key: str) -> bool:
        return (time.monotonic() - self._last_alert.get(key, 0)) > _COOLDOWN

    def _record(self, key: str):
        self._last_alert[key] = time.monotonic()

    def check(self) -> str | None:
        try:
            status = get_system_status()
            cpu  = status["cpu_percent"]
            ram  = status["ram_percent"]
            temp = status["cpu_temp_c"] or -1.0
            gpu_t = status["gpu_temp_c"] or -1.0
            gpu_u = status["gpu_percent"] or -1.0
        except Exception:
            return None

        alerts: list[str] = []

        if cpu >= self.thresholds["cpu"]:
            self._cpu_streak += 1
            if self._cpu_streak >= _CPU_STREAK and self._can_alert("cpu"):
                alerts.append(
                    f"[SYSTEM_ALERT] CPU usage has been critically high ({cpu:.0f}%) "
                    "for several seconds. Warn the user and suggest closing heavy apps."
                )
                self._record("cpu")
                self._cpu_streak = 0
        else:
            self._cpu_streak = 0

        if ram >= self.thresholds["ram"] and self._can_alert("ram"):
            alerts.append(
                f"[SYSTEM_ALERT] RAM is at {ram:.0f}% — nearly exhausted. "
                "Suggest freeing memory."
            )
            self._record("ram")

        if temp > 0 and temp >= self.thresholds["temp"] and self._can_alert("temp"):
            alerts.append(
                f"[SYSTEM_ALERT] CPU temperature is {temp:.0f}°C — above the safe limit. "
                "Advise reducing system load or checking cooling."
            )
            self._record("temp")

        if gpu_t > 0 and gpu_t >= 85.0 and self._can_alert("gpu_temp"):
            alerts.append(
                f"[SYSTEM_ALERT] GPU temperature is {gpu_t:.0f}°C — running very hot."
            )
            self._record("gpu_temp")

        if gpu_u >= 0 and gpu_u >= self.thresholds["gpu"] and self._can_alert("gpu"):
            alerts.append(f"[SYSTEM_ALERT] GPU load is at {gpu_u:.0f}%.")
            self._record("gpu")

        return " ".join(alerts) if alerts else None
