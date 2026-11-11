# KIVO — Stage 13 Runtime Environment Inventory

```
Document:   Stage 13.0 Runtime Environment Inventory
Status:     Verified against host environment
Date:       2026-11-09
Target:     Local Productivity Utilities (Clipboard, File Shelf, Phone-to-PC Bridge)
Host:       Arch Linux (kernel 7.2.14-arch1-1), Wayland, niri 26.04
```

---

## 1. Operating System & Compositor Baseline

| Property | Value | Verification Command / Source |
|---|---|---|
| **OS Distribution** | Arch Linux (rolling) | `/etc/os-release` |
| **Kernel Version** | `7.2.14-arch1-1` | `uname -r` |
| **Compositor** | `niri 26.04 (8ed0da4)` | `niri --version` |
| **Display Protocol** | Wayland exclusively (`WAYLAND_DISPLAY=wayland-1`) | `echo $WAYLAND_DISPLAY` |
| **Session Type** | `XDG_SESSION_TYPE=wayland` | `echo $XDG_SESSION_TYPE` |
| **User Runtime Dir** | `/run/user/1000` (`mode 0700`) | `echo $XDG_RUNTIME_DIR` |
| **User Config Home** | `/home/muzzu/.config` | `echo $XDG_CONFIG_HOME` |
| **User Cache Home** | `/home/muzzu/.cache` | `echo $XDG_CACHE_HOME` |
| **User Data Home** | `/home/muzzu/.local/share` | `echo $XDG_DATA_HOME` |

---

## 2. Clipboard Subsystem

### 2.1 Installed Binaries & Capabilities

| Binary | Version | Path | Capabilities & Role |
|---|---|---|---|
| `wl-copy` | `2.3.0` | `/usr/bin/wl-copy` | Sets Wayland clipboard / primary selection from stdin. Supports explicit MIME types (`--type`), transient paste, and clear. |
| `wl-paste` | `2.3.0` | `/usr/bin/wl-paste` | Reads Wayland clipboard. `--list-types` queries available MIME types; `--watch <cmd>` executes command on selection change via `wlr-data-control`. |
| `cliphist` | `0.7.0` | `/usr/bin/cliphist` | Go-based clipboard history manager utilizing an embedded BoltDB database. Supports `store`, `list`, `decode`, `delete`, `delete-query`, `wipe`. |

### 2.2 Active User Session Integration

- **Compositor Startup Script**: Inspected `~/.config/niri/config.kdl`:
  ```kdl
  spawn-at-startup "wl-paste" "--watch" "cliphist" "store"
  ```
- **Active Process**: Verified live daemon running under user session:
  ```
  muzzu  2466  0.0  0.0  3092  2196 ?  S  14:34  0:00 wl-paste --watch cliphist store
  ```
- **Database Path**: `/home/muzzu/.cache/cliphist/db` (format: BoltDB, observed size: ~65 MB).
- **Execution Performance**:
  - `cliphist list | head -n 1 | cliphist decode`: **~3 ms** per read.
  - Zero polling storm: `wl-paste --watch` is event-driven by Wayland compositor selection changes.
- **MIME Types Observed on Host**:
  ```
  text/plain
  text/plain;charset=utf-8
  UTF8_STRING
  text/html
  chromium/x-source-url
  chromium/x-internal-source-rfh-token
  image/png (for copied screenshots / images)
  ```
- **Cliphist Wire Format**:
  - `cliphist list` outputs tab-separated `<id>\t<preview>`.
  - Image/binary clips produce previews formatted as `[[ binary data <size> <mime> <dimensions> ]]`.
  - `cliphist decode <line>` receives the exact list entry on stdin and outputs the raw stored bytes (UTF-8 text or binary PNG/JPEG).
  - `cliphist delete <line>` deletes a single item from BoltDB.
  - `cliphist wipe` clears the entire database.

### 2.3 Clipboard Limitations & Constraints

1. **Wayland Sandbox Isolation**: Regular Wayland client surfaces cannot read clipboard content unless focused or using privileged data-control protocols (`wlr-data-control-unstable-v1` or `ext-data-control-v1`).
2. **Secret Leakage in Default cliphist**: `cliphist store` currently captures all copies indiscriminately. Passwords copied from browsers or password managers enter `~/.cache/cliphist/db` unencrypted unless explicitly filtered by KIVO.
3. **Primary Selection vs Regular Clipboard**: Default is the standard clipboard (`Ctrl+C` / `Ctrl+V`). Primary selection (middle-click) is distinct and should be excluded from long-term history by default to avoid tracking casual text highlights.

---

## 3. Filesystem, Workspace & Drag-and-Drop Baseline

### 3.1 Existing KIVO Filesystem Architecture

- **`kivo/files/workspace.py`**:
  - Manages approved workspace roots configured in `$XDG_CONFIG_HOME/kivo/workspaces.json`.
  - Strict enforcement: no implicit home directory or root access.
  - Workspaces are read-only by default; mutation requires explicit `"mutation_enabled": true`.
- **`kivo/files/service.py` (`FileIntelligenceService`)**:
  - File-descriptor-based traversal (`O_NOFOLLOW | O_DIRECTORY`).
  - Path traversal and symlink escape prevention.
  - Filesystem mount boundary enforcement (device ID and mount ID verification).
  - Sensitive file heuristics (blocks `.env`, `.ssh`, private keys, certificates, password stores).
- **`kivo/files/mutation_service.py`**:
  - Atomic copy, move, delete, write within approved workspace roots.
  - Audit logging of all mutations.

### 3.2 Desktop Drag-and-Drop Environment

- **Quickshell 0.3.2 Support**:
  - Built against Qt 6.12.0.
  - QtQuick provides native `DropArea` with `onDropped: (drop) => {}`.
  - When files are dragged from Wayland file managers (Nautilus, Dolphin, Thunar), `drop.urls` yields `file:///path/to/file` URI lists.
  - `drop.formats` contains `["text/uri-list", "text/plain"]`.
- **Storage Subsystem**:
  - Root filesystem: `/dev/nvme0n1p8` mounted on `/` (ext4).
  - Available blocks: ~19 GB free, total ~157 GB (`os.statvfs("/")`).

---

## 4. Local Networking & Cryptographic Capabilities

### 4.1 Python Dependencies in `.venv`

Verified installed in Python 3.14 virtual environment without needing new packages:

| Package | Version | Import Spec | Intended Role in Phone-to-PC Bridge |
|---|---|---|---|
| `cryptography` | `46.0.5` | `cryptography.x509`, `hazmat` | TLS certificate generation, key exchange, SHA-256 streaming hashes |
| `qrcode` | `8.2` | `qrcode.QRCode` (Pillow PIL) | Generating desktop QR code for phone camera pairing |
| `PIL` (Pillow) | `12.1.1` | `PIL.Image` | In-memory rendering of QR code image for QML display |
| `fastapi` | `0.135.1` | `fastapi.FastAPI` | Authenticated local-network HTTP/WebSocket endpoints |
| `uvicorn` | `0.44.0` | `uvicorn.Server`, `Config` | Running ephemeral ASGI server bound strictly to LAN IP |
| `websockets` | `16.0` | `websockets` | Real-time transfer progress and pairing handshake |
| `httpx` | `0.28.1` | `httpx` | Local loopback probes and internal client testing |
| `psutil` | `7.2.2` | `psutil` | Network interface IP discovery and connection tracking |

### 4.2 Network Interfaces Verified on Host

| Interface | Type | Operational State | IP Address / Subnet | Role |
|---|---|---|---|---|
| `lo` | Loopback | `UNKNOWN` (active) | `127.0.0.1/8`, `::1/128` | Local IPC only; not for phone access |
| `wlp0s20f3` | Wi-Fi (Intel AX211) | `UP` | `192.168.1.7/24` | **Target interface for Phone Bridge** |
| `enp109s0` | Ethernet | `DOWN` | None | Inactive |
| `docker0`, `br-*` | Virtual Docker | Mixed | `172.17.0.1/16` - `172.21.0.1/16` | **Explicitly excluded** from pairing discovery |

**Crucial Networking Constraint**:
The Phone Bridge daemon must dynamically bind to the default gateway's active subnet IP (`192.168.1.7`), **never to `0.0.0.0`** (which would expose the port on public interfaces or container bridges).

---

## 5. Reusable KIVO Components

1. **`kivo/core/bounded_process.py` (`run_bounded`)**:
   - Fixed argv arrays, strict timeout, capped stdout/stderr, process-group `killpg(SIGKILL)`.
   - Directly reusable for `wl-copy`, `cliphist list`, `cliphist decode`, `cliphist delete`, and `xdg-open`.
2. **`kivo/ipc/server.py` & `protocol.py`**:
   - Unix domain socket server (`$XDG_RUNTIME_DIR/kivo/kivo.sock`).
   - Line-delimited JSON v1 framing (64 KiB cap).
   - Dynamic event broadcasting and subscription filtering.
3. **`shell/services/KivoIpc.qml`**:
   - Single authoritative connection between Quickshell and backend.
   - Reconnect resync counters and stale event handling.
4. **`shell/components/DynamicNotch.qml`**:
   - Idle width adaptation, expandable drawer system, theme tokens.
5. **`shell/styles/Theme.qml`**:
   - Palette (`surfaceSunken`, `surfaceRaised`, `border`, `accent`, `mint`, `danger`, `warning`).
   - Typography (`fontFamily: Noto Sans`, `monoFamily: JetBrainsMono Nerd Font`, micro/caption sizes).
