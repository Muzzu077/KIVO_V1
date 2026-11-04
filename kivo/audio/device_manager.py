"""Audio input device management for KIVO.

Discovers, filters, selects, and resolves microphone devices using sounddevice.
Stores selection by persistent device name, never by unstable integer indices.
"""
from __future__ import annotations

import threading
from typing import Optional

from kivo.audio.audio_config import get_selected_device, save_selected_device
from kivo.audio.audio_models import AudioDevice

# Devices and host APIs that are pseudo-devices, monitor sinks or loopbacks
_PSEUDO_SUBSTRINGS = (
    "sound mapper",
    "primary sound",
    "sysdefault",
    "dmix",
    "dsnoop",
    "surround",
    "samplerate",
    "speexrate",
    "upmix",
    "vdownmix",
    "null",
)


class AudioDeviceManager:
    """Discovers, enumerates, and tracks audio input devices."""

    def __init__(self, config_getter=get_selected_device, config_setter=save_selected_device):
        self._config_getter = config_getter
        self._config_setter = config_setter
        self._lock = threading.Lock()
        self._cached_devices: list[AudioDevice] = []
        self._selected_device_name: str = self._config_getter()

    def list_devices(self, refresh: bool = False) -> list[AudioDevice]:
        """Return all discoverable audio input devices."""
        with self._lock:
            if self._cached_devices and not refresh:
                return list(self._cached_devices)

            devices = self._query_devices()
            self._cached_devices = devices
            return list(devices)

    def _query_devices(self) -> list[AudioDevice]:
        """Query host audio devices via sounddevice and filter for input."""
        try:
            import sounddevice as sd
        except ImportError:
            return []

        try:
            raw_devices = list(sd.query_devices())
            hostapis = list(sd.query_hostapis())
        except Exception:
            return []

        default_input_idx = None
        try:
            default_tuple = sd.default.device
            if isinstance(default_tuple, (list, tuple)) and len(default_tuple) > 0:
                default_input_idx = default_tuple[0]
            elif isinstance(default_tuple, int):
                default_input_idx = default_tuple
        except Exception:
            pass

        discovered: list[AudioDevice] = []
        seen_names: set[str] = set()

        for idx, dev in enumerate(raw_devices):
            if not isinstance(dev, dict):
                continue
            name = (dev.get("name") or "").strip()
            max_in = dev.get("max_input_channels", 0)
            if not name or max_in <= 0:
                continue

            # Filter out known ALSA/Pulse pseudo devices
            low_name = name.lower()
            if any(pseudo in low_name for pseudo in _PSEUDO_SUBSTRINGS):
                # Special case: "pipewire" or "pulse" might be present as default device
                if low_name not in ("pipewire", "pulse"):
                    continue

            # Deduplicate by name
            if name in seen_names:
                continue
            seen_names.add(name)

            hostapi_idx = dev.get("hostapi", -1)
            hostapi_name = "Unknown"
            if 0 <= hostapi_idx < len(hostapis):
                hostapi_name = hostapis[hostapi_idx].get("name", "Unknown")

            is_def = (idx == default_input_idx)
            samplerate = float(dev.get("default_samplerate", 16000.0) or 16000.0)

            discovered.append(
                AudioDevice(
                    name=name,
                    hostapi=hostapi_name,
                    max_input_channels=max_in,
                    default_samplerate=samplerate,
                    is_default=is_def,
                    is_available=True,
                    index=idx,
                )
            )

        return discovered

    def get_default_device(self) -> Optional[AudioDevice]:
        """Return the system default input device, or the first available."""
        devices = self.list_devices()
        for dev in devices:
            if dev.is_default:
                return dev
        return devices[0] if devices else None

    def get_selected_device_name(self) -> str:
        """Return the currently selected device name."""
        with self._lock:
            return self._selected_device_name

    def get_selected_device(self) -> Optional[AudioDevice]:
        """Return the AudioDevice for the current selection.

        If configured device is empty string or not found, falls back to default.
        """
        devices = self.list_devices()
        with self._lock:
            chosen_name = self._selected_device_name or self._config_getter()

        if chosen_name:
            for dev in devices:
                if dev.name == chosen_name:
                    return dev
            # Device configured but not found: return an unavailable placeholder
            return AudioDevice(
                name=chosen_name,
                hostapi="Unknown",
                max_input_channels=0,
                default_samplerate=16000.0,
                is_default=False,
                is_available=False,
                index=None,
            )

        return self.get_default_device()

    def select_device(self, name: str) -> AudioDevice:
        """Select a microphone by name and persist the choice.

        Empty string or "default" resets to the system default.
        Raises ValueError if a specific non-empty device name is not found.
        """
        clean_name = (name or "").strip()
        if clean_name.lower() in ("", "default", "system default"):
            with self._lock:
                self._selected_device_name = ""
                self._config_setter("")
            default_dev = self.get_default_device()
            if default_dev is None:
                return AudioDevice(
                    name="Default",
                    hostapi="Unknown",
                    max_input_channels=0,
                    default_samplerate=16000.0,
                    is_default=True,
                    is_available=False,
                    index=None,
                )
            return default_dev

        devices = self.list_devices(refresh=True)
        match = next((d for d in devices if d.name == clean_name), None)
        if match is None:
            raise ValueError(f"Microphone device '{clean_name}' was not found.")

        with self._lock:
            self._selected_device_name = clean_name
            self._config_setter(clean_name)

        return match

    def resolve_device_index(self, name: Optional[str] = None) -> Optional[int]:
        """Resolve a device name to a live sounddevice index at runtime.

        Returns None to use sounddevice's default device.
        """
        target = name if name is not None else self.get_selected_device_name()
        if not target or target.lower() in ("default", "system default"):
            return None

        try:
            import sounddevice as sd
            raw_devices = list(sd.query_devices())
            for idx, dev in enumerate(raw_devices):
                if dev.get("max_input_channels", 0) > 0:
                    if dev.get("name", "").strip() == target:
                        return idx
        except Exception:
            pass

        return None
