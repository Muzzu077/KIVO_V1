# KIVO — Stage 12.2 Runtime Inventory

```
Document:   Stage 12.2.0 runtime inventory
Status:     Research complete; specification input
Date:       2026-11-09
Host:       globy — Arch Linux, Wayland, niri
Baseline:   HEAD 1062c0d (Stage 12.1.2 media work and Stage 11.3 work
            remain uncommitted in the working tree)
Audit mode: Read-only. No package installed, no service enabled or restarted,
            no PipeWire/WirePlumber configuration changed, no microphone
            state changed, no application code or QML modified.
```

This document records only what was observed on the target machine. Where a
statement is an inference rather than an observation, it is marked
**Assumption** or **Design decision**. Values that change continuously (CPU
load, temperatures, GPU utilization) are single samples and are labelled with
the sample time where useful.

---

## 1. Executive summary

The host provides every source Stage 12.2 needs for its initial scope:

- `/proc/stat`, `/proc/meminfo`, CPU frequency sysfs, hwmon, thermal zones,
  power-supply sysfs, and `statvfs()` are all readable without privileges.
- NVIDIA telemetry works through both NVML (`pynvml`) and a single batched
  `nvidia-smi` query. NVML is fast enough for 2 s polling; `nvidia-smi` is a
  viable but slower fallback.
- PipeWire 1.6.10 and WirePlumber 0.5.18 are active. `wpctl` can list audio
  sources and read the default source's volume/mute line. There is exactly one
  WirePlumber audio source on this host: the physical ALSA digital microphone
  (node ID 85). Four PulseAudio-compatible monitor sources exist but are not
  listed by `wpctl list audio sources`.
- KIVO already has the lifecycle, IPC server, handler surface, QML bridge,
  notch, dashboard, theme, and test patterns needed to add both services
  without a second backend or a competing event loop.

Two findings materially shape the design:

1. **`wpctl get-volume` exits 0 for a missing node.** It prints an empty
   stdout and writes `Node '…' not found` to stderr. A successful exit code is
   therefore not evidence of success. Parsing and post-operation verification
   are mandatory.
2. **KIVO Push-to-Talk uses `sounddevice`, not the WirePlumber default-source
   node directly.** Depending on KIVO's selected input, `sounddevice` may
   follow the system default, so the mute may or may not affect PTT. Muting
   the WirePlumber default source must therefore not be described as
   disabling KIVO's own PTT capture. The feature is a default-source mute
   control, not a PTT privacy gate.

The immediate Stage 12.2.1 telemetry work is unblocked. The microphone work is
ready but carries the default-source-only limitation and the PTT-path
divergence described in this document.

---

## 2. Host and session

| Property | Observed value | Source |
|---|---|---|
| Distribution | Arch Linux (rolling) | `/etc/os-release` |
| Kernel | `7.2.14-arch1-1` | `uname -a` |
| Architecture | `x86_64` | `uname -a` |
| Session type | Wayland | `XDG_SESSION_TYPE` |
| Desktop | niri | `XDG_CURRENT_DESKTOP` |
| Wayland display | `wayland-1` | `WAYLAND_DISPLAY` |
| Runtime directory | `/run/user/1000` | `XDG_RUNTIME_DIR` |
| Python | 3.14.9 (`.venv`) | `.venv/bin/python --version` |
| Quickshell | 0.3.2 (Arch package) | `qs --version` |
| Qt QML linter | `qmllint 6.12.0` | `/usr/lib/qt6/bin/qmllint --version` |

These values are environment facts, not KIVO configuration. KIVO must not
hardcode the UID, the username, the hostname, or `wayland-1`; it already uses
XDG paths and `Path.home()`.

---

## 3. Audio stack

### 3.1 Versions and service state

| Component | Observed value |
|---|---|
| PipeWire | 1.6.10 (compiled and linked) |
| WirePlumber | 0.5.18 (compiled and linked with libwireplumber 0.5.18) |
| PulseAudio compatibility server | “PulseAudio (on PipeWire 1.6.10)”, protocol 35 |
| `wireplumber.service` | `active (running)` since 2026-11-09 11:38:51 IST, PID 2902 |
| `pipewire` | active (PulseAudio compatibility socket available) |
| `wpctl` | `/usr/bin/wpctl`, 76,080 bytes, root-owned, executable |
| `pactl` | `/usr/bin/pactl`, 92,336 bytes, root-owned, executable |
| `wpctl --version` | **Not supported.** `wpctl` exits with “Unknown option --version”. Version must be read from `wireplumber --version`. |

### 3.2 Default source and capture topology

`pactl info` reports:

```
Default Source: alsa_input.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__Mic1__source
```

`wpctl status` reports one audio device and one source:

```
Audio
 ├─ Devices:
 │      76. 700 Series Chipset Family HD Audio  [alsa]
 ├─ Sinks:
 │      81. … HDMI / DisplayPort 3 Output (Stereo) [vol: 1.00]
 │      82. … HDMI / DisplayPort 2 Output (Stereo) [vol: 1.00]
 │      83. … HDMI / DisplayPort 1 Output (Stereo) [vol: 1.00]
 │  *   84. … Speaker [vol: 1.00]
 ├─ Sources:
 │  *   85. … Digital Microphone [vol: 0.38]
```

`wpctl list audio sources` returns exactly one line (tabs shown as `\t`):

```
85\talsa_input.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__Mic1__source\taudio/source\t*
```

Parsed identity:

| Field | Value |
|---|---|
| Node ID | 85 |
| Node name | `alsa_input.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__Mic1__source` |
| Media class | `Audio/Source` |
| Node description | `700 Series Chipset Family HD Audio Digital Microphone` |
| ALSA card | `sof-hda-dsp` (Realtek ALC245 codec, `snd_soc_skl_hda_dsp`) |
| Default marker | `*` on node 85 |
| Volume | `0.38` |

`pactl list short sources` reports five capture sources:

```
81  …HDMI3__sink.monitor       (monitor)
82  …HDMI2__sink.monitor       (monitor)
83  …HDMI1__sink.monitor       (monitor)
84  …Speaker__sink.monitor     (monitor)
85  …HiFi__Mic1__source        (physical microphone, default)
```

The four monitor sources are not listed by `wpctl list audio sources`. They
capture sink output, not a microphone. The initial Stage 12.2 feature controls
only the WirePlumber default audio source (node 85 on this host). It does not
mute monitor sources, virtual sources, application streams, or capture nodes
used directly by an application.

### 3.3 Mute-state read semantics

`wpctl get-volume @DEFAULT_AUDIO_SOURCE@` returned:

```
Volume: 0.38
```

No `[MUTED]` marker was present. The `wpctl` documentation defines the muted
form as the same line with a trailing `[MUTED]` marker. No muted node existed
on this host during the audit, and changing microphone state was prohibited,
so the muted form was **not empirically observed**. It must be verified on
hardware, with user consent, in Stage 12.2.2.

`wpctl inspect @DEFAULT_AUDIO_SOURCE@` did **not** contain a `mute` property
while the source was unmuted. Therefore `wpctl inspect` cannot be used as the
primary mute-state reader. The `get-volume` marker is the available
WirePlumber-native signal.

Adversarial observation:

```
$ wpctl get-volume 99999
exit=0
stdout=(empty)
stderr=Node '99999' not found
```

A zero exit code does not mean the node was found or that a state was read.
The microphone design must require:

1. a zero exit code,
2. non-empty, parseable stdout,
3. an explicit post-operation read that verifies the requested state.

### 3.4 Stable command latency (measured, read-only)

Median of 5–50 runs on this host:

| Command | Median | Notes |
|---|---|---|
| `wpctl get-volume @DEFAULT_AUDIO_SOURCE@` | 15.5 ms | Connects to PipeWire each call |
| `wpctl list audio sources` | 7.5 ms | Returns one line |
| `nvidia-smi --query-gpu=…` (5 fields) | 16.0 ms | One batched process |
| NVML temperature + utilization + memory | 0.030 ms | In-process, after `nvmlInit()` |

These measurements drive the sampling and polling intervals in the
architecture specification. They are not universal constants and must be
re-measured after implementation.

---

## 4. Standard hardware telemetry sources

### 4.1 CPU

| Property | Observed value |
|---|---|
| Model | `13th Gen Intel(R) Core(TM) i7-13700HX` |
| Logical CPUs | 24 (`nproc`) |
| `/proc/stat` | Present and readable; aggregate `cpu` line plus `cpu0`…`cpu23` |
| Frequency sysfs | `/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq` present |
| Observed frequencies | cpu0 3,204,198 kHz; cpu1 800,000 kHz; cpu8 3,364,131 kHz; cpu16 2,494,140 kHz; cpu23 2,461,171 kHz |
| `cpuinfo_max_freq` | 4,800,000 kHz |
| `cpuinfo_min_freq` | 800,000 kHz |
| `cpuinfo_avg_freq` | Present (798,415 kHz in one sample, inconsistent with the per-core sample; not used) |
| `psutil.cpu_freq()` | 1,850.65 MHz current, 800 MHz min, 4,466.67 MHz max |

`psutil` reports a different maximum (4,466.67 MHz) than sysfs (4,800 MHz).
The implementation should read sysfs directly for a transparent, consistent
value rather than mixing sources.

`/proc/stat` aggregate sample:

```
cpu  64702 177 27717 3776151 22265 7412 5187 0 0 0
```

The first eight fields are `user nice system idle iowait irq softirq steal`.
`guest` and `guest_nice` are already included in `user`/`nice` and must not be
double-counted. CPU utilization is a delta calculation; the first sample and
any counter reset have no valid percentage.

Measured read cost:

| Read | Median |
|---|---|
| `/proc/stat` first line | 0.018 ms |
| All 24 `scaling_cur_freq` files | 0.234 ms |

### 4.2 Memory

`/proc/meminfo` sample:

```
MemTotal:       16076116 kB
MemFree:         7773488 kB
MemAvailable:   10476272 kB
Buffers:          212320 kB
Cached:          3597716 kB
SwapTotal:       4194300 kB
SwapFree:        4194300 kB
```

`MemTotal` ≈ 16.08 GiB; `MemAvailable` ≈ 9.99 GiB at the sample. `MemAvailable`
is the kernel's estimate of memory available for new workloads and is the
correct field for a user-facing “used” value:

```
used = MemTotal - MemAvailable
percent = used / MemTotal * 100
```

Read cost: 0.005 ms.

### 4.3 CPU temperature and thermal sensors

`/sys/class/hwmon` contains:

| hwmon | Name | Relevant files and sample values |
|---|---|---|
| `hwmon0` | `ACAD` | No temperature files |
| `hwmon1` | `acpitz` | `temp1_input=54000` (54 °C) |
| `hwmon2` | `BAT1` | No temperature files |
| `hwmon3` | `nvme` | `temp1_input=39850` (`Composite`), `temp2_input=40850` (`Sensor 1`) |
| `hwmon4` | `acer` | `temp1_input=54000`, `temp2_input=47000`, `temp3_input=51000` — **unlabelled chassis sensors** |
| `hwmon5` | `spd5118` | `temp1_input=49500` — unlabelled (memory module) |
| `hwmon6` | `coretemp` | `temp1_input=55000` (`Package id 0`), plus per-core `temp2`…`temp41` with labels such as `Core 0`, `Core 4`, `Core 8` |
| `hwmon7` | `iwlwifi_1` | `temp1_input=44000` (44 °C) |

`/sys/class/thermal` contains ten zones:

| Zone | Type | Sample |
|---|---|---|
| `thermal_zone0` | `acpitz` | 50.000 °C |
| `thermal_zone1` | `INT3400 Thermal` | 20.000 °C |
| `thermal_zone2` | `SEN2` | 47.050 °C |
| `thermal_zone3` | `SEN3` | 52.050 °C |
| `thermal_zone4` | `SEN4` | 44.050 °C |
| `thermal_zone5` | `SEN5` | 43.050 °C |
| `thermal_zone6` | `TCPU` | 50.050 °C |
| `thermal_zone7` | `TCPU_PCI` | 53.000 °C |
| `thermal_zone8` | `x86_pkg_temp` | 51.000 °C |
| `thermal_zone9` | `iwlwifi_1` | 45.000 °C |

`psutil.sensors_temperatures()` exposes groups `acpitz`, `nvme`, `acer`,
`spd5118`, `coretemp`, `iwlwifi_1`; it does not expose the ACPI thermal-zone
names directly. The implementation should not treat every sensor as “CPU
temperature”. A deterministic selection is required:

- CPU package: `coretemp` / `Package id 0` first, then thermal zone
  `x86_pkg_temp`, then `TCPU`/`TCPU_PCI`, then `acpitz`.
- NVMe: `nvme` / `Composite`.
- Wi-Fi: `iwlwifi_1` (hwmon or thermal zone).
- Unlabelled `acer` and `spd5118` sensors are excluded from the initial
  contract. They cannot be named accurately from the available labels.

A full `hwmon*/temp*_input` scan measured 8.95 ms. Reading a cached set of
three targeted paths measured 2.2 ms. Discovery must be cached; it must not
scan every sensor on every poll.

### 4.4 Battery and AC

`/sys/class/power_supply` contains `ACAD` and `BAT1`.

| Field | Observed value |
|---|---|
| `BAT1/type` | `Battery` |
| `BAT1/present` | `1` |
| `BAT1/status` | `Discharging` |
| `BAT1/capacity` | `40` |
| `BAT1/capacity_level` | `Normal` |
| `BAT1/charge_now` | `1,900,000` µAh |
| `BAT1/charge_full` | `4,804,000` µAh |
| `BAT1/charge_full_design` | `5,845,000` µAh |
| `BAT1/voltage_now` | `14,706,000` µV |
| `BAT1/current_now` | `2,154,000` µA |
| `BAT1/cycle_count` | `672` |
| `BAT1/model_name` | `AP21A7T` |
| `BAT1/manufacturer` | `SIMPLO` |
| `BAT1/serial_number` | Present |
| `ACAD/type` | `Mains` |
| `ACAD/online` | `0` |

This battery exposes `charge_*` fields, not `energy_*` fields. The parser must
support both naming schemes and prefer `capacity` when present. Battery model,
manufacturer, serial number, and cycle count are hardware-identifying and must
not appear in the telemetry snapshot. `psutil.sensors_battery()` also works
(`percent=39.22`, `power_plugged=False`) but is not needed.

Read cost: 0.072 ms.

### 4.5 Storage

| Property | Value |
|---|---|
| Root source | `/dev/nvme0n1p8` |
| Filesystem | `ext4` |
| Mount options | `rw,relatime` |
| Total | 157,031,038,976 bytes |
| Used | 128,473,645,056 bytes |
| Available | 20,506,062,848 bytes |
| Use | 87% |
| `os.statvfs("/")` | `f_blocks=38,337,656`, `f_bfree=6,972,020`, `f_bavail=5,006,363`, `f_frsize=4,096` |

`statvfs` on `/` measured 0.001 ms. No external utility is required. Storage
capacity should be read with `os.statvfs`, not `df`, to avoid a subprocess.

### 4.6 GPU

| Property | Observed value |
|---|---|
| GPU | `NVIDIA GeForce RTX 4050 Laptop GPU` |
| `nvidia-smi` | `/usr/bin/nvidia-smi`, executable, root-owned |
| NVIDIA-SMI version | 615.78.08 |
| NVML version | 615.78 |
| KMD version | 615.78.08 |
| CUDA UMD version | 13.4 |
| Device nodes | `/dev/nvidia0`, `/dev/nvidiactl`, `/dev/nvidia-modeset`, `/dev/nvidia-uvm`, `/dev/nvidia-uvm-tools` |
| NVML library | `/usr/lib/libnvidia-ml.so.1` |
| NVML device count | 1 |

Batched `nvidia-smi` query:

```
$ nvidia-smi --query-gpu=timestamp,name,driver_version,temperature.gpu,utilization.gpu,utilization.memory,memory.used,memory.total,power.draw,clocks.current.graphics,clocks.current.memory --format=csv,noheader,nounits
2026/11/09 12:05:01.628, NVIDIA GeForce RTX 4050 Laptop GPU, 615.78.08, 46, 6, 0, 13, 6141, 590.01, 2355, 7001
```

A shorter query used by the proposed fallback:

```
$ nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits
NVIDIA GeForce RTX 4050 Laptop GPU, 44, 6, 13, 6141
```

NVML through `pynvml` returned the same device:

```
count 1
name NVIDIA GeForce RTX 4050 Laptop GPU
temp 45
mem 314,966,016 used / 6,439,305,216 total
util 0 / 0
power 6041 mW
```

Notes:

- `memory.used`/`memory.total` from `nvidia-smi` are MiB; NVML reports bytes.
  The contract normalizes everything to bytes.
- The two sources are not guaranteed to agree byte-for-byte. In this audit
  `nvidia-smi` reported 13 MiB used while NVML reported 314,966,016 bytes
  (about 300 MiB) at a different moment. The service must choose one source
  per snapshot, label it in `source`, and never mix fields from both. The
  example snapshot uses NVML values consistently.
- `power.draw` reported `590.01` by `nvidia-smi` while NVML reported
  `6041 mW`. The unit/scale is not consistent enough to trust without
  verification. GPU power is excluded from the Stage 12.2 contract.
- `utilization.memory` is not memory capacity usage; it must not be displayed
  as “VRAM %”. Capacity percentage is `memory.used / memory.total`.
- There is one GPU. The initial contract reports device 0 only; multi-GPU
  support is a later extension.
- NVML query latency measured 0.030 ms. `nvidia-smi` measured 16.0 ms. NVML
  is the primary source; `nvidia-smi` is a fallback only.

---

## 5. Python environment and dependency declarations

Installed in `.venv`:

| Package | Version | Relevance |
|---|---|---|
| `psutil` | 7.2.2 | Works, but direct `/proc`/`/sys` reads are preferred for transparency and testability |
| `pynvml` | 13.0.1 | Deprecated shim; import emits a `FutureWarning` |
| `nvidia-ml-py` | 13.615.71 | The maintained provider of the `pynvml` module |
| `sensors` (PyPI) | Not installed | Not needed; `sensors` CLI 3.6.2 exists but would add a subprocess |
| `dbus` / `jeepney` | Not installed | Not needed for the initial design |
| `GPUtil` / `py3nvml` | Not installed | Not needed |

`requirements.txt` already declares `psutil>=5.9,<8` and `pynvml` (comment:
“system_monitor + the HUD's GPU meter (NVIDIA)”). No new dependency is needed
for Stage 12.2. The `pynvml` distribution is deprecated in favour of
`nvidia-ml-py`; a dependency cleanup can be scheduled separately, but it is
not required for this stage and must not be bundled into a security-sensitive
change.

The `pynvml` import must be optional. A missing module, missing library, or
failed `nvmlInit()` must degrade GPU telemetry only; it must never abort
backend startup.

---

## 6. Existing KIVO components and reuse opportunities

| Component | Path | Reuse decision | Constraint |
|---|---|---|---|
| `KivoApp` lifecycle | `kivo/app.py` | Add two components to `_components`; start before IPC; stop before IPC | Do not reorder or break existing initialization steps; progress labels are cosmetic; failures must not abort startup |
| `IPCHandlers` | `kivo/ipc/handlers.py` | Add `telemetry_*` and `mic_*` methods; accept optional services | Existing constructor calls in tests must keep working; return degraded shapes when services are absent |
| `IPCServer` | `kivo/ipc/server.py` | Reuse unchanged: 0600 socket, per-client queues, subscriptions, sequence | No new listener, no TCP, no second backend |
| Protocol allowlists | `kivo/ipc/protocol.py` | Add operations, events, and strict parameter validators | 64 KiB frame limit; no arbitrary command/path parameters |
| `RuntimeState` | `kivo/runtime_state.py` | Leave assistant/audio/agent state unchanged | Do not overload `audio_state` with system mute; `audio_state` describes KIVO's capture pipeline |
| `MediaService` | `kivo/media/service.py` | Reuse the dedicated-thread and bounded-subprocess *pattern* | Do not reuse the MPRIS logic; do not introduce a second asyncio loop for telemetry |
| `AudioDeviceManager` | `kivo/audio/device_manager.py` | Read-only reuse for showing KIVO's selected input | Resolves devices through `sounddevice`; when no explicit selection exists it may follow the system default, so the mute must not claim to gate PTT |
| `AudioCaptureManager` | `kivo/audio/capture_manager.py` | **Not used** by mic mute | No recording, no samples, no stream access |
| `KivoIpc.qml` | `shell/services/KivoIpc.qml` | Extend with telemetry/mic properties, requests, and event cases | Sole bridge; request IDs, sequence checks, reconnect behaviour stay intact |
| `DynamicNotch.qml` | `shell/components/DynamicNotch.qml` | Add compact chip and expanded summary/control | Preserve media drawer, expansion, transient states, and existing widths |
| `DashboardOverview.qml` | `shell/components/DashboardOverview.qml` | Host detailed telemetry cards | Keep existing status/task/agent content |
| `DashboardAudio.qml` | `shell/components/DashboardAudio.qml` | Host the default-microphone control | Keep device list, STT, and capture status intact |
| `Theme.qml` | `shell/styles/Theme.qml` | Use existing palette, fonts, radii, durations | No new visual language |
| `Media` tests | `tests/test_media_ipc.py`, `tests/test_media_service.py` | Reuse fake-socket, fake-subprocess, and event-delivery patterns | New tests must be deterministic and never touch the live socket |
| Shell tests | `shell/tests/test_notch.py` | Reuse fake backend and `qmllint` harness | Integration tests skip without Wayland/niri |
| Health check | `scripts/desktop-health.sh` | Keep 9/9 PASS | Adding a probe would change the documented count; defer to a later integration stage |
| `actions/system_monitor.py` | `actions/system_monitor.py` | **Reference only** | Blocking `psutil` intervals, agent-facing summary, chassis sensors, GPU power units; not suitable for ambient polling |
| `kivo/agent/tools/system_info.py` | `kivo/agent/tools/system_info.py` | **Reference only** | Agent tool with `psutil.cpu_percent(interval=0.1)`; not an IPC telemetry source |
| `BoundedFileWorker` / `os.fstatvfs` | `kivo/files/service.py` | Pattern reference for `statvfs` error handling | Do not couple file intelligence to telemetry |
| `requirements.txt` | `requirements.txt` | No change | `psutil` and `pynvml` already declared |

### Why the existing agent utilities are not reused

`actions/system_monitor.py`:

- calls `psutil.cpu_percent(interval=…)`, which blocks the calling thread;
- returns a human-readable `summary` intended for model prompts;
- includes chassis/GPU/battery logic with platform branches;
- may use `nvidia-smi` and `pynvml` with different unit assumptions;
- has no per-metric freshness/status model.

`kivo/agent/tools/system_info.py`:

- is registered as an agent tool and returns a model-facing overview;
- uses `psutil` convenience calls rather than explicit counter deltas;
- does not expose typed availability or staleness;
- must remain an agent tool, not become an IPC service.

Using either directly for ambient polling would mix agent and UI concerns,
block a thread on interval-based calls, and make deterministic testing harder.

---

## 7. What is missing

The repository currently has **no**:

- `kivo/telemetry/` module or telemetry service; the existing
  `tests/test_system_telemetry.py` covers only the legacy agent-facing
  `actions/system_monitor.py`, not a telemetry service;
- `kivo/microphone/` module, WirePlumber mute service, or microphone tests;
- telemetry or microphone operations in `REQUEST_OPERATIONS`;
- telemetry or microphone events in `EVENT_NAMES`;
- telemetry or microphone properties/functions in `KivoIpc.qml`;
- compact telemetry indicators or expanded telemetry section in the notch;
- default-microphone control in `DashboardAudio.qml`;
- per-metric availability, staleness, or “unsupported vs unavailable” model;
- bounded subprocess helper shared by media/mic/GPU fallback;
- tests for `/proc`/`/sys` parsing, NVML fallback, `wpctl` parsing, mute
  verification, rapid requests, or shutdown races.

The existing `get_status` payload intentionally excludes telemetry and system
mute. Stage 12.2 should keep it that way: a 1 Hz telemetry snapshot does not
belong in every status response.

---

## 8. Verified facts vs assumptions vs design decisions

### Verified facts

- The host is Arch Linux on Wayland/niri with Python 3.14.9 and Quickshell
  0.3.2.
- PipeWire 1.6.10 and WirePlumber 0.5.18 are active.
- `wpctl` exists at `/usr/bin/wpctl`; `wpctl --version` is unsupported.
- The default WirePlumber audio source is node 85, the physical ALSA digital
  microphone.
- `wpctl list audio sources` lists one source; `pactl` additionally reports
  four monitor sources.
- The default source was unmuted and at volume 0.38 during the audit.
- `wpctl get-volume 99999` exits 0 with empty stdout and a stderr message.
- `/proc/stat`, `/proc/meminfo`, CPU frequency sysfs, hwmon, thermal zones,
  power-supply sysfs, and `statvfs("/")` are readable without privileges.
- The CPU is an i7-13700HX with 24 logical CPUs; the GPU is an RTX 4050
  Laptop GPU with one NVML device.
- NVML and batched `nvidia-smi` both return GPU telemetry.
- Battery BAT1 uses `charge_*` fields and reports 40%, discharging, AC offline.
- KIVO's PTT path uses `sounddevice`/`AudioDeviceManager`; it is not the
  WirePlumber default-source node itself and may follow the system default
  when KIVO has no explicit input selection.
- The KIVO socket is `srw-------` in a `drwx------` runtime directory; desktop
  health is 9/9 PASS.
- No application code, QML, service configuration, or microphone state was
  changed during this audit.

### Assumptions

- `wpctl get-volume` appends `[MUTED]` to the same `Volume: …` line when a
  node is muted. This is documented by WirePlumber but was not observed here.
- Setting a source node's mute property causes consumers of that node to
  receive silence. KIVO does not verify samples and must not claim this.
- `nvidia-smi` memory fields are MiB and can be converted to bytes. NVML's
  total matched the MiB value exactly on this host.
- Polling `wpctl` every 5 s and reading NVML every 2 s is acceptable overhead
  on this hardware; the implementation must measure it.

### Design decisions already fixed by this specification

- The microphone feature is named **default microphone mute**, not system-wide
  mute.
- Telemetry is read-only, uses `/proc`/`/sys` first, and never exposes
  per-process information.
- NVML is the primary GPU source; `nvidia-smi` is a bounded fallback.
- GPU power is excluded because its unit/scale was not consistent.
- No new Python dependency is introduced.
- No microphone operation is registered as an agent tool.
- No optimistic UI state is allowed.

---

## 9. Inventory conclusion

The environment is capable, and the existing KIVO architecture has the right
seams. Stage 12.2.1 (telemetry) can begin immediately with standard-library
readers and an optional NVML provider. Stage 12.2.2 (microphone) can begin
after the service contract in the architecture specification is accepted, with
the default-source scope and PTT-path caveat treated as product requirements,
not implementation details to be smoothed over later.
