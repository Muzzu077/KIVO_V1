"""Read-only system telemetry for KIVO.

Stage 12.2.1 exposes the service to the application lifecycle only.  IPC
operations, events and QML bindings are intentionally absent until later
stages.
"""
from __future__ import annotations

from kivo.telemetry.gpu import GpuReader
from kivo.telemetry.models import (
    SCHEMA_VERSION,
    BatteryMetrics,
    CpuFrequency,
    CpuMetrics,
    CpuUtilization,
    GpuMetrics,
    MemoryMetrics,
    StorageMetrics,
    TelemetrySnapshot,
    TemperatureReading,
)
from kivo.telemetry.readers import TelemetryPaths
from kivo.telemetry.service import TelemetryService

__all__ = [
    "SCHEMA_VERSION",
    "BatteryMetrics",
    "CpuFrequency",
    "CpuMetrics",
    "CpuUtilization",
    "GpuMetrics",
    "GpuReader",
    "MemoryMetrics",
    "StorageMetrics",
    "TelemetryPaths",
    "TelemetryService",
    "TelemetrySnapshot",
    "TemperatureReading",
]
