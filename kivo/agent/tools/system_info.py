"""System overview and hardware telemetry tool for KIVO agent runtime."""
from __future__ import annotations

import os
import platform
from typing import Any

import psutil

from kivo.agent.tool_schema import PermissionLevel, ToolDefinition


def _get_os_info() -> str:
    """Resolve operating system and distribution name cleanly."""
    try:
        os_release = platform.freedesktop_os_release()
        pretty = os_release.get("PRETTY_NAME")
        if pretty:
            return pretty
    except Exception:
        pass

    try:
        if os.path.exists("/etc/arch-release"):
            return "Arch Linux"
    except Exception:
        pass

    return f"{platform.system()} {platform.release()}"


def _get_cpu_model() -> str:
    """Resolve CPU brand/model name from /proc/cpuinfo on Linux."""
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
            for line in f:
                if "model name" in line:
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "Unknown CPU"


def handle_get_system_overview(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return read-only system and hardware overview."""
    # OS & Kernel
    os_name = _get_os_info()
    kernel_version = platform.release()
    arch = platform.machine()

    # CPU
    cpu_model = _get_cpu_model()
    cpu_cores_physical = psutil.cpu_count(logical=False) or 1
    cpu_cores_logical = psutil.cpu_count(logical=True) or 1
    cpu_percent = psutil.cpu_percent(interval=0.1)

    # Memory
    mem = psutil.virtual_memory()
    mem_used_gb = round(mem.used / (1024 ** 3), 1)
    mem_total_gb = round(mem.total / (1024 ** 3), 1)

    # Disk
    disk = psutil.disk_usage("/")
    disk_used_gb = round(disk.used / (1024 ** 3), 1)
    disk_total_gb = round(disk.total / (1024 ** 3), 1)
    disk_percent = disk.percent

    # GPU telemetry (reuse existing action if available)
    gpu_data = None
    try:
        from actions.system_monitor import _get_gpu_info
        raw_gpu = _get_gpu_info()
        if raw_gpu.get("name"):
            gpu_data = {
                "name": raw_gpu.get("name"),
                "temp_c": raw_gpu.get("temp_c"),
                "util_percent": raw_gpu.get("util_percent"),
                "memory_used_mb": raw_gpu.get("mem_used_mb"),
                "memory_total_mb": raw_gpu.get("mem_total_mb"),
            }
    except Exception:
        pass

    return {
        "os": os_name,
        "kernel": kernel_version,
        "architecture": arch,
        "cpu": {
            "model": cpu_model,
            "physical_cores": cpu_cores_physical,
            "logical_cores": cpu_cores_logical,
            "utilization_percent": cpu_percent,
        },
        "memory": {
            "used_gb": mem_used_gb,
            "total_gb": mem_total_gb,
            "percent": mem.percent,
        },
        "disk_root": {
            "used_gb": disk_used_gb,
            "total_gb": disk_total_gb,
            "percent": disk_percent,
        },
        "gpu": gpu_data,
    }


SYSTEM_OVERVIEW_TOOL = ToolDefinition(
    name="get_system_overview",
    description="Retrieve read-only system information, including OS distribution, kernel version, CPU cores and usage, memory (RAM), root disk usage, and GPU metrics.",
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    handler=handle_get_system_overview,
    permission=PermissionLevel.READ_ONLY,
    timeout_seconds=5.0,
)
