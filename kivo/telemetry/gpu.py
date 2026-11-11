"""Optional NVIDIA telemetry: NVML first, one bounded ``nvidia-smi`` fallback.

No per-process or per-device UUID queries are made.  The fallback is one
process per GPU cycle, only when NVML cannot initialize at startup, and its
output is capped and parsed strictly.
"""
from __future__ import annotations

import importlib
import os
import re
import shutil
import warnings
from typing import Callable, Mapping, Sequence

from kivo.core.bounded_process import BoundedProcessResult, run_bounded
from kivo.telemetry.models import (
    ERROR_DRIVER,
    ERROR_INVALID_VALUE,
    ERROR_NOT_AVAILABLE,
    ERROR_NOT_SUPPORTED,
    ERROR_OUTPUT_TOO_LARGE,
    ERROR_PARSE_FAILED,
    ERROR_TIMEOUT,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    STATUS_UNSUPPORTED,
    GpuMetrics,
)

_NVIDIA_SMI_ARGV = (
    "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
    "--format=csv,noheader,nounits",
)
_SMI_TIMEOUT_SECONDS = 2.0
_SMI_STDOUT_LIMIT = 16 * 1024
_SMI_STDERR_LIMIT = 4 * 1024
_NVML_REINIT_SECONDS = 60.0
_NAME_LIMIT = 128
_UNSET = object()


def _sanitize_name(value: str) -> str:
    printable = "".join(
        character if character.isprintable() else " " for character in value
    )
    return re.sub(r"\s+", " ", printable).strip()[:_NAME_LIMIT]


def _valid_temperature(value: float) -> bool:
    return -40.0 <= value <= 150.0


def _valid_utilization(value: float) -> bool:
    return 0.0 <= value <= 100.0


class GpuReader:
    """Process-wide NVML owner with a single bounded CLI fallback."""

    def __init__(
        self,
        *,
        nvml_module: object | None = None,
        enable_nvml: bool = True,
        nvidia_smi_path: str | None | object = _UNSET,
        runner: Callable[..., BoundedProcessResult] = run_bounded,
        timeout: float = _SMI_TIMEOUT_SECONDS,
    ):
        self._nvml_module = nvml_module
        self._enable_nvml = enable_nvml
        self._runner = runner
        self._timeout = float(timeout)
        self._smi_path: str | None = None
        self._smi_requested = nvidia_smi_path

        self._started = False
        self._closed = False
        self._nvml_state = "missing"
        self._nvml_handle: object | None = None
        self._owns_nvml = False
        self._last_init_attempt: float | None = None
        self._last_sampled_at: float | None = None
        self._last_sampled_monotonic: float | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self, now_monotonic: float | None = None) -> None:
        if self._started or self._closed:
            return
        self._started = True
        self._resolve_smi()
        self._initialize_nvml(now_monotonic)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_nvml and self._nvml_module is not None:
            try:
                self._nvml_module.nvmlShutdown()
            except Exception:
                pass
        self._owns_nvml = False
        self._nvml_handle = None
        self._nvml_state = "closed"

    # ── Sampling ───────────────────────────────────────────────────────────

    def sample(
        self, *, sampled_at: float, sampled_monotonic: float
    ) -> GpuMetrics:
        if self._closed:
            return self._error_metric(
                sampled_at, sampled_monotonic, ERROR_NOT_AVAILABLE
            )
        if not self._started:
            self.start(sampled_monotonic)

        if self._nvml_state == "ready":
            return self._sample_nvml(sampled_at, sampled_monotonic)

        if self._nvml_state == "failed":
            last = self._last_init_attempt
            if last is None or sampled_monotonic - last >= _NVML_REINIT_SECONDS:
                self._initialize_nvml(sampled_monotonic)
                if self._nvml_state == "ready":
                    return self._sample_nvml(sampled_at, sampled_monotonic)

        if self._nvml_state == "no_device":
            return GpuMetrics(
                status=STATUS_UNSUPPORTED,
                error=ERROR_NOT_SUPPORTED,
            )

        if self._smi_path is not None:
            return self._sample_nvidia_smi(sampled_at, sampled_monotonic)

        if self._nvml_state == "failed":
            return self._error_metric(
                sampled_at,
                sampled_monotonic,
                ERROR_NOT_AVAILABLE,
                status=STATUS_UNAVAILABLE,
            )
        return GpuMetrics(status=STATUS_UNSUPPORTED, error=ERROR_NOT_SUPPORTED)

    # ── NVML ───────────────────────────────────────────────────────────────

    def _initialize_nvml(self, now_monotonic: float | None) -> None:
        if not self._enable_nvml:
            self._nvml_state = "missing"
            return

        module = self._nvml_module
        if module is None:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    module = importlib.import_module("pynvml")
            except Exception:
                self._nvml_state = "missing"
                return

        self._last_init_attempt = now_monotonic
        try:
            module.nvmlInit()
            self._owns_nvml = True
            count = int(module.nvmlDeviceGetCount())
            if count <= 0:
                self._nvml_state = "no_device"
                return
            self._nvml_handle = module.nvmlDeviceGetHandleByIndex(0)
            self._nvml_module = module
            self._nvml_state = "ready"
        except Exception:
            if self._owns_nvml and module is not None:
                try:
                    module.nvmlShutdown()
                except Exception:
                    pass
            self._owns_nvml = False
            self._nvml_handle = None
            self._nvml_module = module
            self._nvml_state = "failed"

    def _sample_nvml(
        self, sampled_at: float, sampled_monotonic: float
    ) -> GpuMetrics:
        module = self._nvml_module
        handle = self._nvml_handle
        if module is None or handle is None:
            return self._error_metric(
                sampled_at, sampled_monotonic, ERROR_DRIVER
            )
        try:
            raw_name = module.nvmlDeviceGetName(handle)
            if isinstance(raw_name, bytes):
                name = raw_name.decode("utf-8", errors="replace")
            else:
                name = str(raw_name)
            name = _sanitize_name(name) or "NVIDIA GPU"

            temperature = float(
                module.nvmlDeviceGetTemperature(
                    handle, module.NVML_TEMPERATURE_GPU
                )
            )
            utilization = float(module.nvmlDeviceGetUtilizationRates(handle).gpu)
            memory = module.nvmlDeviceGetMemoryInfo(handle)
            used = int(memory.used)
            total = int(memory.total)
        except Exception:
            return self._error_metric(
                sampled_at, sampled_monotonic, ERROR_DRIVER
            )

        if not self._valid_numeric(temperature, utilization, used, total):
            return self._error_metric(
                sampled_at, sampled_monotonic, ERROR_INVALID_VALUE
            )

        self._remember(sampled_at, sampled_monotonic)
        return GpuMetrics(
            status=STATUS_OK,
            name=name,
            utilization_percent=utilization,
            temperature_c=temperature,
            memory_used_bytes=used,
            memory_total_bytes=total,
            memory_percent=used / total * 100.0,
            source="nvml",
            sampled_at=sampled_at,
            sampled_monotonic=sampled_monotonic,
        )

    # ── nvidia-smi fallback ────────────────────────────────────────────────

    def _resolve_smi(self) -> None:
        requested = self._smi_requested
        if requested is _UNSET:
            path = shutil.which("nvidia-smi")
        elif requested is None:
            path = None
        else:
            path = str(requested)

        if not path:
            self._smi_path = None
            return
        candidate = os.path.abspath(path)
        if (
            os.path.isabs(candidate)
            and os.path.isfile(candidate)
            and os.access(candidate, os.X_OK)
        ):
            self._smi_path = candidate
        else:
            self._smi_path = None

    def _sample_nvidia_smi(
        self, sampled_at: float, sampled_monotonic: float
    ) -> GpuMetrics:
        path = self._smi_path
        if path is None:
            return GpuMetrics(status=STATUS_UNSUPPORTED, error=ERROR_NOT_SUPPORTED)

        argv: Sequence[str] = (path, *_NVIDIA_SMI_ARGV)
        try:
            result = self._runner(
                list(argv),
                timeout=self._timeout,
                stdout_limit=_SMI_STDOUT_LIMIT,
                stderr_limit=_SMI_STDERR_LIMIT,
                env={"LC_ALL": "C", "LANG": "C"},
            )
        except OSError:
            return self._error_metric(
                sampled_at, sampled_monotonic, ERROR_DRIVER
            )
        except Exception:
            return self._error_metric(
                sampled_at, sampled_monotonic, ERROR_DRIVER
            )

        if result.timed_out:
            return self._error_metric(
                sampled_at, sampled_monotonic, ERROR_TIMEOUT
            )
        if result.stdout_truncated or result.stderr_truncated:
            return self._error_metric(
                sampled_at, sampled_monotonic, ERROR_OUTPUT_TOO_LARGE
            )
        if result.returncode != 0:
            return self._error_metric(
                sampled_at, sampled_monotonic, ERROR_DRIVER
            )

        line = next(
            (candidate.strip() for candidate in result.stdout.splitlines() if candidate.strip()),
            "",
        )
        if not line:
            return self._error_metric(
                sampled_at, sampled_monotonic, ERROR_PARSE_FAILED
            )

        parts = [part.strip() for part in line.rsplit(",", 4)]
        if len(parts) != 5:
            return self._error_metric(
                sampled_at, sampled_monotonic, ERROR_PARSE_FAILED
            )

        name = _sanitize_name(parts[0]) or "NVIDIA GPU"
        try:
            temperature = float(parts[1])
            utilization = float(parts[2])
            used_mib = float(parts[3])
            total_mib = float(parts[4])
        except (TypeError, ValueError):
            return self._error_metric(
                sampled_at, sampled_monotonic, ERROR_PARSE_FAILED
            )

        if used_mib < 0 or total_mib <= 0 or used_mib > total_mib:
            return self._error_metric(
                sampled_at, sampled_monotonic, ERROR_INVALID_VALUE
            )
        if not self._valid_numeric(
            temperature, utilization, 0, total_mib
        ):
            return self._error_metric(
                sampled_at, sampled_monotonic, ERROR_INVALID_VALUE
            )

        used = int(used_mib * 1024 * 1024)
        total = int(total_mib * 1024 * 1024)
        self._remember(sampled_at, sampled_monotonic)
        return GpuMetrics(
            status=STATUS_OK,
            name=name,
            utilization_percent=utilization,
            temperature_c=temperature,
            memory_used_bytes=used,
            memory_total_bytes=total,
            memory_percent=used / total * 100.0,
            source="nvidia-smi",
            sampled_at=sampled_at,
            sampled_monotonic=sampled_monotonic,
        )

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _valid_numeric(
        temperature: float, utilization: float, used: int | float, total: int | float
    ) -> bool:
        return (
            _valid_temperature(temperature)
            and _valid_utilization(utilization)
            and total > 0
            and used >= 0
            and used <= total
        )

    def _remember(self, sampled_at: float, sampled_monotonic: float) -> None:
        self._last_sampled_at = sampled_at
        self._last_sampled_monotonic = sampled_monotonic

    def _error_metric(
        self,
        sampled_at: float,
        sampled_monotonic: float,
        error: str,
        *,
        status: str = STATUS_ERROR,
    ) -> GpuMetrics:
        return GpuMetrics(
            status=status,
            sampled_at=self._last_sampled_at,
            sampled_monotonic=self._last_sampled_monotonic,
            error=error,
        )


__all__ = ["GpuReader"]
