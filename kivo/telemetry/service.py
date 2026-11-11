"""Single-thread telemetry sampler and snapshot assembler.

The service owns one daemon thread.  It does not use asyncio, a thread pool, or
a timer per metric.  Slow groups are sampled after the core CPU and memory
groups so a GPU hiccup cannot prevent the values the UI depends on from being
refreshed first.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import replace
from typing import Callable

from kivo.telemetry.gpu import GpuReader
from kivo.telemetry.models import (
    ERROR_DRIVER,
    ERROR_NOT_AVAILABLE,
    ERROR_READ_FAILED,
    STATUS_ERROR,
    STATUS_UNAVAILABLE,
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
from kivo.telemetry.readers import (
    BatteryReader,
    CpuInfo,
    CpuReader,
    FrequencyReader,
    MemoryReader,
    StorageReader,
    TelemetryPaths,
    TemperatureReader,
    read_cpu_info,
)

CPU_INTERVAL = 1.0
MEMORY_INTERVAL = 1.0
FREQUENCY_INTERVAL = 2.0
GPU_INTERVAL = 2.0
TEMPERATURE_INTERVAL = 3.0
BATTERY_INTERVAL = 5.0
STORAGE_INTERVAL = 30.0
DISCOVERY_INTERVAL = 60.0
PUBLISH_INTERVAL = 1.0

_WAIT_SLICE = 1.0


class TelemetryService:
    """Read-only system telemetry with an explicit start/close lifecycle."""

    def __init__(
        self,
        on_snapshot: Callable[[TelemetrySnapshot], None] | None = None,
        *,
        paths: TelemetryPaths | None = None,
        gpu_provider: object | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ):
        self._on_snapshot = on_snapshot
        self._paths = paths or TelemetryPaths()
        self._clock = clock
        self._wall_clock = wall_clock
        self._gpu = gpu_provider if gpu_provider is not None else GpuReader()
        self._owns_gpu = gpu_provider is None

        self._cpu_reader = CpuReader(self._paths)
        self._memory_reader = MemoryReader(self._paths)
        self._frequency_reader = FrequencyReader(self._paths)
        self._temperature_reader = TemperatureReader(self._paths)
        self._battery_reader = BatteryReader(self._paths)
        self._storage_reader = StorageReader(self._paths)

        self._cpu_info = CpuInfo(model=None, logical_cores=os.cpu_count())
        self._cpu_metric = CpuUtilization(
            status=STATUS_UNAVAILABLE, error=ERROR_NOT_AVAILABLE
        )
        self._frequency_metric = CpuFrequency(
            status=STATUS_UNAVAILABLE, error=ERROR_NOT_AVAILABLE
        )
        self._memory_metric = MemoryMetrics(
            status=STATUS_UNAVAILABLE, error=ERROR_NOT_AVAILABLE
        )
        self._gpu_metric = GpuMetrics(
            status=STATUS_UNAVAILABLE, error=ERROR_NOT_AVAILABLE
        )
        self._temperature_readings: tuple[TemperatureReading, ...] = ()
        self._battery_metric = BatteryMetrics(
            status=STATUS_UNAVAILABLE, error=ERROR_NOT_AVAILABLE
        )
        self._storage_metric = StorageMetrics(
            status=STATUS_UNAVAILABLE, error=ERROR_NOT_AVAILABLE
        )

        self._snapshot = TelemetrySnapshot.starting()
        self._sequence = 0
        self._next_due: dict[str, float] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._running = False
        self._closed = False
        self._initialized = False

    # ── Public API ─────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Start the sampler once.

        Returns ``False`` if the service was already closed; a closed service
        is terminal and a second start is explicitly rejected.
        """
        if self._closed:
            return False
        if self._running:
            return True
        self._started = True
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="kivo-telemetry",
            daemon=True,
        )
        self._thread.start()
        return True

    def close(self, timeout: float = 2.0) -> None:
        """Stop the sampler and join it within a bounded time."""
        self._closed = True
        self._running = False
        self._stop_event.set()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and threading.current_thread() is not thread
        ):
            thread.join(timeout=max(0.0, float(timeout)))

    def latest_snapshot(self) -> TelemetrySnapshot:
        """Return the last immutable snapshot without performing I/O."""
        return self._snapshot

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return bool(self._running and thread is not None and thread.is_alive())

    @property
    def thread_name(self) -> str | None:
        thread = self._thread
        return thread.name if thread is not None else None

    # ── Thread loop ────────────────────────────────────────────────────────

    def _thread_main(self) -> None:
        try:
            now_monotonic = self._clock()
            now_wall = self._wall_clock()
            self._initialize_sources(now_monotonic)
            while not self._stop_event.is_set():
                now_monotonic = self._clock()
                self._run_due(now_monotonic)
                delay = self._seconds_until_due(now_monotonic)
                self._stop_event.wait(min(max(0.0, delay), _WAIT_SLICE))
        except Exception:
            # A telemetry failure must never take down the backend.  Preserve
            # the last snapshot; its age and stale flag make the failure
            # visible instead of silently presenting old values as live.
            pass
        finally:
            self._running = False
            if self._owns_gpu:
                try:
                    self._gpu.close()
                except Exception:
                    pass

    def _initialize_sources(self, now_monotonic: float) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._frequency_reader.discover()
        self._temperature_reader.discover()
        try:
            self._cpu_info = read_cpu_info(self._paths)
        except Exception:
            self._cpu_info = CpuInfo(
                model=None, logical_cores=os.cpu_count()
            )
        try:
            start = getattr(self._gpu, "start", None)
            if start is not None:
                start(now_monotonic)
        except Exception:
            pass

        self._next_due = {
            "cpu": now_monotonic,
            "memory": now_monotonic,
            "frequency": now_monotonic,
            "gpu": now_monotonic,
            "temperature": now_monotonic,
            "battery": now_monotonic,
            "storage": now_monotonic,
            "discovery": now_monotonic + DISCOVERY_INTERVAL,
            "publish": now_monotonic,
        }

    def _run_due(self, now_monotonic: float) -> None:
        # Core values are sampled first; only then do optional/slow groups run.
        if self._is_due("cpu", now_monotonic):
            self._sample_cpu(now_monotonic)
        if self._is_due("memory", now_monotonic):
            self._sample_memory(now_monotonic)
        if self._is_due("storage", now_monotonic):
            self._sample_storage(now_monotonic)
        if self._is_due("battery", now_monotonic):
            self._sample_battery(now_monotonic)
        if self._is_due("temperature", now_monotonic):
            self._sample_temperature(now_monotonic)
        if self._is_due("frequency", now_monotonic):
            self._sample_frequency(now_monotonic)
        if self._is_due("gpu", now_monotonic):
            self._sample_gpu(now_monotonic)
        if self._is_due("discovery", now_monotonic):
            self._rediscover(now_monotonic)
        if self._is_due("publish", now_monotonic):
            self._publish(now_monotonic)

    def _is_due(self, key: str, now_monotonic: float) -> bool:
        deadline = self._next_due.get(key)
        return deadline is not None and now_monotonic >= deadline

    def _seconds_until_due(self, now_monotonic: float) -> float:
        if not self._next_due:
            return _WAIT_SLICE
        return min(self._next_due.values()) - now_monotonic

    # ── Group sampling ─────────────────────────────────────────────────────

    def _sample_cpu(self, now_monotonic: float) -> None:
        wall = self._wall_clock()
        try:
            self._cpu_metric = self._cpu_reader.sample(
                sampled_at=wall, sampled_monotonic=now_monotonic
            )
        except Exception:
            self._cpu_metric = CpuUtilization(
                status=STATUS_ERROR,
                sampled_at=self._cpu_metric.sampled_at,
                sampled_monotonic=self._cpu_metric.sampled_monotonic,
                error=ERROR_READ_FAILED,
            )
        self._next_due["cpu"] = self._clock() + CPU_INTERVAL

    def _sample_memory(self, now_monotonic: float) -> None:
        wall = self._wall_clock()
        try:
            self._memory_metric = self._memory_reader.sample(
                sampled_at=wall, sampled_monotonic=now_monotonic
            )
        except Exception:
            self._memory_metric = MemoryMetrics(
                status=STATUS_ERROR,
                sampled_at=self._memory_metric.sampled_at,
                sampled_monotonic=self._memory_metric.sampled_monotonic,
                error=ERROR_READ_FAILED,
            )
        self._next_due["memory"] = self._clock() + MEMORY_INTERVAL

    def _sample_frequency(self, now_monotonic: float) -> None:
        wall = self._wall_clock()
        try:
            self._frequency_metric = self._frequency_reader.sample(
                sampled_at=wall, sampled_monotonic=now_monotonic
            )
        except Exception:
            self._frequency_metric = CpuFrequency(
                status=STATUS_ERROR,
                sampled_at=self._frequency_metric.sampled_at,
                sampled_monotonic=self._frequency_metric.sampled_monotonic,
                error=ERROR_READ_FAILED,
            )
        self._next_due["frequency"] = self._clock() + FREQUENCY_INTERVAL

    def _sample_temperature(self, now_monotonic: float) -> None:
        wall = self._wall_clock()
        try:
            self._temperature_readings = self._temperature_reader.sample(
                sampled_at=wall, sampled_monotonic=now_monotonic
            )
        except Exception:
            self._temperature_readings = tuple(
                replace(
                    reading,
                    status=STATUS_ERROR,
                    celsius=None,
                    error=ERROR_READ_FAILED,
                )
                for reading in self._temperature_readings
            )
        self._next_due["temperature"] = self._clock() + TEMPERATURE_INTERVAL

    def _sample_gpu(self, now_monotonic: float) -> None:
        wall = self._wall_clock()
        try:
            self._gpu_metric = self._gpu.sample(
                sampled_at=wall, sampled_monotonic=now_monotonic
            )
        except Exception:
            self._gpu_metric = GpuMetrics(
                status=STATUS_ERROR,
                sampled_at=self._gpu_metric.sampled_at,
                sampled_monotonic=self._gpu_metric.sampled_monotonic,
                error=ERROR_DRIVER,
            )
        self._next_due["gpu"] = self._clock() + GPU_INTERVAL

    def _sample_battery(self, now_monotonic: float) -> None:
        wall = self._wall_clock()
        try:
            self._battery_metric = self._battery_reader.sample(
                sampled_at=wall, sampled_monotonic=now_monotonic
            )
        except Exception:
            self._battery_metric = BatteryMetrics(
                status=STATUS_ERROR,
                sampled_at=self._battery_metric.sampled_at,
                sampled_monotonic=self._battery_metric.sampled_monotonic,
                error=ERROR_READ_FAILED,
            )
        self._next_due["battery"] = self._clock() + BATTERY_INTERVAL

    def _sample_storage(self, now_monotonic: float) -> None:
        wall = self._wall_clock()
        try:
            self._storage_metric = self._storage_reader.sample(
                sampled_at=wall, sampled_monotonic=now_monotonic
            )
        except Exception:
            self._storage_metric = StorageMetrics(
                mount=self._paths.root_mount,
                status=STATUS_ERROR,
                sampled_at=self._storage_metric.sampled_at,
                sampled_monotonic=self._storage_metric.sampled_monotonic,
                error=ERROR_READ_FAILED,
            )
        self._next_due["storage"] = self._clock() + STORAGE_INTERVAL

    def _rediscover(self, now_monotonic: float) -> None:
        try:
            self._temperature_reader.discover()
        except Exception:
            pass
        try:
            self._frequency_reader.discover()
        except Exception:
            pass
        self._next_due["discovery"] = self._clock() + DISCOVERY_INTERVAL

    # ── Publication ────────────────────────────────────────────────────────

    def _publish(self, now_monotonic: float) -> None:
        wall = self._wall_clock()
        self._sequence += 1
        snapshot = TelemetrySnapshot(
            sequence=self._sequence,
            sampled_at=wall,
            sampled_monotonic=now_monotonic,
            cpu=CpuMetrics(
                model=self._cpu_info.model,
                logical_cores=self._cpu_info.logical_cores,
                utilization=self._cpu_metric,
                frequency=self._frequency_metric,
            ),
            memory=self._memory_metric,
            gpu=self._gpu_metric,
            temperatures=self._temperature_readings,
            battery=self._battery_metric,
            storage=self._storage_metric,
        )
        snapshot = replace(
            snapshot,
            service_state=snapshot.service_state_at(now_monotonic),
        )
        self._snapshot = snapshot
        self._next_due["publish"] = self._clock() + PUBLISH_INTERVAL

        callback = self._on_snapshot
        if callback is not None and not self._closed:
            try:
                callback(snapshot)
            except Exception:
                pass


__all__ = [
    "TelemetryService",
    "CPU_INTERVAL",
    "MEMORY_INTERVAL",
    "FREQUENCY_INTERVAL",
    "GPU_INTERVAL",
    "TEMPERATURE_INTERVAL",
    "BATTERY_INTERVAL",
    "STORAGE_INTERVAL",
    "DISCOVERY_INTERVAL",
    "PUBLISH_INTERVAL",
]
