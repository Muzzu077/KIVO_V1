"""Immutable telemetry snapshots and metric status values.

A snapshot is published by swapping an immutable reference.  ``age_seconds``
and ``stale`` are derived when the snapshot is serialized so an old snapshot
can never be mistaken for a fresh one just because the service stopped
publishing.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import ClassVar

SCHEMA_VERSION = 1

STATUS_OK = "ok"
STATUS_WARMING_UP = "warming_up"
STATUS_UNSUPPORTED = "unsupported"
STATUS_UNAVAILABLE = "unavailable"
STATUS_STALE = "stale"
STATUS_ERROR = "error"

STATUSES = frozenset(
    {
        STATUS_OK,
        STATUS_WARMING_UP,
        STATUS_UNSUPPORTED,
        STATUS_UNAVAILABLE,
        STATUS_STALE,
        STATUS_ERROR,
    }
)

ERROR_READ_FAILED = "read_failed"
ERROR_PARSE_FAILED = "parse_failed"
ERROR_TIMEOUT = "timeout"
ERROR_DRIVER = "driver_error"
ERROR_NO_SENSOR = "no_sensor"
ERROR_COUNTER_RESET = "counter_reset"
ERROR_NOT_SUPPORTED = "not_supported"
ERROR_NOT_AVAILABLE = "not_available"
ERROR_INVALID_VALUE = "invalid_value"
ERROR_OUTPUT_TOO_LARGE = "output_too_large"


def _round_or_none(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)


def _bounded_percent(value: float | None) -> float | None:
    if value is None:
        return None
    return min(100.0, max(0.0, float(value)))


@dataclass(frozen=True, kw_only=True)
class Metric:
    """Common status and freshness fields for every metric group."""

    status: str = STATUS_UNAVAILABLE
    sampled_at: float | None = None
    sampled_monotonic: float | None = field(
        default=None, repr=False, compare=False
    )
    error: str | None = None

    stale_after: ClassVar[float] = 3.0

    def age_seconds(self, now_monotonic: float | None = None) -> float | None:
        if self.sampled_monotonic is None:
            return None
        now = time.monotonic() if now_monotonic is None else now_monotonic
        return max(0.0, float(now) - self.sampled_monotonic)

    def is_stale(self, now_monotonic: float | None = None) -> bool:
        if self.status == STATUS_STALE:
            return True
        if self.status != STATUS_OK:
            return False
        age = self.age_seconds(now_monotonic)
        return age is not None and age > self.stale_after

    def effective_status(self, now_monotonic: float | None = None) -> str:
        if self.is_stale(now_monotonic):
            return STATUS_STALE
        return self.status

    def meta_dict(self, now_monotonic: float | None = None) -> dict:
        age = self.age_seconds(now_monotonic)
        stale = self.is_stale(now_monotonic)
        return {
            "status": STATUS_STALE if stale else self.status,
            "sampled_at": _round_or_none(self.sampled_at, 3),
            "age_seconds": _round_or_none(age, 3),
            "stale": stale,
            "error": self.error,
        }

    def _payload(self, values: dict, now_monotonic: float | None) -> dict:
        payload = self.meta_dict(now_monotonic)
        payload.update(values)
        return payload


@dataclass(frozen=True, kw_only=True)
class CpuUtilization(Metric):
    stale_after: ClassVar[float] = 3.0
    percent: float | None = None

    def to_dict(self, now_monotonic: float | None = None) -> dict:
        return self._payload(
            {"percent": _bounded_percent(self.percent)}, now_monotonic
        )


@dataclass(frozen=True, kw_only=True)
class CpuFrequency(Metric):
    stale_after: ClassVar[float] = 5.0
    mhz: float | None = None
    max_mhz: float | None = None

    def to_dict(self, now_monotonic: float | None = None) -> dict:
        return self._payload(
            {
                "mhz": _round_or_none(self.mhz, 1),
                "max_mhz": _round_or_none(self.max_mhz, 1),
            },
            now_monotonic,
        )


@dataclass(frozen=True, kw_only=True)
class CpuMetrics:
    model: str | None = None
    logical_cores: int | None = None
    utilization: CpuUtilization = field(default_factory=CpuUtilization)
    frequency: CpuFrequency = field(default_factory=CpuFrequency)

    def to_dict(self, now_monotonic: float | None = None) -> dict:
        return {
            "model": self.model,
            "logical_cores": self.logical_cores,
            "utilization": self.utilization.to_dict(now_monotonic),
            "frequency": self.frequency.to_dict(now_monotonic),
        }


@dataclass(frozen=True, kw_only=True)
class MemoryMetrics(Metric):
    stale_after: ClassVar[float] = 3.0
    total_bytes: int | None = None
    used_bytes: int | None = None
    available_bytes: int | None = None
    percent: float | None = None

    def to_dict(self, now_monotonic: float | None = None) -> dict:
        return self._payload(
            {
                "total_bytes": self.total_bytes,
                "used_bytes": self.used_bytes,
                "available_bytes": self.available_bytes,
                "percent": _bounded_percent(self.percent),
            },
            now_monotonic,
        )


@dataclass(frozen=True, kw_only=True)
class GpuMetrics(Metric):
    stale_after: ClassVar[float] = 6.0
    name: str | None = None
    utilization_percent: float | None = None
    temperature_c: float | None = None
    memory_used_bytes: int | None = None
    memory_total_bytes: int | None = None
    memory_percent: float | None = None
    source: str | None = None

    def to_dict(self, now_monotonic: float | None = None) -> dict:
        return self._payload(
            {
                "name": self.name,
                "utilization_percent": _bounded_percent(self.utilization_percent),
                "temperature_c": _round_or_none(self.temperature_c, 1),
                "memory_used_bytes": self.memory_used_bytes,
                "memory_total_bytes": self.memory_total_bytes,
                "memory_percent": _bounded_percent(self.memory_percent),
                "source": self.source,
            },
            now_monotonic,
        )


@dataclass(frozen=True, kw_only=True)
class TemperatureReading(Metric):
    stale_after: ClassVar[float] = 8.0
    id: str
    label: str
    celsius: float | None = None
    source: str | None = None

    def to_dict(self, now_monotonic: float | None = None) -> dict:
        return self._payload(
            {
                "id": self.id,
                "label": self.label,
                "celsius": _round_or_none(self.celsius, 2),
                "source": self.source,
            },
            now_monotonic,
        )


@dataclass(frozen=True, kw_only=True)
class BatteryMetrics(Metric):
    stale_after: ClassVar[float] = 15.0
    present: bool | None = None
    percent: int | None = None
    state: str | None = None
    ac_online: bool | None = None

    def to_dict(self, now_monotonic: float | None = None) -> dict:
        return self._payload(
            {
                "present": self.present,
                "percent": self.percent,
                "state": self.state,
                "ac_online": self.ac_online,
            },
            now_monotonic,
        )


@dataclass(frozen=True, kw_only=True)
class StorageMetrics(Metric):
    stale_after: ClassVar[float] = 60.0
    mount: str = "/"
    total_bytes: int | None = None
    used_bytes: int | None = None
    available_bytes: int | None = None
    percent: float | None = None

    def to_dict(self, now_monotonic: float | None = None) -> dict:
        return self._payload(
            {
                "mount": self.mount,
                "total_bytes": self.total_bytes,
                "used_bytes": self.used_bytes,
                "available_bytes": self.available_bytes,
                "percent": _bounded_percent(self.percent),
            },
            now_monotonic,
        )


@dataclass(frozen=True, kw_only=True)
class TelemetrySnapshot:
    schema_version: int = SCHEMA_VERSION
    sequence: int = 0
    sampled_at: float | None = None
    sampled_monotonic: float | None = field(
        default=None, repr=False, compare=False
    )
    service_state: str = "starting"
    cpu: CpuMetrics = field(default_factory=CpuMetrics)
    memory: MemoryMetrics = field(default_factory=MemoryMetrics)
    gpu: GpuMetrics = field(default_factory=GpuMetrics)
    temperatures: tuple[TemperatureReading, ...] = ()
    battery: BatteryMetrics = field(default_factory=BatteryMetrics)
    storage: StorageMetrics = field(default_factory=StorageMetrics)

    def service_state_at(self, now_monotonic: float | None = None) -> str:
        if self.service_state == "degraded":
            return "degraded"
        if self.sequence <= 0:
            return "starting"
        cpu = self.cpu.utilization
        if cpu.status == STATUS_WARMING_UP or cpu.sampled_at is None:
            return "starting"
        if self.memory.sampled_at is None:
            return "starting"
        if (
            cpu.effective_status(now_monotonic) == STATUS_OK
            and self.memory.effective_status(now_monotonic) == STATUS_OK
        ):
            return "ok"
        return "degraded"

    def to_dict(self, now_monotonic: float | None = None) -> dict:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "sampled_at": _round_or_none(self.sampled_at, 3),
            "service_state": self.service_state_at(now),
            "cpu": self.cpu.to_dict(now),
            "memory": self.memory.to_dict(now),
            "gpu": self.gpu.to_dict(now),
            "temperatures": [
                reading.to_dict(now) for reading in self.temperatures
            ],
            "battery": self.battery.to_dict(now),
            "storage": self.storage.to_dict(now),
        }

    @classmethod
    def starting(cls, *, sampled_at: float | None = None) -> "TelemetrySnapshot":
        return cls(
            sequence=0,
            sampled_at=sampled_at,
            service_state="starting",
            cpu=CpuMetrics(
                utilization=CpuUtilization(
                    status=STATUS_UNAVAILABLE, error=ERROR_NOT_AVAILABLE
                ),
                frequency=CpuFrequency(
                    status=STATUS_UNAVAILABLE, error=ERROR_NOT_AVAILABLE
                ),
            ),
            memory=MemoryMetrics(
                status=STATUS_UNAVAILABLE, error=ERROR_NOT_AVAILABLE
            ),
            gpu=GpuMetrics(status=STATUS_UNAVAILABLE, error=ERROR_NOT_AVAILABLE),
            battery=BatteryMetrics(
                status=STATUS_UNAVAILABLE, error=ERROR_NOT_AVAILABLE
            ),
            storage=StorageMetrics(
                status=STATUS_UNAVAILABLE, error=ERROR_NOT_AVAILABLE
            ),
        )

    @classmethod
    def unavailable(
        cls, *, reason: str = ERROR_NOT_AVAILABLE
    ) -> "TelemetrySnapshot":
        return cls.starting().with_error(reason)

    def with_error(self, reason: str) -> "TelemetrySnapshot":
        return TelemetrySnapshot(
            schema_version=self.schema_version,
            sequence=self.sequence,
            sampled_at=self.sampled_at,
            sampled_monotonic=self.sampled_monotonic,
            service_state="degraded",
            cpu=CpuMetrics(
                model=self.cpu.model,
                logical_cores=self.cpu.logical_cores,
                utilization=CpuUtilization(
                    status=STATUS_UNAVAILABLE, error=reason
                ),
                frequency=CpuFrequency(
                    status=STATUS_UNAVAILABLE, error=reason
                ),
            ),
            memory=MemoryMetrics(status=STATUS_UNAVAILABLE, error=reason),
            gpu=GpuMetrics(status=STATUS_UNAVAILABLE, error=reason),
            temperatures=(),
            battery=BatteryMetrics(status=STATUS_UNAVAILABLE, error=reason),
            storage=StorageMetrics(status=STATUS_UNAVAILABLE, error=reason),
        )


__all__ = [
    "SCHEMA_VERSION",
    "STATUS_OK",
    "STATUS_WARMING_UP",
    "STATUS_UNSUPPORTED",
    "STATUS_UNAVAILABLE",
    "STATUS_STALE",
    "STATUS_ERROR",
    "STATUSES",
    "ERROR_READ_FAILED",
    "ERROR_PARSE_FAILED",
    "ERROR_TIMEOUT",
    "ERROR_DRIVER",
    "ERROR_NO_SENSOR",
    "ERROR_COUNTER_RESET",
    "ERROR_NOT_SUPPORTED",
    "ERROR_NOT_AVAILABLE",
    "ERROR_INVALID_VALUE",
    "ERROR_OUTPUT_TOO_LARGE",
    "Metric",
    "CpuUtilization",
    "CpuFrequency",
    "CpuMetrics",
    "MemoryMetrics",
    "GpuMetrics",
    "TemperatureReading",
    "BatteryMetrics",
    "StorageMetrics",
    "TelemetrySnapshot",
]
