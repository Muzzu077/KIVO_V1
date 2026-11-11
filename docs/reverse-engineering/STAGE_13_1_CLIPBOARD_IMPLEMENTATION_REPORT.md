# KIVO — Stage 13.1 Clipboard Intelligence Implementation Report

```
Document:   Stage 13.1 Clipboard Intelligence Implementation Report
Status:     Complete
Date:       2026-11-09
Baseline:   HEAD 1062c0d; Stage 12.2 and Stage 13.0 completed
Scope:      Clipboard history, search, privacy filtering, persistent pins, IPC protocol
Environment: Arch Linux, niri 26.04 (Wayland), wl-clipboard 2.3.0, cliphist 0.7.0
```

---

## 1. Architecture Overview

Stage 13.1 implements KIVO's **Clipboard Intelligence** subsystem (`kivo/clipboard/`) without spawning competing clipboard daemons or directly mutating BoltDB storage.

```
┌────────────────────────────────────────────────────────────────────────┐
│ Wayland Compositor (niri 26.04)                                         │
│  Active daemon: wl-paste --watch cliphist store (PID 2466)             │
│  Stores all selections in ~/.cache/cliphist/db (BoltDB, ~67 MB)        │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ inotify modification detection (2s)
┌───────────────────────────────────▼────────────────────────────────────┐
│ ClipboardService (kivo/clipboard/service.py)                           │
│  • CliphistRunner (run_bounded: list, decode, copy, delete, wipe)      │
│  • ClipboardPrivacyFilter (passwords, JWTs, private keys, entropy)     │
│  • PinStore ($XDG_DATA_HOME/kivo/clipboard_pins.json, mode 0600)       │
│  • In-memory ring buffer (bounded max 100 items)                       │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ on_history_changed callback
┌───────────────────────────────────▼────────────────────────────────────┐
│ KIVO IPC Server ($XDG_RUNTIME_DIR/kivo/kivo.sock, mode 0600)           │
│  • Operations: clip_get_history, clip_get_item, clip_copy_item,        │
│                clip_pin_item, clip_delete_item, clip_clear_history     │
│  • Event: clip_history_changed                                         │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Upstream cliphist Integration & Persistence Limitations

### 2.1 The Architectural Reality of cliphist on Host
During Phase 1 inspection, we verified:
1. `niri` launches `wl-paste --watch cliphist store` at startup.
2. `cliphist 0.7.0` has **no built-in content exclusion or regex filters**. It stores any clipboard update directly to `~/.cache/cliphist/db` before any downstream application can inspect it.
3. Therefore:
   - **KIVO's Privacy Filter operates downstream** when reading and presenting history to KIVO's UI and IPC clients.
   - KIVO strictly prevents detected secrets from entering KIVO's persistent pin storage, prevents secrets from being shared remotely, and masks them as `••••••••••••` in previews.
   - **However, KIVO cannot guarantee that sensitive text was never written to the host's existing `~/.cache/cliphist/db` by the upstream `cliphist` process.**
4. **Changes Required for Pre-Persistence Filtering**:
   - For genuine zero-trace secret retention, the system-level watcher in `~/.config/niri/config.kdl` would need to pipe clipboard data through an upstream filter (e.g. `wl-paste --watch kivo-clip-filter | cliphist store`) or use a custom privacy-filtering clipboard daemon.
   - In accordance with non-negotiable development rules, KIVO did NOT modify `~/.config/niri/config.kdl` or replace the user's running daemon.
   - KIVO explicitly documents this boundary and does not advertise complete host-level secret elimination.

---

## 3. Privacy Filter Implementation (`kivo/clipboard/privacy.py`)

`ClipboardPrivacyFilter` implements multi-stage heuristic analysis:

1. **MIME Metadata Inspection**:
   - Matches hints from password managers: `x-kde-passwordManagerHint: secret`, `application/x-secret`.
2. **High-Confidence Credential Regex Patterns**:
   - Private keys: `-----BEGIN [A-Z ]*PRIVATE KEY-----`
   - JWT tokens: `eyJ...` (three base64url sections)
   - Cloud & Git tokens: `AKIA...`, `ghp_...`, `github_pat_...`, `glpat-...`, `xoxb-...`
   - Authentication headers: `Bearer [A-Za-z0-9._~+/-]{24,}`
   - Embedded basic auth: `https://user:password@host`
   - Labeled secrets: `password:`, `secret =`, `api_key:`
3. **Entropy Analysis with Confidence Levels**:
   - Single-token strings with length 20–80 chars, Shannon entropy > 4.5, and mixed character classes (uppercase, lowercase, digits, symbols) are marked `PrivacyLevel.SUSPICIOUS`.
   - Ordinary sentences or normal code snippets are preserved as `PrivacyLevel.NORMAL`.
4. **Actions on Sensitive Entries**:
   - `is_sensitive = True`.
   - Previews masked as `••••••••••••`.
   - Content excluded from `PinStore` (calling `pin_item()` on a sensitive entry raises `ValueError`).
   - `clip_get_item` returns masked text content.
   - Raw values are never written to logs or telemetry.
   - Completely excluded from any AI model prompt context.

---

## 4. Pin Management (`kivo/clipboard/pin_store.py`)

- **File Path**: `$XDG_DATA_HOME/kivo/clipboard_pins.json` (fallback `~/.local/share/kivo/clipboard_pins.json`).
- **File Permissions**: Created and replaced atomically with mode `0600` in directory mode `0700`.
- **Integrity & Fault Tolerance**:
  - Writes through a temporary file `.clipboard_pins_<random>.tmp` with `os.replace`.
  - Corrupt or invalid JSON files fail safely to an empty list without crashing.
  - Maximum 100 pins enforced.
  - Sensitive items are strictly rejected.

---

## 5. IPC Protocol & Handler Contracts

### 5.1 Operations

| Operation | Dotted Alias | Parameters | Result | Notes |
|---|---|---|---|---|
| `clip_get_history` | `clip.get_history` | `{"limit": int, "search": str, "filter": str}` (all optional) | `{"items": [...], "total_count": int}` | `limit` in `[1, 100]`; `filter` in `("all", "text", "image", "pinned")`. Sensitive items masked. |
| `clip_get_item` | `clip.get_item` | `{"id": str}` | `{"ok": bool, "item": {...}}` | Decodes content up to 128 KiB; sensitive items masked. |
| `clip_copy_item` | `clip.copy_item` | `{"id": str}` | `{"ok": bool}` | Decodes item and sets Wayland clipboard via `run_bounded(["wl-copy"])`. |
| `clip_pin_item` | `clip.pin_item` | `{"id": str, "pin": bool}` | `{"ok": bool}` | Pinned state saved to `clipboard_pins.json`. Sensitive items rejected. |
| `clip_delete_item` | `clip.delete_item` | `{"id": str}` | `{"ok": bool}` | Deletes from cliphist and unpins. |
| `clip_clear_history` | `clip.clear_history` | None or `{}` | `{"ok": bool}` | Explicit user action only: wipes cliphist history. Pinned items preserved. |

### 5.2 Events

- `clip_history_changed` (`clip.history_changed`): Emitted whenever clipboard history changes (on external copy detected via database mtime, or after a KIVO mutation). Payload: `{"count": int}`.

---

## 6. Lifecycle & Resource Ownership

- **Single Daemon Thread**: `kivo-clipboard` runs in background, checking database `mtime` with a 2-second sleep deadline.
- **Zero Polling Storm**: `os.stat()` takes 0.002 ms.
- **Orderly Shutdown**: `clipboard.close(2.0)` stops the watcher thread before the IPC server closes.
- **Zero Orphaned Processes**: Verified with `pgrep wl-copy` and `pgrep cliphist`.

---

## 7. Security Review

- ✅ **No Agent Tool Registration**: Clipboard operations are strictly for user desktop UI; never registered as LLM agent tools.
- ✅ **No Arbitrary Shell Execution**: `CliphistRunner` uses fixed paths, `run_bounded()`, argv lists, and `shell=False`.
- ✅ **No Direct BoltDB Manipulation**: Avoids database corruption; accesses BoltDB strictly through cliphist CLI.
- ✅ **No Unbounded Decoding**: Metadata list caps at 100 items; text decode caps at 128 KiB; images cap at 5 MiB.
- ✅ **No Path Traversal**: `id` parameters strictly validated with regex `^[A-Za-z0-9_.-]+$`.
- ✅ **No Raw Secrets in Logs**: Previews and debug output mask detected credentials.

---

## 8. Verification Results

All 10 verification gates passed cleanly:

1. **Focused Clipboard Tests**:
   - `pytest tests/test_clipboard_*.py -v` → **37 passed** in 4.78s
     - `ClipboardPrivacyFilterTests`: 8 passed
     - `ClipboardIPCTests`: 14 passed
     - `PinStoreTests`: 3 passed
     - `ClipboardServiceTests`: 12 passed
2. **Regression & Security Tests**:
   - `pytest tests/test_stage12_2_regression.py -v` → **10 passed** in 0.67s
3. **Combined Stage 12 + 13 Focused Suite**:
   - `pytest tests/test_telemetry_*.py tests/test_microphone_*.py tests/test_clipboard_*.py tests/test_stage12_2_regression.py -q` → **185 passed** in 22.3s
4. **Full Backend Test Suite**:
   - `pytest tests/ -q` → **806 passed, 176 subtests passed** in 61.4s (0 failures)
5. **Shell Test Suite**:
   - `pytest shell/tests/ -q` → **16 passed** in 15.1s
6. **Qt6 QML Lint**:
   - `/usr/lib/qt6/bin/qmllint shell/**/*.qml` → **Exit 0** (zero errors)
7. **Desktop Health Check**:
   - `./scripts/desktop-health.sh` → **9/9 PASS**
8. **Git Whitespace & Syntax**:
   - `git diff --check` → **Exit 0**
9. **Python Compileall**:
   - `py_compile` all files → **ALL COMPILED OK**
10. **Hardware State**:
    - Host microphone remains **unmuted** (`Volume: 0.38`).
    - Existing cliphist daemon (PID 2466) remains **untouched and running**.
    - No niri configuration was changed.
    - No commit or push occurred.

---

**Verdict: PASS WITH LIMITATIONS**
*(Limitation: As documented in Section 2, KIVO enforces downstream privacy filtering, preview masking, and pin exclusion; however, the host's existing `wl-paste --watch cliphist store` daemon persists raw clipboard entries into `~/.cache/cliphist/db` before KIVO can inspect them.)*
