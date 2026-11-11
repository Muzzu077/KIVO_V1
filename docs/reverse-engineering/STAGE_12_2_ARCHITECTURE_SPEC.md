# KIVO — Stage 12.2 System Telemetry & Default Microphone Mute Architecture

```
Document:   Stage 12.2.0 architecture specification
Status:     Proposed; implementation has not started
Date:       2026-11-09
Baseline:   HEAD 1062c0d; Stage 11.3 and Stage 12.1.2 work uncommitted
Scope:      Read-only hardware telemetry; WirePlumber default microphone mute;
            typed IPC; Dynamic Notch and Dashboard integration
Non-goals:  System-wide mute of every capture source; recording; audio capture;
            AI/agent access; PipeWire configuration changes; new dependencies
```

This specification is normative for Stages 12.2.1–12.2.3 unless a later stage
explicitly replaces it. It is written against the facts in
`STAGE_12_2_RUNTIME_INVENTORY.md`.

---

## 1. Invariants

1. **One backend, one IPC server, one QML bridge.** `KivoApp`, `IPCServer`,
   `KivoIpc.qml`, and the existing service lifecycle remain the only paths.
2. **Telemetry is read-only.** No sysfs/procfs writes, no device configuration,
   no process inspection, no privilege escalation.
3. **Microphone control is default-source-only.** The feature never claims to
   mute monitors, virtual sources, application streams, or KIVO's own PTT path.
4. **No optimistic UI.** A muted state is displayed only after a verified
   backend read. A dispatched command is not a completed operation.
5. **No arbitrary execution.** No shell strings, no user-supplied command,
   path, sensor name, or source ID. All subprocesses use fixed argv arrays.
6. **Bounded work.** Every subprocess has a timeout, bounded stdout/stderr, and
   explicit termination. Every queue, collection, and frame is bounded.
7. **No new dependency.** Standard library plus the already-declared `pynvml`
   (provided by `nvidia-ml-py`).
8. **No agent surface.** Neither telemetry nor microphone control is registered
   as an agent tool or sent to a model provider in this stage.
9. **Graceful degradation.** Missing sensors, GPU tooling, WirePlumber, or
   `wpctl` degrade one capability; they never abort backend startup.
10. **Existing behaviour is preserved.** Media playback, lyrics, PTT, task
    cancellation, notch expansion, dashboard navigation, and desktop health
    must remain intact.

---

## 2. Component architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│ KivoApp (kivo/app.py)                                                │
│  owns: telemetry_service, microphone_service, ipc server, runtime    │
│  callbacks publish typed events through IPCServer.publish()          │
└───────────────┬───────────────────────────────┬──────────────────────┘
                │                               │
   ┌────────────▼─────────────┐     ┌───────────▼──────────────────────┐
   │ TelemetryService         │     │ MicrophoneService                │
   │ one daemon thread        │     │ one daemon poll thread           │
   │ /proc, /sys, statvfs     │     │ wpctl list/get-volume/set-mute   │
   │ NVML → nvidia-smi        │     │ explicit 1/0 + verify            │
   │ immutable snapshots      │     │ serialized operations            │
   └────────────┬─────────────┘     └───────────┬──────────────────────┘
                │ snapshot callback              │ state callback
                ▼                               ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │ IPCHandlers (kivo/ipc/handlers.py)                               │
   │  telemetry_get_snapshot / mic_get_state / mic_set_mute           │
   └────────────┬─────────────────────────────────────────────────────┘
                │ existing protocol v1, 0600 Unix socket, 64 KiB frames
                ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │ KivoIpc.qml (sole bridge) → DynamicNotch / Dashboard             │
   │  compact chip, mic badge, expanded summary, dashboard cards      │
   └──────────────────────────────────────────────────────────────────┘
```

No component below the QML bridge talks to the socket directly. No service
opens a second socket, event loop, or backend process.

### 2.1 Module layout

```
kivo/
  core/
    bounded_process.py        # shared bounded argv runner (new)
  telemetry/
    __init__.py
    model.py                  # frozen dataclasses + JSON serialization
    readers.py                # /proc, /sys, statvfs readers
    gpu.py                    # NVML primary, nvidia-smi fallback
    service.py                # sampling thread + lifecycle
  microphone/
    __init__.py
    model.py                  # MicState, MicSource, MicResult
    wpctl.py                  # fixed-argv runner + strict parsers
    service.py                # poll thread, set/verify, serialization
```

`kivo/core/bounded_process.py` is new and shared by the GPU fallback and the
microphone runner. `MediaService` is **not** refactored in this stage; it keeps
its proven internal runner. A later cleanup may make it delegate to the shared
helper after the new code is stable.

---

## 3. TelemetryService

### 3.1 Public API

```python
class TelemetryService:
    def __init__(
        self,
        on_snapshot: Callable[[TelemetrySnapshot], None] | None = None,
        *,
        paths: TelemetryPaths | None = None,
        gpu_provider: GpuProvider | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None: ...

    def start(self) -> None: ...
    def close(self, timeout: float = 2.0) -> None: ...
    def latest_snapshot(self) -> TelemetrySnapshot: ...
```

- `start()` is idempotent. It primes CPU counters, discovers sensors, and
  starts one daemon thread named `kivo-telemetry`.
- `latest_snapshot()` performs **no I/O**. It returns the last immutable
  snapshot; if none exists, it returns a `starting` snapshot.
- `close()` sets a stop event, joins the thread with a timeout, and is
  idempotent. It does not write to sysfs or change hardware state.
- `on_snapshot` is called from the telemetry thread at most once per second.
  The callback must be fast and exception-safe; `KivoApp` wraps
  `IPCServer.publish` in its existing try/except.

### 3.2 Threading model

- Exactly one telemetry thread.
- A single `threading.Event` is used for shutdown and for sleeping between
  deadlines. No `asyncio` loop, no `Timer` per metric, no thread pool.
- Snapshot publication swaps an immutable dataclass reference; readers do not
  need a lock.
- The thread catches every per-cycle exception, records the affected metric as
  `error`, and continues after a 1 s backoff. A single bad sensor must not kill
  the service.
- If the thread stalls, `latest_snapshot()` still returns the old snapshot; the
  handler computes `age_seconds` and `stale` at serialization time. This is how
  a hung read becomes visible rather than silently “live”.

### 3.3 Sampling schedule

| Group | Interval | Primary source | Measured cost on host |
|---|---|---|---|
| CPU utilization | 1.0 s | `/proc/stat` first line | 0.018 ms |
| Memory | 1.0 s | `/proc/meminfo` | 0.005 ms |
| CPU frequency | 2.0 s | cached `scaling_cur_freq` paths | 0.234 ms |
| Temperatures | 3.0 s | cached hwmon paths | ~2.2 ms targeted |
| GPU | 2.0 s | NVML (0.030 ms); `nvidia-smi` fallback (~16 ms) | see left |
| Battery | 5.0 s | `/sys/class/power_supply` | 0.072 ms |
| Storage | 30.0 s | `os.statvfs("/")` | 0.001 ms |
| Sensor discovery | 60.0 s or on `ENOENT` | hwmon/thermal enumeration | ~9 ms full scan |
| Snapshot publish | 1.0 s | assembled from the freshest values | ~1 KB event |

The schedule is implemented as independent next-deadline values in one loop:

```
next_cpu = now + 1.0
next_freq = now + 2.0
next_temp = now + 3.0
...
wait(min(next_deadline) - now)
```

A slow group delays only the next iteration; it does not spawn a thread. If the
loop is late, the next deadlines are recomputed from the current monotonic
clock rather than accumulated, so the service does not busy-loop to catch up.

**Measured overhead target:** below 0.5 % of one logical CPU for standard
telemetry, and below 1.5 % worst case when the `nvidia-smi` fallback is active.
These are targets to verify in 12.2.1, not guarantees.

### 3.4 Readers and algorithms

#### CPU utilization

Read the aggregate `cpu` line from `/proc/stat`:

```
cpu  user nice system idle iowait irq softirq steal guest guest_nice
```

- Sum fields 0–7 only. `guest`/`guest_nice` are already included in
  `user`/`nice`; adding them double-counts.
- `total = user + nice + system + idle + iowait + irq + softirq + steal`
- `idle_total = idle + iowait`
- `delta_total = total - previous_total`
- `delta_idle = idle_total - previous_idle_total`
- `percent = 100 * (delta_total - delta_idle) / delta_total`
- Clamp to `[0.0, 100.0]`.

First sample: store counters and return `status="warming_up"`, `percent=null`.
Counter reset (`delta_total <= 0` or `total < previous_total`): replace the
baseline, return `warming_up` with `error="counter_reset"`. The next 1 s sample
recovers. Never emit a negative or >100 % value.

#### CPU frequency

- Discover `/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq` once.
- Bound discovery to 256 CPUs.
- Read each cached path; parse a positive integer in kHz.
- Average the successful values and convert to MHz (`kHz / 1000.0`).
- Read `cpuinfo_max_freq` once per discovery for `max_mhz`.
- If some CPUs fail, average the successes and set `status="ok"`; if none
  succeed, `status="unavailable"`. A partial read is not a total failure.
- Do not use `cpuinfo_avg_freq`; the observed value was inconsistent with the
  per-core sample.

#### Memory

- Parse `MemTotal` and `MemAvailable` from `/proc/meminfo` (kB → bytes ×1024).
- `used = max(0, total - available)`.
- `percent = used / total * 100` if `total > 0`, clamped to `[0, 100]`.
- If `MemAvailable` is missing, fall back to `MemFree + Buffers + Cached`
  only if all three are present; otherwise `status="unavailable"`.
- A valid `used=0` is `status="ok"`, `percent=0.0`. It is not “missing”.

#### CPU temperature and sensor discovery

Discovery (at start, every 60 s, or on `ENOENT`):

1. Enumerate `/sys/class/hwmon/hwmon*`, read `name`, and index temperature
   inputs with their labels. Bound to 64 hwmon devices and 128 temperature
   inputs total.
2. Enumerate `/sys/class/thermal/thermal_zone*`, read `type` and `temp`.
   Bound to 64 zones.
3. Select:
   - CPU: `coretemp` + label `Package id 0`; else `coretemp` first input;
     else thermal `x86_pkg_temp`; else `TCPU`; else `TCPU_PCI`; else `acpitz`.
   - NVMe: `nvme` + label `Composite`; else `nvme` first input.
   - Wi-Fi: hwmon `iwlwifi_1`; else thermal `iwlwifi_1`.
4. Store only the selected paths/labels. Do not scan every sensor on each
   temperature tick.
5. Exclude unlabelled `acer` and `spd5118` sensors from the v1 contract. They
   cannot be named accurately.

Temperature values are millidegrees Celsius; divide by 1000. Validate
`-40.0 <= celsius <= 150.0`. A value outside that range is `error`, not a
displayed temperature.

Fixed temperature IDs in v1:

| ID | Label | Required? |
|---|---|---|
| `cpu_package` | CPU Package | Core metric; `unsupported` if no sensor |
| `nvme_composite` | NVMe | Optional |
| `wifi` | Wi-Fi | Optional |

#### Battery

- Enumerate `/sys/class/power_supply/*`, bound to 32 entries.
- Battery: first entry with `type == "Battery"` whose `present` is missing
  or nonzero. A present `0` means no usable battery.
- AC: first entry with `type == "Mains"`; read `online`.
- Percent: `capacity` if present and 0–100; else `charge_now/charge_full`;
  else `energy_now/energy_full`; else `status="error"`.
- State normalization:
  - `Charging` → `charging`
  - `Discharging` → `discharging`
  - `Full` → `full`
  - `Not charging` → `not_charging`
  - anything else → `unknown`
- `ac_online`: `true`/`false`, or `null` if no Mains entry exists.
- No battery device → `status="unsupported"`.
- Battery present but unreadable → `status="unavailable"` or `error`.
- Never include serial number, model, manufacturer, or cycle count.

#### Storage

- Fixed mount `/`; no path parameter accepted over IPC.
- `os.statvfs("/")`:
  - `frsize = f_frsize or f_bsize`
  - `total = f_blocks * frsize`
  - `free = f_bfree * frsize`
  - `available = f_bavail * frsize`
  - `used = max(0, total - free)`
  - `percent = used / (used + available) * 100` when the denominator is > 0.
- This matches `df`'s Use% calculation (df rounds the displayed percentage
  up) and accounts for reserved blocks. `used + available` may be less than
  `total`.
- `total <= 0` or `OSError` → `status="error"`/`"unavailable"`, values null.

#### GPU

NVML is primary:

- Optional import of `pynvml`; suppress the deprecation `FutureWarning` but log
  once that the shim is in use.
- `nvmlInit()`, `nvmlDeviceGetCount()`. Zero devices → `unsupported`.
- Device 0 only. Cache the handle after initialization.
- Query in one batch:
  - `nvmlDeviceGetName(handle)` (decode bytes if needed; sanitize; cap 128)
  - `nvmlDeviceGetTemperature(handle, NVML_TEMPERATURE_GPU)`
  - `nvmlDeviceGetUtilizationRates(handle).gpu`
  - `nvmlDeviceGetMemoryInfo(handle).used/.total`
- Validate: temperature `-40..150 °C`, utilization `0..100`, `0 <= used <=
  total`, `total > 0`.
- Never query per-process information, running processes, or graphics
  processes.
- Never query power; the observed unit/scale was inconsistent.
- On a transient `NVMLError`, mark GPU `error` and retry the query on the next
  cycle. Re-run `nvmlInit()` at most once per 60 s after an initialization
  failure.
- On service close, call `nvmlShutdown()` only if this service initialized
  NVML. Document that NVML is process-global; if another component starts
  using it, introduce a shared refcount in a later stage.

`nvidia-smi` fallback:

- Enabled only when NVML import/initialization is unavailable at startup. Do
  not spawn it after a transient NVML query error, to avoid process churn.
- Resolve `shutil.which("nvidia-smi")` once at startup; require an absolute
  path and an executable regular file.
- Fixed argv:
  ```
  [path, "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
         "--format=csv,noheader,nounits"]
  ```
- `stdin=DEVNULL`, `stdout/stderr=PIPE`, `env` inherits the session plus
  `LC_ALL=C`, timeout 2.0 s, stdout cap 16 KiB, stderr cap 4 KiB.
- Parse the first non-empty line with `rsplit(",", 4)` so a GPU name containing
  a comma does not shift the numeric fields.
- `memory.used`/`memory.total` are MiB → bytes (`× 1024 × 1024`).
- Validate the same ranges. Nonzero exit, timeout, empty output, wrong field
  count, non-numeric values, or out-of-range values → `error`.
- One process per GPU cycle. Never one process per field.

#### Snapshot assembly

The 1 s publish uses the freshest value for each group. Groups sampled on
slower cadences keep their last successful value and timestamp. A failed slow
group does not invalidate CPU or memory.

### 3.5 Status, availability, and staleness model

Every metric group carries:

| Field | Type | Meaning |
|---|---|---|
| `status` | `ok \| warming_up \| unsupported \| unavailable \| error` | Current source state |
| `sampled_at` | number (epoch seconds) or null | When this value was last read |
| `age_seconds` | number or null | Computed when serialized |
| `stale` | boolean | `status == ok` and age exceeds the group threshold |
| `error` | string enum or null | `read_failed`, `parse_failed`, `timeout`, `driver_error`, `no_sensor`, `counter_reset`, `not_supported`, `not_available` |

Rules:

- `unsupported`: the hardware/source does not exist on this machine (no
  battery, no NVIDIA GPU, no matching sensor). Values are null.
- `unavailable`: the source exists or is expected but cannot be accessed now
  (permission, missing file, driver not loaded, `/proc` not mounted). Values
  are null.
- `warming_up`: the source exists but a delta is not ready (CPU first sample
  or counter reset). Value is null.
- `error`: the latest read or parse failed unexpectedly. The value is null;
  `sampled_at` refers to the last successful read, if any. The UI shows `—`
  and the error reason, not the old value as live.
- `ok` with `0` is a valid zero and must be displayed as `0`, not `—`.
- Missing values are `null`, never `0`, `""`, or `-1`.
- `stale=true` may coexist with `status="ok"`; the last value remains for
  continuity while the thread is late, but must be visually marked stale.
- `stale` is false for `unsupported`, `unavailable`, `warming_up`, and `error`;
  those are missing, not stale.

Staleness thresholds:

| Group | Threshold |
|---|---|
| CPU utilization | 3.0 s |
| Memory | 3.0 s |
| CPU frequency | 5.0 s |
| Each temperature | 8.0 s |
| GPU | 6.0 s |
| Battery | 15.0 s |
| Storage | 60.0 s |

`service_state`:

- `starting`: no CPU or memory snapshot yet.
- `ok`: CPU utilization and memory are `ok` and not stale.
- `degraded`: either core metric is missing, errored, or stale. Optional GPU,
  battery, temperature, or storage failures do **not** degrade this field;
  their own statuses explain the gap.

### 3.6 Telemetry snapshot schema

`SCHEMA_VERSION = 1`. All byte counts are integers. All percentages are floats
in `[0, 100]`. All timestamps are epoch seconds (float). `null` is explicit.

```json
{
  "schema_version": 1,
  "sequence": 42,
  "sampled_at": 1762680000.123,
  "service_state": "ok",
  "cpu": {
    "model": "13th Gen Intel(R) Core(TM) i7-13700HX",
    "logical_cores": 24,
    "utilization": {
      "status": "ok",
      "percent": 12.4,
      "sampled_at": 1762680000.100,
      "age_seconds": 0.023,
      "stale": false,
      "error": null
    },
    "frequency": {
      "status": "ok",
      "mhz": 2310.5,
      "max_mhz": 4800.0,
      "sampled_at": 1762679999.900,
      "age_seconds": 0.223,
      "stale": false,
      "error": null
    }
  },
  "memory": {
    "status": "ok",
    "total_bytes": 16461942784,
    "used_bytes": 5755121664,
    "available_bytes": 10706821120,
    "percent": 34.96,
    "sampled_at": 1762680000.100,
    "age_seconds": 0.023,
    "stale": false,
    "error": null
  },
  "gpu": {
    "status": "ok",
    "name": "NVIDIA GeForce RTX 4050 Laptop GPU",
    "utilization_percent": 6.0,
    "temperature_c": 44.0,
    "memory_used_bytes": 314966016,
    "memory_total_bytes": 6439305216,
    "memory_percent": 4.89,
    "source": "nvml",
    "sampled_at": 1762679999.900,
    "age_seconds": 0.223,
    "stale": false,
    "error": null
  },
  "temperatures": [
    {
      "id": "cpu_package",
      "label": "CPU Package",
      "celsius": 55.0,
      "source": "hwmon:coretemp/temp1_input",
      "status": "ok",
      "sampled_at": 1762679999.900,
      "age_seconds": 0.223,
      "stale": false,
      "error": null
    },
    {
      "id": "nvme_composite",
      "label": "NVMe",
      "celsius": 39.85,
      "source": "hwmon:nvme/temp1_input",
      "status": "ok",
      "sampled_at": 1762679999.900,
      "age_seconds": 0.223,
      "stale": false,
      "error": null
    },
    {
      "id": "wifi",
      "label": "Wi-Fi",
      "celsius": 44.0,
      "source": "hwmon:iwlwifi_1/temp1_input",
      "status": "ok",
      "sampled_at": 1762679999.900,
      "age_seconds": 0.223,
      "stale": false,
      "error": null
    }
  ],
  "battery": {
    "status": "ok",
    "present": true,
    "percent": 40,
    "state": "discharging",
    "ac_online": false,
    "sampled_at": 1762679998.100,
    "age_seconds": 2.023,
    "stale": false,
    "error": null
  },
  "storage": {
    "status": "ok",
    "mount": "/",
    "total_bytes": 157031038976,
    "used_bytes": 128473645056,
    "available_bytes": 20506062848,
    "percent": 86.24,
    "sampled_at": 1762679975.100,
    "age_seconds": 25.023,
    "stale": false,
    "error": null
  }
}
```

Unsupported GPU example (the rest of the snapshot stays valid):

```json
"gpu": {
  "status": "unsupported",
  "name": null,
  "utilization_percent": null,
  "temperature_c": null,
  "memory_used_bytes": null,
  "memory_total_bytes": null,
  "memory_percent": null,
  "source": null,
  "sampled_at": null,
  "age_seconds": null,
  "stale": false,
  "error": "not_supported"
}
```

The snapshot must serialize to less than 64 KiB (the IPC frame limit) with
worst-case fixed-size strings. Tests assert this bound.

### 3.7 Publication policy

- One `telemetry_snapshot_changed` event per second, after the CPU/memory tick.
- The event data is the snapshot object, not a wrapper.
- The service samples regardless of subscribers; 1 Hz is the floor of useful
  UI freshness. It does **not** poll faster when a client asks.
- `telemetry_get_snapshot` returns the cached snapshot. It has no refresh
  parameter and performs no I/O.
- Clients cannot choose sensors, intervals, mounts, or GPU devices.

---

## 4. MicrophoneService

### 4.1 Scope and naming

**Feature name:** Default microphone mute.

**Controlled object:** the WirePlumber default audio source
(`@DEFAULT_AUDIO_SOURCE@` / the source marked `*` by
`wpctl list audio sources`). The operation is pinned to the numeric node ID
resolved at the start of the request, then verified on that same ID.

**Not controlled:**

- monitor sources of sinks;
- virtual sources, loopbacks, null sinks;
- application capture streams or per-application mute;
- non-default physical microphones;
- KIVO's internal `sounddevice` capture path;
- PipeWire routes, profiles, volume, or default-device selection.

The UI must say “Default microphone”, never “all microphones” or “system
microphone”. If the system default and KIVO's selected input differ, the UI
must show both names and explain that they are different.

### 4.2 Public API

```python
class MicrophoneService:
    def __init__(
        self,
        on_state_changed: Callable[[MicState], None] | None = None,
        *,
        runner: WpctlRunner | None = None,
        poll_interval: float = 5.0,
        identity_interval: float = 30.0,
        state_ttl: float = 2.0,
    ) -> None: ...

    def start(self) -> None: ...
    def close(self, timeout: float = 3.0) -> None: ...
    def get_state(self) -> MicState: ...
    def set_mute(self, muted: bool) -> MicResult: ...
```

- `get_state()` returns the cached state if it is younger than `state_ttl`
  (2 s) and not in an error/unavailable transition; otherwise it performs one
  bounded read. There is no caller-controlled force flag, so repeated IPC calls
  cannot drive unrestricted `wpctl` polling.
- `set_mute(bool)` is the only state-changing API. It never accepts a source
  ID, device name, command, or “toggle”.
- `close()` stops the poll thread and prevents new operations. It never
  unmutes, mutes, or changes the default device on shutdown.

### 4.3 Command runner

`WpctlRunner` wraps `kivo/core/bounded_process.py` with fixed subcommands:

| Operation | argv | Timeout |
|---|---|---|
| Resolve default | `[wpctl, "list", "audio", "sources"]` | 0.75 s |
| Read default mute | `[wpctl, "get-volume", "@DEFAULT_AUDIO_SOURCE@"]` | 0.75 s |
| Read pinned mute | `[wpctl, "get-volume", "<numeric-id>"]` | 0.75 s |
| Set mute | `[wpctl, "set-mute", "<numeric-id>", "1" or "0"]` | 0.75 s |

Rules:

- `wpctl` is resolved once with `shutil.which("wpctl")` at service
  construction. Require an absolute path and an executable regular file. Never
  re-resolve from `PATH` per call.
- `subprocess.Popen` with an argv list, `shell=False` (default),
  `stdin=DEVNULL`, `stdout/stderr=PIPE`.
- Bounded stdout (16 KiB) and stderr (4 KiB); kill the process on timeout or
  overflow; close pipes and selector in `finally`.
- Nonzero exit is a failure.
- **Zero exit with empty stdout is also a failure.** This is mandatory because
  `wpctl get-volume <missing-id>` exits 0.
- Output is decoded as UTF-8 with `errors="replace"` and stripped.
- No user string ever reaches argv. The only variable argument is a validated
  positive integer node ID; the only other values are the fixed
  `@DEFAULT_AUDIO_SOURCE@` alias and the literals `"1"`/`"0"`.

### 4.4 Parsing

`wpctl list audio sources` line format:

```
<id>\t<node.name>\t<media.class>[\t*]
```

- Split on tabs. Require 3 or 4 fields.
- `id`: decimal integer in `[1, 2^31-1]`.
- `node.name`: `[A-Za-z0-9_.:-]{1,255}`; reject anything else.
- `media.class`: exactly `audio/source`.
- The fourth field, if present, must be `*`; the default line has it.
- Skip malformed lines. If no valid `*` line exists → `no_default_source`.
- More than one `*` line → `invalid_output` (do not guess).
- Cap the output at 16 KiB (the runner limit) and the line count at 256.

`wpctl get-volume <id>` format:

```
Volume: 0.38
Volume: 0.38 [MUTED]
```

- Regex: `^Volume:\s+([0-9]+(?:\.[0-9]+)?)(?:\s+\[MUTED\])?\s*$`.
- Volume must be finite and `>= 0`; do not enforce an upper bound (PipeWire
  allows amplification above 1.0).
- `muted = "[MUTED]" in line`.
- Empty stdout, extra lines, or no match → `invalid_output`, even with exit 0.
- The muted form is documented by WirePlumber and must be confirmed on
  hardware in Stage 12.2.2. Until then, tests use a fixture and the UI copy
  does not claim stronger evidence than a verified read.

### 4.5 State and operation models

Backend states (never `transitioning`):

| State | Meaning |
|---|---|
| `muted` | A read of the target source verified the mute marker |
| `unmuted` | A read of the target source verified no mute marker |
| `unavailable` | WirePlumber/`wpctl`/default source is not reachable |
| `error` | A read, parse, set, or verification failed |

`transitioning` is a **UI-local** state while a `mic_set_mute` request is in
flight. It is never emitted by the backend and never persisted.

`MicState` fields:

| Field | Type | Notes |
|---|---|---|
| `state` | enum above | |
| `muted` | bool or null | Null unless verified |
| `verified` | bool | True only if `muted` came from a read |
| `source` | object or null | `{id, name, description, is_default}` |
| `revision` | integer ≥ 0 | Increments whenever an emitted state transition occurs (verified mute/source change or availability transition) |
| `changed_at` | epoch seconds | Last revision increment |
| `sampled_at` | epoch seconds or null | Last read |
| `stale` | bool | Age > 12 s |
| `error_code` | string or null | Enum below |
| `error_message` | string or null | Short, sanitized human text |

Source names/descriptions are sanitized: strip C0/C1 control characters and
Unicode bidi controls (`U+202A`–`U+202E`, `U+2066`–`U+2069`), collapse
whitespace, cap at 128 characters. Empty names fall back to `"Default
microphone"`.

The node `name` can contain a local hardware path (for example a PCI address).
It is returned only in the microphone state for display and is not part of the
telemetry snapshot; it is never sent to a model. The UI should prefer
`description` and show `name` only as secondary detail.

`MicResult` adds:

| Field | Type | Notes |
|---|---|---|
| `ok` | bool | True only if the requested state was verified |
| `requested_muted` | bool | Echo of the request |
| `changed` | bool | Pre-state verified and differed from post-state |
| `default_source_changed` | bool or null | Null if the post-check could not resolve |
| `duration_ms` | integer | Bounded operation duration |

When `default_source_changed` is null, `source.is_default` reflects the last
successful identity read and may be stale until the next poll.

Error codes:

| Code | Meaning |
|---|---|
| `wpctl_missing` | `wpctl` not found or not executable |
| `wireplumber_unavailable` | PipeWire/WirePlumber connection failed |
| `no_default_source` | No source marked `*` |
| `command_failed` | `wpctl` exited nonzero |
| `command_timeout` | A bounded command timed out |
| `invalid_output` | Empty/malformed stdout, including exit 0 with no data |
| `verification_failed` | Post-read did not match the requested state |
| `device_changed` | The default identity changed during the operation |
| `busy` | Another operation holds the lock |
| `rate_limited` | A second set request arrived within 250 ms |
| `service_unavailable` | Service not initialized or already closed |
| `internal_error` | Unexpected exception, sanitized |

### 4.6 Resolve → set → verify algorithm

`set_mute(desired: bool)`:

1. If closed → `service_unavailable`.
2. Acquire the operation lock with a 0.5 s timeout. If unavailable → `busy`.
3. If the last set completed less than 250 ms ago → `rate_limited`.
4. Resolve the default source with `wpctl list audio sources`. No default →
   `no_default_source`, state `unavailable`.
5. Read the pre-state of the **pinned numeric ID** with `get-volume`. If the
   read fails, the pre-state is unknown: continue with the set, set
   `changed=false`, and rely on the verification read.
6. If the pre-state is verified and already equals `desired`, set
   `changed=false`, use the pre-state as the verified pinned state, and skip
   the `set-mute` call. Continue to step 8 so the default identity is still
   re-checked.
7. Otherwise run `set-mute <id> 1|0`. A nonzero exit or timeout is a failure,
   but the state may still have changed; do not return yet. Read
   `get-volume <id>` again. This is the pinned-target verification read. If it
   fails, the pinned state is unknown (`muted=null`, `verified=false`).
8. Best-effort resolve the default again and compare the source identity:
   - same ID/name → `default_source_changed=false`; the result describes the
     pinned target, and `ok` is true only if the pinned state equals `desired`.
   - different → `default_source_changed=true`; read the new default's state.
     The operation returns `ok=false`, `error_code="device_changed"`, and the
     state fields describe the **new default**, not the pinned target. The UI
     resyncs and shows the new device; it never claims the old target is still
     the default.
   - resolve failed → `default_source_changed=null`; the result describes the
     pinned target, and an immediate poll is scheduled.
9. If the default did not change and the pinned verification failed or did
   not match, return `ok=false`, `error_code="verification_failed"`. If the
   default changed, return `device_changed` as above.
10. Update the cached state, increment `revision` whenever the emitted state
    changes (verified mute/source change or availability transition), emit
    `mic_state_changed`, and return the result.

The operation is idempotent in desired state. It never uses `toggle` as the
sole mechanism. `toggle` may be accepted by `wpctl`, but KIVO does not use it.

Worst-case duration: five bounded calls × 0.75 s = 3.75 s. The QML client
timeout is 5 s. Typical duration is ~50 ms.

### 4.7 Polling, hotplug, and default-device changes

Two cadences, one thread:

- **Fast poll, every 5 s:** `get-volume @DEFAULT_AUDIO_SOURCE@`. It follows the
  current default and catches mute changes made by other applications.
- **Identity poll, every 30 s:** `list audio sources`. Refreshes the source ID,
  name, and description, and detects a default change that happens to preserve
  the same mute value.
- Run the identity poll immediately after a fast-poll parse failure, after a
  set operation, or when the observed mute value changes.

Emit `mic_state_changed` only when `(state, muted, source.id, source.name)`
changes or availability transitions. Do not emit on every poll.

Failure backoff: 5 s → 10 s → 20 s → 30 s cap, reset on success. Log the first
failure and then at most once per 60 s.

No `systemctl` call, no WirePlumber restart, no PipeWire configuration write.
KIVO observes and sets one node property; it does not manage the audio stack.

### 4.8 Concurrency, rapid requests, and shutdown

- One `threading.RLock` guards all `wpctl` calls and state updates.
- The poll thread uses `acquire(timeout=0.5)`; if an operation is running, it
  skips that cycle.
- `set_mute` uses the same lock. Concurrent requests receive `busy`; they are
  not queued. This bounds process creation and prevents interleaved
  set/verify sequences.
- A 250 ms rate limit rejects request floods. The UI also disables the button
  while `transitioning`.
- `close(timeout=3.0)`:
  1. set `closed`,
  2. set the stop event,
  3. join the poll thread,
  4. acquire the lock with the remaining timeout,
  5. mark the service closed; do not emit after close.
- If `set-mute` was already dispatched when shutdown begins, it may complete;
  KIVO does not send an unmute, a toggle, or a compensating command. The
  explicit desired state is the only state KIVO ever writes.
- No audio capture object is constructed, imported, or touched by this service.

### 4.9 Microphone state schema

`mic_get_state` result:

```json
{
  "ok": true,
  "state": "muted",
  "muted": true,
  "verified": true,
  "source": {
    "id": 85,
    "name": "alsa_input.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__Mic1__source",
    "description": "700 Series Chipset Family HD Audio Digital Microphone",
    "is_default": true
  },
  "revision": 4,
  "changed_at": 1762680000.0,
  "sampled_at": 1762680000.1,
  "age_seconds": 0.05,
  "stale": false,
  "error_code": null,
  "error_message": null
}
```

Unavailable example:

```json
{
  "ok": false,
  "state": "unavailable",
  "muted": null,
  "verified": false,
  "source": null,
  "revision": 5,
  "changed_at": 1762680001.0,
  "sampled_at": null,
  "age_seconds": null,
  "stale": false,
  "error_code": "wireplumber_unavailable",
  "error_message": "WirePlumber is not reachable."
}
```

`mic_set_mute` result (success):

```json
{
  "ok": true,
  "requested_muted": true,
  "changed": true,
  "state": "muted",
  "muted": true,
  "verified": true,
  "source": { "id": 85, "name": "…", "description": "…", "is_default": true },
  "default_source_changed": false,
  "revision": 5,
  "changed_at": 1762680000.2,
  "sampled_at": 1762680000.2,
  "age_seconds": 0.01,
  "stale": false,
  "duration_ms": 47,
  "error_code": null,
  "error_message": null
}
```

`mic_set_mute` result (failure):

```json
{
  "ok": false,
  "requested_muted": true,
  "changed": false,
  "state": "error",
  "muted": null,
  "verified": false,
  "source": { "id": 85, "name": "…", "description": "…", "is_default": true },
  "default_source_changed": false,
  "revision": 5,
  "changed_at": 1762680000.2,
  "sampled_at": null,
  "age_seconds": null,
  "stale": false,
  "duration_ms": 780,
  "error_code": "verification_failed",
  "error_message": "The requested state could not be verified."
}
```

`mic_state_changed` event data is the `mic_get_state` object without `ok`:

```json
{
  "state": "muted",
  "muted": true,
  "verified": true,
  "source": { "id": 85, "name": "…", "description": "…", "is_default": true },
  "revision": 5,
  "changed_at": 1762680000.2,
  "sampled_at": 1762680000.2,
  "age_seconds": 0.01,
  "stale": false,
  "error_code": null,
  "error_message": null
}
```

Events are emitted only on revision changes/availability transitions, not on
every poll.

---

## 5. IPC contracts

### 5.1 Naming and allowlists

Add to `REQUEST_OPERATIONS`:

| Canonical | Dotted alias | Handler method |
|---|---|---|
| `telemetry_get_snapshot` | `telemetry.get_snapshot` | `telemetry_get_snapshot` |
| `mic_get_state` | `mic.get_state` | `mic_get_state` |
| `mic_set_mute` | `mic.set_mute` | `mic_set_mute` |

Add to `EVENT_NAMES`:

| Canonical | Dotted alias |
|---|---|
| `telemetry_snapshot_changed` | `telemetry.snapshot_changed` |
| `mic_state_changed` | `mic.state_changed` |

`IPCServer._dispatch` already normalizes dots to underscores
(`operation.replace(".", "_")`). No server change is required for aliases.

### 5.2 Request validation

`decode_request` gains strict validators:

- `telemetry_get_snapshot` / `telemetry.get_snapshot`:
  - `params` must be absent or `{}`. Any key → `invalid_params`.
- `mic_get_state` / `mic.get_state`:
  - `params` must be absent or `{}`. Any key → `invalid_params`.
- `mic_set_mute` / `mic.set_mute`:
  - Exactly one required key: `muted`.
  - `muted` must be a JSON boolean. `1`, `0`, `"true"`, `"false"`, `null`,
    missing, or extra keys → `invalid_params`.
  - No `source`, `id`, `device`, `command`, `action`, `toggle`, or `force`
    parameter exists.

These are protocol-layer rejections (`ok:false` response with an `error`
object). Handler-level failures use the domain result with `ok:false` and
`error_code`; they are still successful transport responses.

### 5.3 Telemetry operations

#### `telemetry_get_snapshot`

Request: `{}` or no params.

Result: the snapshot object from §3.6, directly (no wrapper).

Failure behaviour:

- If the service is absent, the handler returns
  `TelemetrySnapshot.unavailable(reason="service_unavailable")` with
  `service_state="degraded"`, CPU/memory `status="unavailable"`, and all
  optional groups `unsupported` or `unavailable`.
- The handler never raises for a missing service and never performs I/O.

#### `telemetry_snapshot_changed`

Event data: the same snapshot object. Emitted once per second while the
service is running. Clients that miss events recover with
`telemetry_get_snapshot`.

### 5.4 Microphone operations

#### `mic_get_state`

Request: `{}` or no params.

Result: the state object from §4.9.

Caching: the service may answer from a state younger than 2 s. A read is
bounded by the runner timeout. Repeated calls cannot force faster polling.

#### `mic_set_mute`

Request:

```json
{ "muted": true }
```

Result: the operation object from §4.9.

Semantics:

- `ok=true` only when the post-operation read verified the requested state.
- `changed` is informational; a no-op request can be `ok=true,
  changed=false`.
- A failed command does not imply the state did not change. The service
  attempts a verification read and reports the observed state if available.
- `default_source_changed=true` means the default moved during the operation.
  The operation returns `ok=false` with `error_code="device_changed"`; the
  event/result describes the new default's observed state, not the pinned
  target.
- The response is always a transport-level success; domain failure is in
  `ok`, `state`, `error_code`, and `error_message`.

#### `mic_state_changed`

Event data: the state object without `ok`. Emitted on verified changes and
availability transitions only.

### 5.5 Timeouts and failure codes

| Layer | Limit |
|---|---|
| `wpctl list` | 0.75 s |
| `wpctl get-volume` | 0.75 s |
| `wpctl set-mute` | 0.75 s |
| Worst-case set operation | 3.75 s |
| QML telemetry request timeout | 3 s |
| QML mic get timeout | 3 s |
| QML mic set timeout | 5 s |
| IPC frame | 64 KiB (existing) |
| Telemetry snapshot | < 64 KiB (tested) |

There is no server-side per-request deadline in the existing IPC server. The
service-level bounded calls are the guarantee. The QML timeout is a safety net
for a lost response, not the primary control.

### 5.6 Subscription and reconnection

- Existing `subscribe_events` semantics are unchanged. A client may subscribe
  to all events or filter to the two new names.
- Event sequence numbers remain per client and monotonic; duplicate or
  out-of-order events are ignored by `KivoIpc.qml`.
- On connection/subscription:
  1. `KivoIpc.qml` resets its locally tracked `telemetrySequence` and
     `micRevision` to 0. These counters belong to the backend service
     instance; a restarted backend begins again at a low value, and a stale
     local counter must not reject the fresh snapshot/state.
  2. It sets `telemetryResyncPending` and `micResyncPending` true, then calls
     `telemetry_get_snapshot` and `mic_get_state`.
  3. It keeps the last displayed values but marks them stale until the fresh
     responses arrive.
  4. It clears `micTransitioning` and fails pending callbacks using the
     existing `_failPending` path.
- On reconnect, a missed mute change is recovered by `mic_get_state`; a missed
  telemetry tick is recovered by `telemetry_get_snapshot`.
- Multiple UI clients are supported. All receive the same events. A set from
  one client updates all clients after verification; no client applies an
  optimistic state.

---

## 6. App lifecycle integration

### 6.1 Initialization order

Initialize both services after `_init_audio()` and before `_init_ipc()`:

```
[1] configuration
[2] memory
[3] brain
[4] screen context
[5] actions
[6] task orchestrator
[7] Drive
[8] confirmation
[9] audio + media
[10] telemetry service      ← new
[11] microphone service     ← new
[12] IPC server + handlers  ← existing, now receives both services
```

- Construct and start telemetry first so the first snapshot exists before a
  client can request it.
- Construct and start the microphone service before the IPC server so the
  first state is available; it may start in `unavailable` and recover later.
- Wrap each new initialization in `try/except Exception`; log a sanitized
  message and store `None` on failure. `KivoApp.initialize()` must still
  return `True` if the rest of the backend is healthy.
- `IPCHandlers` gains optional `telemetry_service=None` and
  `microphone_service=None` keyword arguments. Existing call sites and tests
  continue to work.
- The ordering is normative; the human-readable `[N/9]` progress labels are
  cosmetic and may be adjusted to `[N/12]` (or the new calls kept inside the
  existing step 9 before the IPC server starts). Do not change the order in
  which resources become available or are released.

### 6.2 Event wiring

`KivoApp` adds two callbacks:

```python
def _on_telemetry_snapshot(self, snapshot) -> None:
    self._publish("telemetry_snapshot_changed", snapshot.to_dict())

def _on_mic_state_changed(self, state) -> None:
    self._publish("mic_state_changed", state.to_dict())
```

`_publish` already catches exceptions and no-ops when the IPC server is absent.
The service threads never touch the socket directly.

`_publish_initial_state()` additionally publishes the current microphone
state (if available) and the latest telemetry snapshot (if available). The
QML bridge also performs explicit get requests on subscribe, so this is
belt-and-braces rather than the only path.

### 6.3 Shutdown order

Add after the existing media close and before `ipc.stop()`:

```
media.close_sync()                 (existing)
telemetry_service.close(2.0)       (new)
microphone_service.close(3.0)      (new)
ipc.stop()                         (existing)
runtime state cleanup              (existing)
```

- Services stop producing events before the server disappears.
- Handlers hold service references; a closed service returns
  `service_unavailable` instead of touching hardware.
- No shutdown path calls `set-mute`, `toggle`, `set-default`, or any
  compensating audio command.
- `close()` is idempotent and safe if initialization failed.

### 6.4 Thread budget

| Thread | Owner | Daemon | Stop mechanism | Join |
|---|---|---|---|---|
| `kivo-telemetry` | TelemetryService | yes | `threading.Event` | 2 s |
| `kivo-microphone` | MicrophoneService | yes | `threading.Event` | 3 s |
| media monitor | MediaService | yes | existing async close | existing |
| IPC client reader/writer | IPCServer | existing | socket close | existing |
| STT/audio workers | existing | existing | existing | existing |

Two new daemon threads. No new event loop. No thread pool. No `Timer` per
metric or per request.

---

## 7. QML and UI specification

### 7.1 `KivoIpc.qml` additions

Properties:

```qml
property var telemetrySnapshot: null
property int telemetrySequence: 0
property bool telemetryResyncPending: true
property var micState: null
property int micRevision: 0
property bool micResyncPending: true
property bool micTransitioning: false

readonly property bool telemetryLinkStale: telemetryResyncPending || !connected
readonly property bool micLinkStale: micResyncPending || !connected
readonly property bool telemetryAvailable: telemetrySnapshot !== null
readonly property string telemetryCpuStatus: ...
readonly property real telemetryCpuPercent: ...
readonly property bool telemetryCpuStale: ...
readonly property string micStateName: micState ? micState.state : "unavailable"
readonly property bool micMuted: micState !== null && micState.verified === true
                                 && micState.muted === true
readonly property bool micAvailable: micStateName === "muted"
                                     || micStateName === "unmuted"
readonly property string micSourceName: ...
```

Functions:

```qml
function telemetryGetSnapshot(callback)
function micGetState(callback)
function micSetMute(muted, callback)
```

- `micSetMute` rejects non-boolean input before sending.
- It sets `micTransitioning=true`, sends exactly `{muted: Boolean(muted)}`,
  and starts a 5 s client timer.
- On a verified response with `ok=true` and `revision >= micRevision`, it
  applies the state, clears `micTransitioning` and `micResyncPending`, and
  calls the callback.
- On a domain error, it clears `micTransitioning`, keeps the previous verified
  state, records the error, calls `micGetState()` to resync, and calls the
  callback.
- On client timeout, it behaves like an error (`client_timeout`), does not
  change `micMuted`, and resyncs.
- `telemetryGetSnapshot` and `micGetState` clear their resync-pending flag only
  when a valid response is applied. Until then, the old values remain visible
  with a stale marker.
- `telemetryGetSnapshot` has a 3 s timeout and applies the snapshot only if
  `sequence >= telemetrySequence` after the reconnect reset described in
  §5.6.

Event cases:

```qml
case "telemetry_snapshot_changed": ...
case "telemetry.snapshot_changed": ...
case "mic_state_changed": ...
case "mic.state_changed": ...
```

Validation reuses the existing `_isObject`, `_bool`, `_number`, and `_text`
helpers. A malformed payload increments `invalidMessages` and never mutates
state. Event application uses the same `telemetrySequence`/`micRevision`
ordering checks as the get responses.

On `subscribed` becoming true, call both get functions. Keep the old values
visible but stale until the fresh responses arrive.

### 7.2 Compact Dynamic Notch

Preserve the existing collapsed row: character icon, `KIVO`, divider,
waveform/thinking indicators, label, elapsed time, stop button, status dot.

Add:

1. **CPU chip** between `KIVO` and the label.
   - Text: `CPU 12%` (`Theme.monoFamily`, `Theme.fontMicro`, `Theme.textSecondary`).
   - Visible only when `visualState === "idle"`, no media is playing, and CPU
     telemetry is `ok` and not stale.
   - If CPU is unavailable, the chip is hidden; the notch does not grow to
     show a dash. The expanded view explains the gap.
   - Idle collapsed width becomes `232` when the chip is visible; otherwise
     the existing `176`. The value is a measurement target; adjust by ±8 px in
     12.2.3 and keep it ≤ 240.
2. **Microphone badge** immediately before the status dot.
   - A 12 px Canvas-drawn mic-with-slash glyph (`MicIndicator.qml`).
   - Visible only when `ipc.micStateName === "muted"` and `verified === true`.
   - Color `Theme.warning`; no fill background.
   - It does not change the notch width: the label uses `Layout.fillWidth` and
     absorbs the 12 px. During media playback the title elides slightly more.
   - No muted badge for `unmuted`, `unavailable`, `error`, or `transitioning`.
     Transitioning may show a dimmed, pulsing outline of the badge without the
     slash, but never the muted glyph.

Compact precedence:

```
transient visual states > media playing > telemetry idle chip > "Ready"
```

The mic badge is independent and may appear with any of them once verified
muted. It never appears based on a button click alone.

No memory, GPU, temperature, battery, or storage values in the compact notch in
v1. They do not fit legibly without widening the notch beyond the media width.

### 7.3 Expanded Dynamic Notch

Add two idle-only sections after the existing body content:

1. **`TelemetrySummary`** (new component), visible when
   `visualState === "idle"` and no media is active.
   - 3 columns × 2 rows of 39 px compact cards: CPU, Memory, GPU, CPU Temp,
     Battery, Storage. Two 39 px rows plus 6 px spacing gives 84 px; the
     section is 86 px including 2 px top padding.
   - Each card: label (`Theme.fontMicro`, muted), value (`Theme.fontBody`,
     primary), unit/secondary line (`Theme.fontMicro`, secondary).
   - Values: `12%`, `5.4 / 15.3 GB`, `6% · 44°C`, `55°C`, `40% · discharging`,
     `86% used`.
   - Missing/unsupported: `—` plus a short reason on the dashboard; do not show
     `0`.
   - Stale: value at 60 % opacity with `stale` in `Theme.warning`.
   - GPU hidden entirely if `status === "unsupported"`; the grid reflows to
     5 cards.
   - Height: 86 px including spacing.
2. **`MicControl`** (new component), visible when `visualState === "idle"`.
   - A 44 px row: `MicIndicator` + “Default microphone” + verified state text
     + explicit action button.
   - Muted: “Muted” in `Theme.warning`; button “Unmute”.
   - Unmuted: “Live” in `Theme.accent`; button “Mute”.
   - Transitioning: “Applying…” in `Theme.textSecondary`; both buttons
     disabled; no state flip.
   - Unavailable: “WirePlumber unavailable” or “No default microphone”;
     button disabled; a “Retry” action refreshes state.
   - Error: short error text in `Theme.danger`; “Retry” action.
   - Below the row, a `Theme.fontMicro` note when KIVO's selected input
     differs from the default: “KIVO Push-to-Talk uses <device>.” This is
     essential to avoid the false impression that this control gates KIVO's
     own capture.
   - Height: 44 px (56 px with the note).

`expandedHeight` adjustment in the idle branch:

```
if visualState == "idle":
    if not mediaActive: base += 86      # TelemetrySummary
    base += 44 or 56                    # MicControl (+ optional note)
# existing media drawer addition remains unchanged
```

All non-idle states keep their current heights. Media playback in the idle
state keeps the existing drawer; telemetry summary yields to media, while the
mic control remains visible.

### 7.4 Dashboard Overview

Add a detailed `TelemetrySummary` grid below the existing status rows:

- 3 columns × 2 rows, 56 px cards, ~122 px total including spacing.
- The page currently uses about 366 px of 540 px; 122 px fits without
  scrolling.
- Show `last updated` as a relative age in the section header, e.g.
  “updated 0.4 s ago”; if stale, “stale 12 s” in `Theme.warning`.
- Show unsupported sensors as “Not available on this device”, not `0`.
- Show CPU model, logical cores, GPU name/source, and storage mount in a
  secondary line where useful. Do not show serial numbers, UUIDs, hostnames,
  or process data.
- If the backend is offline, keep the last values but mark the section stale.

### 7.5 Dashboard Audio

Add a “Default microphone” bar between the status cards and the latest
speech section:

- Height 56 px; reduce the device list from 180 px to 150 px to keep the page
  within 540 px. The list remains scrollable.
- Content:
  - `MicIndicator` + “Default microphone”.
  - Source description (sanitized, elided).
  - State pill: Muted / Live / Applying… / Unavailable / Error.
  - Button: “Unmute default mic” or “Mute default mic”; disabled when offline,
    transitioning, or unavailable.
  - Note: “Controls the WirePlumber default source only. KIVO Push-to-Talk
    uses: <selected KIVO device>.”
- On page visible: call `ipc.micGetState()`.
- On button click: call `ipc.micSetMute(!ipc.micMuted)`. Do not flip the local
  label first.
- On event: update the pill only if `revision >= micRevision`.
- On error: show the sanitized `error_message` and a “Retry” action.

### 7.6 UI state table

| Backend state | Compact notch | Expanded notch | Dashboard Audio |
|---|---|---|---|
| `muted`, verified | Mic-off badge, warning | “Muted”, Unmute button | Muted pill, Unmute |
| `unmuted`, verified | No badge | “Live”, Mute button | Live pill, Mute |
| `transitioning` (local) | Dimmed pulsing badge without slash | “Applying…”, buttons disabled | Applying pill, disabled |
| `unavailable` | No badge | “WirePlumber unavailable”, Retry | Unavailable pill, Retry |
| `error` | No badge | Short error, Retry | Error pill, Retry |
| Telemetry stale | CPU chip hidden | Value dimmed + “stale” | “stale N s” |
| Telemetry unsupported | Chip hidden | `—` / card hidden | “Not available” |

Rules:

- A muted badge appears only after `verified === true`.
- `transitioning` never uses the muted glyph or the muted color.
- Unavailable/error never display `0` as a placeholder; they display `—` or a
  text reason. A verified zero (for example, 0 % CPU) is displayed as `0`.
- No animation that could be mistaken for a completed state. Reduced motion
  removes pulses.

### 7.7 Visual system

Use only existing `Theme` tokens:

- surfaces: `surfaceSunken`, `surfaceRaised`, `surfaceHover`;
- lines: `divider`, `border`;
- text: `textPrimary`, `textSecondary`, `textMuted`;
- accents: `accent`, `accentStrong`, `mint`, `warning`, `danger`;
- type: `fontFamily`, `monoFamily`, `fontMicro`, `fontCaption`, `fontBody`;
- radii: match the existing dashboard/card literals (12 px cards, 8 px inner
  rows); no new radius tokens are required;
- durations: `Theme.durationFast`/`durationNormal`, zero under reduced motion.

No new palette, no neon, no generic dashboard redesign.

---

## 8. Failure and performance analysis

| Scenario | Behaviour | UI |
|---|---|---|
| Missing `/proc`/`/sys` entry | Group `unavailable`/`error`; other groups unaffected | `—` / reason |
| CPU counter reset | `warming_up`, `counter_reset`; next sample recovers | `—`, no wild value |
| First CPU sample | `warming_up`; service publishes static/other groups | `—` |
| Sensor disappears | Cached path `ENOENT`; rediscover immediately (rate-limited) | Card hides/`—` |
| No battery | `unsupported` | Battery card hidden |
| No NVIDIA GPU/tooling | `unsupported`; no subprocess spam | GPU card hidden |
| NVML driver error | GPU `error`/`unavailable`; retry next cycle; re-init ≤1/60 s | GPU `—` |
| `nvidia-smi` timeout/malformed | GPU `error`; other metrics valid | GPU `—` |
| Slow telemetry read | Thread late; per-group age grows; `stale=true` | Dimmed + stale |
| Telemetry thread exception | Caught per cycle; 1 s backoff; service continues | Last values stale |
| WirePlumber restart | Fast poll parse/connection failure → `unavailable`; backoff; recovery emits | Unavailable → Live/Muted |
| PipeWire connection failure | Same as above | Unavailable |
| `wpctl` missing | `wpctl_missing`, `unavailable`; backend startup succeeds | Unavailable |
| No default source | `no_default_source`, `unavailable` | No default microphone |
| Default device changes | Identity poll or post-set check detects; revision increments; new state emitted | New source name |
| Hotplug during set | Pinned target set/verify; post-check reports `default_source_changed` and `device_changed` | Notice + new default state |
| Rapid mute/unmute | Second request `busy`/`rate_limited`; UI disabled | Applying, then verified |
| Shutdown during set | Bounded command completes or times out; no compensating command; no event after close | Shell disconnects |
| IPC disconnect | Existing reconnect; get requests resync; values marked stale | Reconnecting → fresh |
| Multiple clients | Serialized operations; all receive verified events; revision ordering | No optimistic flicker |
| Partial telemetry failure | Snapshot remains valid; failed group only | Per-card reason |
| Event queue full | Existing server behaviour (bounded queue) applies | Connection recovers |

Performance rules:

- No busy-wait; `Event.wait` with a deadline.
- No per-tick process creation for standard telemetry.
- GPU fallback at most one process per 2 s cycle.
- Mic polling at most one process per 5 s plus one identity process per 30 s.
- No client-controlled interval or force-refresh.
- Snapshot and event payloads bounded well below 64 KiB.
- Shutdown joins are bounded; a stuck read cannot block backend exit forever.

---

## 9. Test matrix

Tests are designed now, implemented in the indicated stage. All filesystem
and subprocess inputs are mocked or placed under a temporary directory; no
test touches the live KIVO socket, the live microphone, PipeWire, or the real
`/proc`/`/sys` unless explicitly marked as an on-hardware integration test.

### 9.1 Telemetry unit tests

| ID | Case | Expected |
|---|---|---|
| T01 | Two valid `/proc/stat` samples | Correct percent, clamped 0–100 |
| T02 | First sample | `warming_up`, `percent=null` |
| T03 | Counter reset / total decreases | `warming_up`, `counter_reset`, next sample recovers |
| T04 | Zero delta | No division by zero; `warming_up` |
| T05 | Malformed `/proc/stat` | `error`, other metrics unaffected |
| T06 | Missing `/proc/stat` | `unavailable` |
| T07 | CPU frequency all cores | Correct average MHz |
| T08 | CPU frequency partial/missing/malformed | Partial average or `unavailable` |
| T09 | Memory normal | total/used/available/percent correct |
| T10 | `MemAvailable` missing, fallback fields present | Correct fallback |
| T11 | Memory total 0 / malformed | `error`/`unavailable`, no division by zero |
| T12 | Temperature priority: coretemp package | Selected first |
| T13 | Fallback `x86_pkg_temp`, `TCPU`, `acpitz` | Correct order |
| T14 | NVMe/Wi-Fi discovery | Correct IDs/labels |
| T15 | Unlabelled `acer`/`spd5118` | Excluded |
| T16 | Temperature out of range | `error`, no value |
| T17 | Battery `charge_*` only | Percent correct |
| T18 | Battery `energy_*` only | Percent correct |
| T19 | No battery / absent battery | `unsupported` |
| T20 | AC online/offline | Correct boolean |
| T21 | Battery malformed/partial | `error`/`unavailable` |
| T22 | Storage normal | Matches statvfs math and df % |
| T23 | Storage `OSError` / zero total | `error`/`unavailable` |
| T24 | NVML success | All fields, `source="nvml"` |
| T25 | NVML zero devices | `unsupported` |
| T26 | NVML driver not loaded | `unavailable`/`error` |
| T27 | NVML transient error | `error`, retry next cycle |
| T28 | `nvidia-smi` success | Fields parsed, MiB→bytes |
| T29 | `nvidia-smi` missing/nonzero/timeout | Correct status |
| T30 | `nvidia-smi` malformed/wrong fields/oversize | `error` |
| T31 | GPU name with comma | Name preserved, numbers correct |
| T32 | Valid zero values | Status `ok`, value `0` |
| T33 | Missing values | `null`, never `0` |
| T34 | Staleness thresholds | `stale=true` only when age exceeds threshold |
| T35 | Partial snapshot failure | Other groups remain `ok` |
| T36 | Snapshot JSON size | < 64 KiB |
| T37 | Schema allowlist | No hostname/PID/serial/UUID/MAC/SSID |
| T38 | Sampling cadence (fake clock) | Expected call counts per 60 s |
| T39 | Repeated `telemetry_get_snapshot` | No extra I/O; sequence unchanged |
| T40 | `close()` idempotent | Thread exits, no events after close |

### 9.2 Microphone unit tests

| ID | Case | Expected |
|---|---|---|
| M01 | `list` with one `*` | Correct ID/name |
| M02 | No `*` / multiple `*` | `no_default_source` / `invalid_output` |
| M03 | Malformed/tab/oversize lines | Skipped or `invalid_output` |
| M04 | `get-volume` unmuted fixture | `unmuted` |
| M05 | `get-volume` muted fixture | `muted` (documented format) |
| M06 | Exit 0 + empty stdout | `invalid_output`, not success |
| M07 | Exit 0 + “Node not found” stderr | `invalid_output`/`unavailable` |
| M08 | Explicit mute | argv ends `1`, never `toggle` |
| M09 | Explicit unmute | argv ends `0` |
| M10 | Set succeeds, verify matches | `ok=true`, verified |
| M11 | Set succeeds, verify mismatches | `verification_failed`, `ok=false` |
| M12 | Set nonzero/timeout | `command_failed`/`command_timeout` recorded; `ok` is decided only by the post-read, never assumed |
| M13 | Verification timeout | `verification_failed`, `muted=null` |
| M14 | No default source | `no_default_source`, `unavailable` |
| M15 | Default changes during set | Pinned target set/verified; `default_source_changed=true`, `ok=false`, `device_changed`; result describes the new default |
| M16 | Rapid requests | One operation; `busy`/`rate_limited`; no process storm |
| M17 | Shutdown during operation | Close waits; no event; no unmute |
| M18 | `wpctl` missing | `wpctl_missing`; startup continues |
| M19 | WirePlumber restart/recovery | `unavailable` → verified state event |
| M20 | Poll emits only on change | No event every 5 s |
| M21 | `get_state` TTL | Cache within 2 s; fresh read after |
| M22 | Source name control/bidi chars | Sanitized, capped |
| M23 | No recording/audio API | Static import/call assertions |
| M24 | No default/routing/volume changes | argv allowlist assertion |
| M25 | No agent tool | Tool registry contains no mic tool |

### 9.3 IPC tests

| ID | Case | Expected |
|---|---|---|
| I01 | New ops/aliases in allowlist | Accepted |
| I02 | Unknown op | `unsupported_operation` |
| I03 | `mic_set_mute` boolean accepted | `true`/`false` only |
| I04 | `1`, `"true"`, null, missing, extra keys | `invalid_params` |
| I05 | Telemetry/mic get with extra params | `invalid_params` |
| I06 | Dotted alias dispatch | Same handler |
| I07 | Response required fields/types | Schema match |
| I08 | Event subscription/filter | Only requested events |
| I09 | Event sequence/duplicate handling | Monotonic, duplicates ignored |
| I10 | Reconnect resync | Fresh snapshot/state |
| I11 | Multiple clients | All receive verified event |
| I12 | Handler without services | Degraded shape, no exception |
| I13 | Oversized/fuzzed params | Rejected, no crash |
| I14 | No command/path parameter | Schema allowlist |

### 9.4 QML and integration tests

| ID | Case | Expected |
|---|---|---|
| Q01 | `qmllint` all QML | Zero errors |
| Q02 | KivoIpc has new properties/functions/events | Static assertions |
| Q03 | Compact CPU chip idle only | Hidden with media/non-idle |
| Q04 | Mic badge only after verified muted event | No badge on click alone |
| Q05 | Transitioning presentation | No muted claim; disabled control |
| Q06 | Unavailable/error presentation | No `0`, no muted glyph |
| Q07 | Stale telemetry presentation | Dimmed + stale label |
| Q08 | Dashboard Audio mute/unmute calls | Boolean argument, no optimistic flip |
| Q09 | Fake backend telemetry event | Correct values rendered |
| Q10 | Fake backend mic event | Correct state rendered |
| Q11 | Reconnect | Values stale until resync |
| Q12 | Existing media/PTT/notch tests | Unchanged and passing |

### 9.5 Adversarial validation (Stage 12.2.3)

- Fake `wpctl` scripts: exit 0 with empty output; malformed volume; huge
  output; sleep past timeout; control/bidi characters in names; contradictory
  states; default change mid-operation.
- Fake `/proc`/`/sys` trees: truncated, negative, huge, non-numeric, missing.
- Fake NVML provider: import failure, init failure, query failure, bad ranges.
- 50 concurrent `mic_set_mute` requests: exactly one operation, deterministic
  `busy`/`rate_limited`, no orphan processes.
- Socket fuzzing for the new operations: wrong types, nested objects, arrays,
  oversized strings, duplicate keys, 64 KiB boundary.
- Shutdown race: close while a fake slow `wpctl` is running; assert bounded
  exit and no post-close events.
- No live microphone change in automated tests. On-hardware mute/unmute
  validation is a separate, user-approved manual step that records and
  restores the original state.

---

## 10. Implementation sequence

### Stage 12.2.1 — Telemetry service

**Deliverables**

- `kivo/core/bounded_process.py` + tests.
- `kivo/telemetry/{__init__,model,readers,gpu,service}.py`.
- `kivo/app.py`: construct/start/stop the service; no IPC or QML changes.
- `tests/test_telemetry_readers.py`, `tests/test_telemetry_service.py`.
- No protocol, handler, or QML changes in this stage.

**Acceptance criteria**

- Service starts and stops cleanly; thread join ≤2 s.
- Snapshot contains all groups; a missing GPU/sensor does not invalidate it.
- CPU first sample/reset handled; no negative or >100 % values.
- No new dependency; no sysfs/procfs writes; no process surveillance.
- Measured standard overhead <0.5 % of one logical CPU; fallback <1.5 %.
- Existing backend/shell suites, QML lint, desktop health 9/9, and
  `git diff --check` pass.
- No microphone state is read or changed by this stage.

**Suggested verification**

```
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m pytest shell/tests/ -q
/usr/lib/qt6/bin/qmllint shell/shell.qml shell/components/*.qml \
    shell/services/*.qml shell/styles/*.qml
./scripts/desktop-health.sh
git diff --check
```

### Stage 12.2.2 — Microphone mute service

**Deliverables**

- `kivo/microphone/{__init__,model,wpctl,service}.py`.
- `kivo/app.py`: construct/start/stop the service; no IPC or QML changes.
- `tests/test_microphone_service.py`.
- Optional `tests/test_bounded_process.py` additions if not complete in 12.2.1.

**Acceptance criteria**

- Default source resolution, explicit `1`/`0`, post-set verification.
- Exit 0 with empty stdout is a failure; verification is mandatory.
- No `toggle`, `set-default`, `set-volume`, `set-profile`, or `set-route`.
- No recording, no audio samples, no agent tool.
- Rapid requests are serialized/rate-limited; no orphan processes.
- Shutdown never unmutes or compensates.
- `wpctl`/WirePlumber absence degrades to `unavailable`; backend startup
  still succeeds.
- Existing suites and gates pass.
- On-hardware validation, with explicit user consent:
  1. read and record the original state;
  2. mute, verify the marker, unmute, verify;
  3. confirm the default device, route, and volume did not change;
  4. restore the original state.

### Stage 12.2.3 — IPC and UI integration + adversarial validation

**Deliverables**

- `kivo/ipc/protocol.py`: operations, events, validators.
- `kivo/ipc/handlers.py`: `telemetry_get_snapshot`, `mic_get_state`,
  `mic_set_mute`, optional service kwargs.
- `kivo/app.py`: handler wiring, initial event publication.
- `shell/services/KivoIpc.qml`: properties, requests, events, timeouts.
- `shell/components/DynamicNotch.qml`: compact chip, mic badge, expanded
  summary/control.
- New `TelemetryChip.qml`, `TelemetrySummary.qml`, `MicIndicator.qml`,
  `MicControl.qml`.
- `shell/components/DashboardOverview.qml`: telemetry grid.
- `shell/components/DashboardAudio.qml`: default microphone bar.
- `tests/test_telemetry_ipc.py`, `tests/test_microphone_ipc.py`,
  `shell/tests/test_notch.py` extensions.
- Adversarial validation report.

**Acceptance criteria**

- All three operations and both events work through the real socket.
- `mic_set_mute` rejects non-booleans and extra params.
- UI never shows muted before a verified response/event.
- Transitioning/unavailable/error/stale states are distinct and legible.
- Compact notch preserves media playback and expansion; no width jump beyond
  the specified idle target.
- Dashboard Overview and Audio remain within their 540 px page height.
- Existing media, PTT, task, and notch tests pass unchanged.
- Full gates: backend suite, shell suite, QML lint, desktop health 9/9,
  `git diff --check`, and compile check.
- Manual on-hardware mute/unmute validation is recorded; original state is
  restored.
- No commit is made in the current research task; future stages follow the
  KIVO local-commit workflow and never push.

---

## 11. Known limitations and deferred work

1. **Default source only.** Monitor/virtual/application sources are not
   controlled. The UI must use the “Default microphone” label.
2. **KIVO PTT is separate.** `sounddevice` may capture a selected device that
   is not the WirePlumber node, or may follow the system default. The mute
   feature does not guarantee PTT gating and must say so.
3. **Muted marker confirmation.** The `[MUTED]` output form is documented but
   was not observed on this host; Stage 12.2.2 confirms it with consent.
4. **Polling latency.** External mute changes can take up to 5 s to appear;
   default identity changes up to 30 s (or immediately after a set/failure).
5. **`wpctl` exit-code quirk.** Missing nodes exit 0; only strict stdout
   parsing plus verification catches this.
6. **Single GPU, device 0.** Multi-GPU selection is deferred.
7. **No GPU power.** Unit/scale was inconsistent between `nvidia-smi` and
   NVML.
8. **NVML is process-global.** A future second NVML user needs a shared
   refcount before adding `nvmlShutdown()` calls.
9. **`pynvml` shim is deprecated.** No dependency change in this stage;
   schedule a separate `nvidia-ml-py` cleanup.
10. **No PipeWire event subscription.** Polling is simpler and bounded;
    event-driven updates can replace it later if overhead becomes material.
11. **Notch height.** The idle expanded view grows by ~130 px. If the media
    drawer plus telemetry exceeds the available screen height, the dashboard
    remains the full-detail surface and the notch section can be collapsed in
    a later polish stage.
12. **No new desktop-health probe.** The check remains 9/9; adding telemetry or
    mic probes would change the documented baseline and is deferred.
