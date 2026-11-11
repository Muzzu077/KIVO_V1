# KIVO — Stage 12.2 Threat Model

```
Document:   Stage 12.2.0 threat model
Status:     Proposed; accompanies the Stage 12.2 architecture specification
Date:       2026-11-09
Baseline:   HEAD 1062c0d
Scope:      Read-only hardware telemetry; WirePlumber default microphone mute;
            typed IPC; Dynamic Notch/Dashboard presentation
Out of scope: Recording, audio capture, transcription, AI/agent access,
              PipeWire/WirePlumber configuration management, system-wide mute
```

This document treats telemetry and microphone state as security-relevant. The
telemetry side is mostly a privacy and integrity problem. The microphone side
is a privacy and availability problem with a state-changing subprocess at its
core.

---

## 1. Security objectives

1. **No audio exposure.** KIVO must never capture, buffer, transmit, or log
   microphone samples as part of this feature.
2. **No unintended unmute.** Only an explicit, verified user action may unmute
   the default source. There is no automatic unmute on startup, PTT, task
   completion, reconnect, or shutdown.
3. **Truthful state.** The UI displays a muted state only after a backend read
   verifies it. A dispatched `wpctl` command is not proof of success.
4. **Bounded authority.** The microphone service can change exactly one
   property (mute) on exactly one resolved node (the default source). It cannot
   change routes, profiles, volume, or the default device.
5. **No arbitrary execution.** No IPC parameter reaches a shell, a command
   name, a path, or an unbounded subprocess.
6. **Telemetry privacy.** The snapshot contains hardware metrics only: no
   process list, command lines, usernames, hostname, serial numbers, UUIDs,
   MAC addresses, SSIDs, or file paths beyond the fixed root mount.
7. **Availability.** A missing sensor, GPU, `wpctl`, or WirePlumber degrades one
   capability; it does not crash or block the backend.
8. **No agent escalation.** Neither telemetry nor microphone control is
   registered as an agent tool or exposed to a model provider in this stage.

---

## 2. Assets and data classification

| Asset | Classification | Why it matters |
|---|---|---|
| Microphone mute state | Privacy-sensitive | A false “muted” claim can expose speech; a false “live” claim erodes trust |
| Default source identity | Local hardware metadata | Device names can fingerprint hardware; display names are untrusted text |
| Telemetry snapshot | Low-sensitivity system data | Aggregated load/temperature/battery; still reveals usage patterns |
| IPC socket | Local control channel | Same-user processes can invoke operations |
| `wpctl` process | State-changing capability | The only component that writes to the audio stack |
| Backend lifecycle | Availability | A hung subprocess or thread must not block shutdown |
| Logs | Operationally sensitive | Must not contain audio, secrets, raw command output, or unbounded device names |

Explicitly **not collected**: audio samples, transcripts, per-process data,
open file lists, environment variables, credentials, serial numbers, GPU UUIDs,
battery serial/model/manufacturer, hostname, IP/MAC addresses, Wi-Fi SSIDs.

---

## 3. Trust boundaries

```
┌─────────────────────────────────────────────────────────────────────┐
│ Kernel / firmware                                                    │
│  /proc/stat, /proc/meminfo, hwmon, thermal, power_supply, statvfs    │
│  NVIDIA driver / NVML / nvidia-smi                                   │
│  PipeWire / WirePlumber / wpctl                                      │
└───────────────┬─────────────────────────────────────────────────────┘
                │ parsed as untrusted input
┌───────────────▼─────────────────────────────────────────────────────┐
│ KIVO backend (same user)                                             │
│  TelemetryService, MicrophoneService, IPCHandlers                    │
└───────────────┬─────────────────────────────────────────────────────┘
                │ AF_UNIX, 0600, protocol v1, 64 KiB frames
┌───────────────▼─────────────────────────────────────────────────────┐
│ Quickshell/QML (same user)                                           │
│  KivoIpc.qml, DynamicNotch.qml, Dashboard pages                      │
└───────────────┬─────────────────────────────────────────────────────┘
                │ pixels / input
┌───────────────▼─────────────────────────────────────────────────────┐
│ User                                                                 │
└─────────────────────────────────────────────────────────────────────┘

No path: QML/service → agent/LLM → microphone or telemetry.
```

### Boundary notes

- The kernel and sysfs are trusted for *availability*, not for *well-formed
  values*. Firmware can expose absurd temperatures, negative counters, or
  changing labels. Parsers must validate ranges.
- `wpctl` output is untrusted text. Device names come from hardware/firmware
  and can contain control characters, bidi overrides, or misleading Unicode.
- The IPC socket is same-user only (`0600` in a `0700` directory). It is not an
  authentication boundary against a compromised same-user process; it is a
  boundary against other users and accidental cross-process access.
- QML is presentation. It must not be able to cause arbitrary commands by
  constructing parameters; the protocol schema is closed.

---

## 4. Actors and capabilities

| Actor | Capability | Relevant threat |
|---|---|---|
| Local user | Can click UI, run `wpctl`, change audio settings | Legitimate control; accidental rapid toggles |
| Same-user malicious process | Can open the KIVO socket, run `wpctl` directly, read `/proc` | IPC abuse is not an escalation; rate limits still matter |
| Compromised QML/renderer | Can send IPC requests as the user | Must not gain command execution or unbounded polling |
| Malicious/faulty firmware or device | Supplies sensor values and device names | Parse/range/display-name attacks |
| Malicious `wpctl` binary or PATH entry | Executes before KIVO resolves it | Command substitution, false state |
| Accidental bug | Race, stale cache, wrong source | False mute claim, wrong-device mute |
| Future agent integration | Could call tools if registered | Explicitly prohibited in this stage |

**Important boundary statement:** a same-user attacker can already run
`wpctl set-mute @DEFAULT_AUDIO_SOURCE@ 0` without KIVO. KIVO's IPC does not
claim to defend the microphone against the account owner or code running as
that user. It defends against cross-user access, accidental UI lies, command
injection, resource exhaustion, and future agent misuse.

---

## 5. Threat enumeration

### T1 — Unauthorized unmute through IPC

**Scenario:** A local process opens the KIVO socket and sends
`mic_set_mute {"muted": false}` to turn the microphone back on.

**Impact:** Privacy loss if the user believed the microphone was muted.

**Likelihood:** Low as a cross-user attack (socket is 0600); medium as a
same-user compromised process.

**Mitigations:**

- Socket permissions and runtime-directory ownership checks already exist.
- The operation requires an explicit boolean; there is no `toggle` or
  “unmute on any request” path.
- No agent tool, no model access, no HTTP endpoint.
- The UI reflects only verified state; an unmute event is visible.
- Log the operation outcome (without audio) so the change is attributable.
- No automatic unmute on startup, PTT, reconnect, or shutdown.

**Residual:** A same-user process can call the IPC or `wpctl` directly. KIVO
cannot prevent this without a separate privilege boundary, which is out of
scope.

### T2 — Unauthorized mute (denial of capture)

**Scenario:** A process mutes the default source, causing other applications
to lose input.

**Impact:** Availability loss; confusion during calls or recording.

**Mitigations:** Same as T1; explicit state; visible UI; no hidden mute; rate
limiting; no agent tool. The user can always unmute from the dashboard.

**Residual:** Same-user attacker can mute directly.

### T3 — Command injection through source identity or IPC parameters

**Scenario:** A malicious device name or IPC field is concatenated into a
shell command.

**Impact:** Arbitrary code execution as the user.

**Mitigations:**

- No shell. `subprocess.Popen(argv_list, shell=False)`.
- The only variable argv element is a validated decimal node ID or the
  literals `"1"`/`"0"`.
- `mic_set_mute` accepts exactly one boolean key; unknown keys are rejected at
  the protocol layer.
- No `source`, `device`, `command`, `path`, `action`, `toggle`, or `force`
  parameter exists.
- Device names are display-only and never used to select the node; the numeric
  ID from the parser is used.
- Node names are validated against `[A-Za-z0-9_.:-]{1,255}` before acceptance.

**Residual:** A compromised `wpctl` binary can do anything the user can; see
T5.

### T4 — PATH hijack / malicious `wpctl`

**Scenario:** `PATH` points to a user-writable `wpctl` that returns fake state
or executes a payload.

**Impact:** False mute state or code execution as the user.

**Mitigations:**

- Resolve `wpctl` once at service construction with `shutil.which`.
- Require an absolute path and an executable regular file.
- Do not re-resolve per call.
- Use the resolved absolute path in every argv.
- Treat all output as untrusted and verify with a second read.

**Residual:** If `PATH` is already attacker-controlled when the backend starts,
the attacker can substitute the binary. The same attacker can run code
directly. A future hardening could require a root-owned, non-world-writable
binary; that may break non-standard installs and is deferred.

### T5 — Malicious or malformed `wpctl` output

**Scenario:** `wpctl` returns empty output, a fake `[MUTED]` marker, control
characters, a huge line, or a contradictory state.

**Impact:** The UI claims muted when it is not, or displays spoofed text.

**Mitigations:**

- Bounded stdout/stderr and timeout; kill on overflow.
- Strict regex for `Volume: <number> [MUTED]`.
- Empty stdout is a failure even with exit 0 (observed `wpctl` behaviour).
- Post-operation verification is mandatory; the set command is never trusted.
- The target node ID is pinned before set and verified after set.
- Source identity is compared before/after; a change produces
  `default_source_changed`.
- Display names are sanitized (C0/C1, bidi controls, length cap).
- No `eval`, no `shlex`, no shell, no parsing by delimiter that a device name
  can shift.

**Residual:** A malicious `wpctl` that consistently lies across both reads can
fool KIVO. At that point the user's audio stack is compromised; KIVO cannot
attest it without a trusted broker (out of scope).

### T6 — Exit-code false success (verified finding)

**Scenario:** `wpctl get-volume <missing-id>` exits 0 with empty stdout and
`Node '…' not found` on stderr.

**Impact:** A naive implementation marks the state as read or the operation as
successful.

**Mitigations:**

- Require zero exit **and** non-empty, parseable stdout.
- Capture stderr separately and treat a connection/not-found message as
  `invalid_output`/`unavailable`.
- Post-set verification is mandatory.
- Tests explicitly cover exit 0 + empty stdout.

This is a verified host behaviour, not a hypothetical.

### T7 — IPC flood / fork storm

**Scenario:** A client sends hundreds of `mic_set_mute` requests or
`telemetry_get_snapshot` requests to exhaust processes or CPU.

**Impact:** Resource exhaustion, audio stack churn, UI lag.

**Mitigations:**

- Telemetry get returns a cached snapshot; no I/O, no client-controlled
  refresh.
- Mic get uses a 2 s cache; repeated calls do not force faster polling.
- Mic set is serialized by one lock; concurrent calls get `busy`.
- 250 ms rate limit between set operations.
- Fixed 5 s/30 s polling cadence.
- Existing per-client bounded event queues and frame limits.
- QML disables the control while transitioning and times out at 5 s.
- No `toggle`, so a flood cannot alternate state unpredictably.

**Residual:** A same-user process can still open many connections and send
valid reads. The existing server limits and OS scheduling apply; a future
per-connection rate limit could be added if observed.

### T8 — Subprocess hang or pipe deadlock

**Scenario:** `wpctl` or `nvidia-smi` hangs, writes a huge stream, or leaves a
pipe open.

**Impact:** A backend thread blocks; shutdown stalls; memory grows.

**Mitigations:**

- Bounded `Popen` with `stdin=DEVNULL`.
- Selector-based or bounded `communicate`-style read with a hard deadline.
- Kill and `wait` on timeout; close pipes and selector in `finally`.
- Output caps (16 KiB stdout, 4 KiB stderr for `wpctl`; 16 KiB/4 KiB for
  `nvidia-smi`).
- Service-level join timeouts (2 s telemetry, 3 s mic).
- UI client timeouts.
- Per-cycle exception handling; one failure does not kill the thread.

### T9 — Telemetry privacy leakage

**Scenario:** The snapshot accidentally includes process names, command lines,
usernames, hostname, serial numbers, UUIDs, MAC addresses, or SSIDs.

**Impact:** Local usage and hardware fingerprinting; violation of the
“no per-process surveillance” requirement.

**Mitigations:**

- Fixed schema allowlist; tests assert exact keys.
- Only `/proc/stat`, `/proc/meminfo`, CPU frequency sysfs, selected hwmon
  paths, power-supply capacity/status/online, `statvfs("/")`, and GPU
  name/temperature/utilization/memory.
- No `/proc/<pid>`, no `psutil.Process`, no process iteration.
- No hostname, username, environment, network interfaces, serial numbers,
  battery model/manufacturer, cycle count, GPU UUID, or PCI IDs.
- Storage mount is fixed `/`; no arbitrary path or file listing.
- Temperature `source` fields are fixed, discovered sysfs identifiers (for
  example `hwmon:coretemp/temp1_input`), not user file paths. They are
  allowlisted, sanitized, and capped.
- Temperature `source` strings are fixed, discovered sysfs identifiers such as
  `hwmon:coretemp/temp1_input`, not user file paths. They are allowlisted,
  sanitized, and capped.
- No raw file contents or exception dumps in events.
- Telemetry is never sent to an agent or model.

### T10 — Prompt injection through device/sensor names

**Scenario:** A device or sensor label contains text that looks like an
instruction, and a future agent reads it.

**Impact:** Model manipulation or misleading behavior.

**Mitigations:**

- No telemetry/mic data is sent to a model in this stage.
- No agent tool is registered.
- The UI renders plain text, not rich text/HTML.
- Device names are sanitized and length-capped.
- A future agent integration requires a separate threat review and explicit
  data-minimization design.

### T11 — Stale data presented as live

**Scenario:** The telemetry thread stalls or a sensor stops updating, but the
UI keeps showing the last value.

**Impact:** Wrong decisions (e.g., battery, temperature), loss of trust.

**Mitigations:**

- Per-group `sampled_at`, `age_seconds`, and `stale` computed at serialization
  time.
- Thresholds: CPU/memory 3 s, frequency 5 s, temperatures 8 s, GPU 6 s,
  battery 15 s, storage 60 s.
- UI dims stale values and shows “stale N s”.
- Missing values are `null`, never `0`.
- `service_state="degraded"` when CPU/memory are stale or missing.
- On reconnect, values remain marked stale until a fresh get response.

### T12 — Default-device race

**Scenario:** The default source changes between resolve, set, and verify.

**Impact:** The wrong device is muted, or the UI reports the wrong device.

**Mitigations:**

- Resolve the default once, pin the numeric ID, set/verify that ID.
- Re-resolve after the operation and compare identity.
- `default_source_changed` is reported; the operation returns `ok=false` with
  `device_changed`, and a new event reflects the new default's actual state.
- The UI displays the source name alongside the state.
- No optimistic state hides the race.

**Residual:** If the default changes immediately after the post-check, the next
poll (≤5 s) catches it and emits an event.

### T13 — Shutdown during a set operation

**Scenario:** KIVO stops while `wpctl set-mute` is running.

**Impact:** The command may complete without a verification/event; the backend
may block on join.

**Mitigations:**

- Bounded command timeout (0.75 s per call, 3.75 s worst case).
- `close()` joins with a 3 s timeout.
- No compensating command, no automatic unmute, no event after close.
- The explicit desired state is the only write; a partially completed operation
  is idempotent on the next start/read.
- The UI disconnects and shows the shell's reconnect state.

**Residual:** The already-dispatched command can still change the device. This
is unavoidable without transactional audio control; KIVO does not pretend
otherwise.

### T14 — Multiple clients / conflicting requests

**Scenario:** Two UI clients request opposite states concurrently.

**Impact:** Flicker, last-writer ambiguity, wrong UI state.

**Mitigations:**

- One operation lock; the second request gets `busy`.
- One rate limit; no queue of stale intents.
- `revision` monotonically increases on verified changes.
- All clients receive the same event; responses and events carry the same
  revision.
- Clients ignore events older than their current revision.
- No optimistic UI.

### T15 — GPU tooling/driver failure

**Scenario:** NVML import fails, the driver unloads, `nvidia-smi` is missing,
or the output is malformed.

**Impact:** GPU card missing or wrong; telemetry thread churn; subprocess spam.

**Mitigations:**

- Optional import; failure is `unsupported`.
- NVML primary; `nvidia-smi` fallback only when NVML init fails, not on every
  transient query error.
- Re-init at most once per 60 s.
- Bounded subprocess with timeout/output caps.
- GPU failure never invalidates CPU/memory/battery/storage.
- UI hides an unsupported GPU card instead of showing `0`.

### T16 — Resource leaks

**Scenario:** Threads, file descriptors, NVML state, or subprocess pipes leak
across restarts or errors.

**Impact:** Slow degradation, shutdown hangs.

**Mitigations:**

- `close()` idempotent; join with timeout.
- `finally` closes pipes/selector; `proc.kill()` + `wait`.
- `nvmlShutdown()` once on close if initialized.
- Bounded collections and line counts.
- Tests assert thread exit, no events after close, and no orphan process.

### T17 — Log injection / sensitive logging

**Scenario:** A device name with newlines/control characters is logged, or raw
`wpctl` output is logged.

**Impact:** Forged log lines; hardware/usage leakage.

**Mitigations:**

- Log state transitions and error codes, not raw output.
- Sanitize names before logging (control/bidi stripped, length capped).
- Rate-limit repeated errors to once per 60 s.
- Never log audio, samples, transcripts, or credentials.
- No environment dumps.

### T18 — Dependency risk (`pynvml` shim)

**Scenario:** The `pynvml` distribution is deprecated and may diverge from
`nvidia-ml-py`.

**Impact:** Import warnings, future breakage, supply-chain confusion.

**Mitigations:**

- Import is optional and guarded.
- No new dependency; the installed `nvidia-ml-py` provides the module.
- The service degrades if the module is absent.
- A separate dependency cleanup can replace `pynvml` with `nvidia-ml-py` after
  this stage; it must not be mixed into a security-sensitive change.

### T19 — “System-wide mute” misconception

**Scenario:** The UI or documentation implies every microphone is muted, while
only the default source is.

**Impact:** Users believe they are private when another application captures
from a monitor, virtual source, or non-default device.

**Mitigations:**

- Feature name: “Default microphone mute”.
- UI label: “Default microphone”; source name shown.
- Documentation explicitly lists what is not controlled.
- No “all microphones” or “system-wide” wording.
- No green “private” badge that implies more than the verified node state.

### T20 — KIVO PTT not gated by the mute

**Scenario:** The user mutes the default source, then uses KIVO PTT. KIVO's
`sounddevice` stream may open a device that is not affected by the WirePlumber
node mute, or it may follow the system default depending on KIVO's selected
input; the feature cannot guarantee that PTT is gated.

**Impact:** The user believes KIVO cannot hear them, but KIVO may still be
able to capture through its own selected input.

**Mitigations:**

- The UI states that PTT uses KIVO's selected input and may be unaffected.
- The feature is not described as a PTT privacy control.
- KIVO does not change its PTT selection or capture behavior in this stage.
- A future stage may bind PTT to the default source and enforce mute, but that
  requires its own privacy review and is not part of 12.2.

This is the most important product-security limitation of the stage.

---

## 6. Subprocess attack surface

Only two external tools are used:

| Tool | Use | Variable input | Controls |
|---|---|---|---|
| `wpctl` | list sources, read volume/mute, set mute | Validated numeric node ID; literal `1`/`0` | Absolute path, argv array, 0.75 s timeout, 16 KiB/4 KiB caps, strict parse, verify |
| `nvidia-smi` | GPU fallback query | None (fixed query) | Absolute path, argv array, 2 s timeout, 16 KiB/4 KiB caps, strict CSV parse, NVML preferred |

Non-negotiable controls:

- `shell=False` (the default); never `shell=True`.
- No `os.system`, no `subprocess.call` with a string, no `eval`.
- `stdin=DEVNULL`.
- Resolve paths once; do not accept paths over IPC.
- Kill on timeout/output overflow; reap the process.
- Treat stdout/stderr as untrusted.
- Never interpolate device names into argv; use the numeric ID.
- Never use `toggle` as the sole set mechanism.
- Verify after set; do not trust exit codes alone.

Telemetry uses no subprocesses for CPU, memory, frequency, temperature,
battery, or storage. That is a deliberate reduction of attack surface.

---

## 7. Failure modes and security impact

| Failure | Security impact | Handling |
|---|---|---|
| Missing `/proc`/`/sys` | None; reduced visibility | `unavailable`, other groups valid |
| Bad sensor value | Misleading UI | Range validation → `error` |
| GPU tool missing | None | `unsupported`, no retry storm |
| NVML driver error | None | `error`, backoff, re-init ≤1/60 s |
| `wpctl` missing | Mic control unavailable | `wpctl_missing`, backend healthy |
| WirePlumber down | Mic state unknown | `unavailable`, backoff, recovery event |
| No default source | Mic control unavailable | `no_default_source` |
| Default changes | Wrong-device risk | Pin ID, post-check, `default_source_changed` |
| Set command fails | State may still change | Post-read, `ok=false`, verified actual |
| Verify fails | State unknown | `muted=null`, `verified=false`; UI never guesses |
| Rapid requests | Resource churn | Lock + 250 ms rate limit + `busy` |
| Shutdown during set | Partial operation | Bounded, no compensation, no event after close |
| Slow read | Stale UI | Per-group age/stale, dimmed UI |
| IPC disconnect | Stale UI | Reconnect + get resync |
| Multiple clients | Conflict | Serialized, revision ordering |
| Event queue full | Missed event | Existing bounded queue; get resync |
| Log flood | Disk/CPU | Rate-limited error logs |

---

## 8. Privacy analysis

### Data minimisation

The telemetry snapshot is intentionally narrow:

- CPU: model string, logical core count, utilization, frequency, package
  temperature.
- Memory: total/used/available/percent.
- GPU: name, utilization, temperature, memory used/total/percent.
- Temperatures: CPU package, NVMe, Wi-Fi.
- Battery: present, percent, normalized state, AC online.
- Storage: fixed root mount, total/used/available/percent.

No process-level data. No user files. No network identity. No hardware
serial/UUID. No battery serial/model/manufacturer/cycle count. No hostname.

### Microphone

- The service reads the default source's volume/mute line and identity.
- The node name can contain a local hardware path (for example a PCI address).
  It is displayed only in the microphone control, is not included in the
  telemetry snapshot, and is never sent to a model.
- It writes only the mute property.
- It never opens an audio stream, reads samples, or accesses the capture
  manager/ingress.
- It never sends audio to a model.
- It never unmutes automatically.
- It is not an agent tool.

### Retention and logging

- Snapshots and states live in memory only; no database, no file, no history.
- Logs contain state transitions, error codes, and sanitized source
  descriptions; no raw output, no samples.
- No telemetry history is persisted across restarts.

### User expectation

The UI must not imply that “default microphone muted” means “KIVO cannot hear
you” or “all capture is off”. The PTT note is mandatory when the selected KIVO
input differs from the default.

---

## 9. Residual risks and accepted limitations

| ID | Residual risk | Why accepted | Follow-up |
|---|---|---|---|
| R1 | Same-user process can control the mic directly or via IPC | Same-user code already has `wpctl`; IPC is not an auth boundary | Optional per-client auth in a future stage, not required now |
| R2 | Only the default source is muted | Scope decision to avoid silent system-wide behavior | A future “all sources” design must enumerate monitors/virtual sources and their risks |
| R3 | KIVO PTT may be unaffected by the mute | PTT uses `sounddevice`/PortAudio and may follow the system default; the mute is not a guaranteed PTT gate | UI disclosure now; a future privacy-gating stage could bind PTT to the verified default |
| R4 | External mute changes can take up to 5 s to appear | Polling avoids a persistent event stream/subprocess | Event-driven PipeWire integration later if needed |
| R5 | Default identity changes can take up to 30 s to appear | Identity poll is slower to reduce process churn | Immediate refresh after set/failure; 30 s is acceptable for hotplug |
| R6 | Muted `[MUTED]` output not observed on this host | Changing mic state was prohibited in 12.2.0 | Confirm with user consent in 12.2.2; fixture tests until then |
| R7 | A malicious `wpctl` that lies consistently | The audio stack is already compromised | A trusted broker is out of scope |
| R8 | `nvidia-smi` fallback adds a subprocess | Only a fallback when NVML is unavailable | NVML preferred; fallback disabled if absent |
| R9 | NVML is process-global | Current code has one user | Add refcounting before a second user |
| R10 | `pynvml` shim is deprecated | No dependency change in this stage | Separate cleanup to `nvidia-ml-py` |
| R11 | Notch height grows in idle expanded view | Only idle; media/dashboard remain usable | Measure on 1080p; reduce cards or scroll if needed |
| R12 | No authenticated UI identity | Socket permissions are the existing model | Out of scope; same-user boundary unchanged |

None of these residuals require a new privilege, service, or dependency. They
are documented so later stages do not accidentally widen the claim.

---

## 10. Security acceptance criteria

Stage 12.2.1 (telemetry):

- [ ] No writes to `/proc`, `/sys`, devices, or configuration.
- [ ] No per-process reads; schema allowlist test passes.
- [ ] No hostname/serial/UUID/MAC/SSID in the snapshot.
- [ ] Subprocess fallback (if used) is bounded and fixed-argv.
- [ ] Missing GPU/sensor does not invalidate the snapshot.
- [ ] Stale values are marked; missing values are `null`, not `0`.
- [ ] Thread stops within 2 s; no events after close.

Stage 12.2.2 (microphone):

- [ ] `wpctl` resolved once; absolute path; argv array; no shell.
- [ ] Only `list`, `get-volume`, and `set-mute` are invoked.
- [ ] `set-mute` uses explicit `1`/`0`; no `toggle`.
- [ ] Post-set verification is mandatory; exit 0 + empty stdout is a failure.
- [ ] Node ID is pinned and verified; default changes are reported.
- [ ] Rapid requests are serialized/rate-limited; no orphan processes.
- [ ] Shutdown never unmutes or compensates.
- [ ] No audio capture/import; no agent tool.
- [ ] On-hardware test records and restores the original state.

Stage 12.2.3 (IPC/UI):

- [ ] `mic_set_mute` accepts exactly a boolean; extra/wrong params rejected.
- [ ] No command/path/source parameter exists.
- [ ] Events only on verified changes; revision ordering enforced.
- [ ] UI never shows muted before verification.
- [ ] Transitioning/unavailable/error/stale are visually distinct.
- [ ] Reconnect resyncs and marks values stale until fresh.
- [ ] Multiple clients receive consistent verified state.
- [ ] Existing media/PTT/notch tests and desktop health 9/9 pass.
- [ ] `git diff --check` and QML lint pass.
- [ ] No commit/push in the current research task; future stages follow the
      KIVO local-commit workflow.

---

## 11. Adversarial validation plan

Use fakes; do not touch the live microphone in automated tests.

1. **Fake `wpctl` scripts**
   - exit 0, empty stdout;
   - exit 0, “Node not found” on stderr;
   - malformed `Volume:` line;
   - huge output (16 KiB boundary);
   - sleep past 0.75 s;
   - control/bidi characters in the source name;
   - contradictory pre/post states;
   - default source changes between calls.
2. **Fake `/proc`/`/sys` trees**
   - truncated `/proc/stat`;
   - counter reset and zero delta;
   - negative/huge/non-numeric temperatures;
   - missing battery capacity with `charge_*`/`energy_*` fallbacks;
   - disappearing cached sensor paths;
   - zero/oversized `statvfs` values.
3. **Fake NVML**
   - import failure;
   - init failure;
   - zero devices;
   - query exceptions;
   - out-of-range temperature/utilization/memory;
   - name as bytes and as a comma-containing string.
4. **IPC fuzzing**
   - `muted` as `1`, `0`, `"true"`, `null`, list, object, missing;
   - extra keys;
   - telemetry/mic get with non-empty params;
   - oversized frames;
   - duplicate keys;
   - unknown operations/events.
5. **Concurrency**
   - 50 parallel set requests;
   - assert one lock holder, deterministic `busy`/`rate_limited`, no orphan
     `wpctl`.
6. **Shutdown race**
   - close while a fake slow command is running;
   - assert bounded exit, no post-close event, no compensating command.
7. **UI**
   - fake backend sends muted/unmuted/unavailable/error/stale;
   - click mute and assert no state flip before the verified response;
   - reconnect and assert stale-until-resync.
8. **Manual, user-approved hardware check (12.2.2/12.2.3)**
   - record original state;
   - mute, verify `[MUTED]`, unmute, verify;
   - confirm default/route/volume unchanged;
   - restore the original state.

---

## 12. Threat model conclusion

The design keeps the microphone write surface to one property on one resolved
node, with strict argv construction, bounded execution, and mandatory
verification. Telemetry is read-only and data-minimised. The main residual
risks are inherent to the chosen scope: the feature is default-source-only,
KIVO's PTT path is separate and not guaranteed to be gated, and a same-user
process is already outside the IPC security boundary. Those risks are disclosed in the UI and documentation
rather than hidden behind a broader “microphone muted” claim.
