# KIVO — Stage 12.2.3 Integration and Validation Report

```
Document:   Stage 12.2.3 integration and adversarial validation report
Status:     Complete
Date:       2026-11-09
Baseline:   HEAD 1062c0d
Scope:      Hardware telemetry; WirePlumber default microphone mute; IPC; Quickshell UI
```

---

## 1. Architecture Integration

Stage 12.2.3 integrates the read-only telemetry service (`kivo/telemetry/`) and default microphone mute service (`kivo/microphone/`) into KIVO's Unix-domain IPC server and Quickshell Qt6 interface.

```
┌──────────────────────────────────────────────────────────────────┐
│ KivoApp (app.py)                                                 │
│  owns: telemetry_service (12.2.1), microphone_service (12.2.2)    │
│  lifecycle: starts before IPCServer; stops before IPCServer stops │
└───────────────────────────────┬──────────────────────────────────┘
                                │ on_snapshot / on_state_changed
┌───────────────────────────────▼──────────────────────────────────┐
│ IPCServer & IPCHandlers (protocol v1)                            │
│  operations: telemetry_get_snapshot, mic_get_state, mic_set_mute │
│  events: telemetry_snapshot_changed, mic_state_changed           │
└───────────────────────────────┬──────────────────────────────────┘
                                │ AF_UNIX stream (kivo.sock)
┌───────────────────────────────▼──────────────────────────────────┐
│ KivoIpc.qml (Single shared IPC bridge)                           │
│  exposes: telemetrySnapshot, micState, micSetMute, etc.          │
└───────────────┬───────────────────────────────┬──────────────────┘
                │                               │
┌───────────────▼──────────────┐┌───────────────▼──────────────────┐
│ DynamicNotch.qml             ││ KivoDashboard.qml                │
│  compact CPU & mic chips     ││  DashboardOverview: Telemetry    │
│  expanded TelemetrySummary   ││  DashboardAudio: Default Mic row │
│  expanded MicControlRow      ││  PTT transparency disclaimer     │
└──────────────────────────────┘└──────────────────────────────────┘
```

---

## 2. IPC Operation and Event Contracts

### 2.1 Allowlisted Operations

| Canonical Operation | Dotted Alias | Params | Response Result |
|---|---|---|---|
| `telemetry_get_snapshot` | `telemetry.get_snapshot` | None or `{}` | Snapshot object directly (`schema_version`, `sequence`, `cpu`, `memory`, `gpu`, `temperatures`, `battery`, `storage`) |
| `mic_get_state` | `mic.get_state` | None or `{}` | State object (`state`, `muted`, `verified`, `source`, `revision`, `stale`) |
| `mic_set_mute` | `mic.set_mute` | `{"muted": bool}` strictly | Result object (`ok`, `requested_muted`, `changed`, `default_source_changed`, `duration_ms`, `state`) |

### 2.2 Strict Protocol Validation

- `telemetry_get_snapshot` & `mic_get_state`: any non-empty parameter dictionary raises `invalid_params`.
- `mic_set_mute`:
  - Exactly one parameter key allowed: `muted`.
  - Type must strictly be boolean (`type(v) is bool`). Integers (`1`, `0`), strings (`"true"`, `"false"`), `null`, floats, arrays, or objects are rejected with `invalid_params`.
  - No `node_id`, `device`, `command`, `shell`, or `toggle` parameters are accepted.

### 2.3 IPC Events

- `telemetry_snapshot_changed` (`telemetry.snapshot_changed`): published approximately once per second by the telemetry daemon thread.
- `mic_state_changed` (`mic.state_changed`): published whenever the verified state, mute condition, or default source node identity changes.

---

## 3. QML Integration (`KivoIpc.qml`)

`shell/services/KivoIpc.qml` was extended without altering the single-bridge architecture:
- Added reactive properties for telemetry (`telemetrySnapshot`, `telemetryCpuPercent`, `telemetryCpuStale`, `telemetryMemoryPercent`, `telemetryGpuPercent`, `telemetryBatteryPercent`, `telemetryStoragePercent`).
- Added reactive properties for microphone state (`micState`, `micMuted`, `micVerified`, `micSourceDescription`, `micTransitioning`, `micStale`).
- Implemented request wrappers: `telemetryGetSnapshot(callback)`, `micGetState(callback)`, `micSetMute(muted, callback)`.
- Event handling for `telemetry_snapshot_changed` and `mic_state_changed` using monotonic sequence and revision checks.
- On connection or reconnect: resets local `telemetrySequence` and `micRevision` to 0, sets resync pending flags, and fetches fresh authoritative snapshots from the backend.

---

## 4. Dynamic Notch Integration

- **Compact Idle View**:
  - `compactCpuChip`: renders `CPU 12%` in idle state when telemetry is available, ok, and not stale. Notch idle width expands from 176 px to 232 px.
  - `compactMicChip`: renders `🔇` badge when the default microphone is verified muted (`state == "muted"`, `verified == true`, and not stale).
- **Expanded View**:
  - `TelemetrySummary.qml`: 3×2 compact card grid showing CPU, RAM, GPU, Package Temperature, Battery, and Disk usage with units, staleness markers, and fallback indicators. Height: 74 px.
  - `MicControlRow.qml`: default microphone status row with friendly description, verified state badge, explicit Mute/Unmute action button, and PTT caveat disclaimer. Height: 46 px.
  - Coexistence: when media is actively playing, telemetry summary yields to the media drawer while keeping the interface responsive and within screen bounds.

---

## 5. Dashboard Integration

- **DashboardOverview.qml**:
  - Added a dedicated "Hardware Telemetry" section hosting `TelemetrySummary`.
  - Stale link marker displayed if telemetry data is older than its threshold.
  - Fits comfortably within the 540 px dashboard page height.
- **DashboardAudio.qml**:
  - Added a dedicated 58 px "Default Microphone" control section with verified state badge, friendly node description, node ID, and explicit Mute/Unmute button.
  - PTT Transparency Disclaimer: explicitly informs the user that default microphone mute operates on the WirePlumber default source and does not guarantee KIVO Push-to-Talk or external capture privacy.

---

## 6. Live Hardware Validation (Section 11)

Authorized live validation was executed on host hardware:
- **Device Target**: Node **84** (`alsa_input.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__Mic1__source` / `Internal Microphone`).
- **Initial State**: `Volume: 0.38` (unmuted).
- **Controlled Mute**: `service.set_mute(True)` applied in 35.5 ms.
- **Observed Hardware Output**: `Volume: 0.38 [MUTED]` (empirically confirmed WirePlumber mute syntax).
- **Immediate Restoration**: `runner.set_mute(84, False)` applied.
- **Final State Verified**: `Volume: 0.38` (unmuted, `muted=False`, `verified=True`).
- **Zero Orphaned Processes**: verified with `pgrep wpctl`.

---

## 7. Security Review

- ✅ **No Agent Tool Registration**: Neither telemetry nor microphone mute is exposed to LLMs or registered as an agent tool.
- ✅ **No Arbitrary Execution**: Subprocesses are strictly argv-only with absolute paths, bounded pipes (16 KiB / 4 KiB), and start_new_session process-group termination.
- ✅ **No Shell Invocations**: Zero occurrences of `shell=True` or `os.system`.
- ✅ **No Raw Audio Exposure**: Zero references to audio recording, PCM buffers, or sounddevice in `kivo/microphone/`.
- ✅ **No Automatic Unmute**: Service shutdown never calls `set-mute 0` or `toggle`.
- ✅ **No Arbitrary Node IDs**: Protocol rejects any parameter other than strict boolean `muted`.

---

## 8. Adversarial Test Matrix

| Area | Suite | Tests | Result |
|---|---|---|---|
| Telemetry Readers & Models | `test_telemetry_readers.py` | 45 | PASS |
| Telemetry Service & Lifecycle | `test_telemetry_service.py` | 38 | PASS |
| Telemetry IPC & Events | `test_telemetry_ipc.py` | 6 | PASS |
| WpctlRunner & Parsers | `test_microphone_wireplumber.py` | 20 | PASS |
| Microphone Service & Set/Verify | `test_microphone_service.py` | 19 | PASS |
| Microphone IPC & Events | `test_microphone_ipc.py` | 11 | PASS |
| Stage 12.2 Security Regression & Fuzzing | `test_stage12_2_regression.py` | 9 | PASS |
| Shell Integration & QML Static | `shell/tests/test_notch.py` | 16 | PASS |
| **Total Focused Stage 12.2 Tests** | | **164** | **ALL PASS** |

---

## 9. Verification Gates

| # | Gate | Command | Result |
|---|---|---|---|
| 1 | Focused Telemetry Tests | `.venv/bin/python -m pytest tests/test_telemetry_*.py -q` | **89 passed** |
| 2 | Focused Microphone Tests | `.venv/bin/python -m pytest tests/test_microphone_*.py -q` | **50 passed** |
| 3 | Regression & Fuzzing | `.venv/bin/python -m pytest tests/test_stage12_2_regression.py -q` | **9 passed** |
| 4 | Full Backend Test Suite | `.venv/bin/python -m pytest tests/ -q` | **769 passed, 176 subtests** (58.2s) |
| 5 | Shell Test Suite | `.venv/bin/python -m pytest shell/tests/ -q` | **16 passed** (15.0s) |
| 6 | Qt6 QML Lint | `qmllint shell/**/*.qml` | **Exit 0** (zero errors) |
| 7 | Desktop Health Check | `./scripts/desktop-health.sh` | **9/9 PASS** |
| 8 | Git Whitespace Check | `git diff --check` | **Exit 0** |
| 9 | Python Compilation | `python -m py_compile ...` | **ALL COMPILED OK** |
| 10 | Live Hardware State | `wpctl get-volume @DEFAULT_AUDIO_SOURCE@` | **Verified Unmuted (0.38)** |

---

## 10. Known Limitations

1. **Default Audio Source Only**: Does not mute monitor sources, loopbacks, or secondary capture hardware.
2. **KIVO PTT Path**: KIVO uses `sounddevice` which binds to the PortAudio default; while this typically follows the system default, WirePlumber node mute is not a guaranteed driver-level privacy gate for PTT. This caveat is explicitly displayed in the UI.
3. **No Unsolicited Unmute**: External unmutes (via pavucontrol or other apps) are reflected on the next poll cycle (~5s).
