# KIVO — Stage 13 Implementation Roadmap: Local Productivity Utilities

```
Document:   Stage 13.0 Implementation Roadmap
Status:     Approved for Planning
Date:       2026-11-09
Baseline:   HEAD 1062c0d; Stage 12.2 completed
Structure:  Phased implementation across 4 independently testable milestones
```

---

## 1. Roadmap Overview & Sequencing

Stage 13 introduces three local productivity utilities to KIVO. To ensure zero regressions, strict test coverage, and modular delivery, the implementation is partitioned into four sequential stages:

```
┌─────────────────────────────────────────────────────────────┐
│ Stage 13.1 — Clipboard Intelligence                         │
│  Backend service, cliphist runner, privacy filter, pins, IPC│
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│ Stage 13.2 — File Shelf Staging                             │
│  Backend service, staging models, path security, IPC        │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│ Stage 13.3 — Phone-to-PC Bridge                             │
│  Ephemeral server, WebCrypto pairing, quarantine, transfers │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│ Stage 13.4 — Unified Dynamic Notch & Dashboard Integration  │
│  QML components, Notch drawers, Dashboard pages, E2E tests  │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. Milestone Details

### Stage 13.1 — Clipboard Intelligence

#### Objective
Implement an isolated, privacy-preserving clipboard history and management service integrating with the host's existing `cliphist` and `wl-copy`.

#### Deliverables
- `kivo/clipboard/__init__.py`: Package entry and exports.
- `kivo/clipboard/models.py`: Immutable `ClipItem`, `ClipType`, `ClipFilter`.
- `kivo/clipboard/filter.py`: Sensitive content heuristic engine (passwords, JWTs, private keys, entropy checks).
- `kivo/clipboard/runner.py`: `CliphistRunner` wrapping `run_bounded`.
- `kivo/clipboard/pin_store.py`: Persistent JSON pin storage at `$XDG_DATA_HOME/kivo/clipboard_pins.json` (mode `0600`).
- `kivo/clipboard/service.py`: `ClipboardService` with `inotify` database watcher (250 ms debounce).
- `kivo/ipc/protocol.py` & `kivo/ipc/handlers.py`:
  - Operations: `clip_get_history`, `clip_get_item`, `clip_copy_item`, `clip_pin_item`, `clip_delete_item`, `clip_clear_history`.
  - Event: `clip_history_changed`.
- Tests: `tests/test_clipboard_filter.py`, `tests/test_clipboard_service.py`, `tests/test_clipboard_ipc.py`.

#### Acceptance Criteria
1. Reads existing host history without modifying `cliphist` configuration.
2. High-entropy strings, passwords, and private keys receive `is_sensitive=True` and have previews masked.
3. Sensitive items are never written to the pinned store.
4. Pinning, unpinning, and deleting individual entries function accurately.
5. Re-copying an item sets Wayland clipboard via `run_bounded(["wl-copy", ...])`.
6. Full test suite passes; zero orphan processes.

---

### Stage 13.2 — File Shelf Staging

#### Objective
Implement a reference-based file-staging service that allows dragging files onto desktop drop targets, inspecting metadata, and performing explicit safe operations.

#### Deliverables
- `kivo/shelf/__init__.py`: Package entry and exports.
- `kivo/shelf/models.py`: Immutable `ShelfItem`.
- `kivo/shelf/service.py`: `FileShelfService` managing in-memory staging references and path verification.
- `kivo/ipc/protocol.py` & `kivo/ipc/handlers.py`:
  - Operations: `shelf_list_items`, `shelf_add_uris`, `shelf_remove_item`, `shelf_clear`, `shelf_open_item`.
  - Event: `shelf_items_changed`.
- Tests: `tests/test_shelf_service.py`, `tests/test_shelf_ipc.py`.

#### Acceptance Criteria
1. Dragging file URIs creates memory references without copying physical files.
2. Canonical paths resolved using `O_NOFOLLOW`; symlinks pointing outside permitted roots are refused.
3. Staged items report accurate name, size, MIME type, and accessibility status.
4. "Open" securely delegates to `run_bounded(["xdg-open", ...])`.
5. Removing an item unpins the reference without touching the underlying disk file.
6. Staging a file never executes it and never grants LLMs access to the parent folder.

---

### Stage 13.3 — Phone-to-PC Bridge

#### Objective
Implement an ephemeral, authenticated local-network bridge for peer-to-peer file transfers between smartphones and KIVO without cloud dependencies.

#### Deliverables
- `kivo/bridge/__init__.py`: Package entry and exports.
- `kivo/bridge/models.py`: Immutable `PairingSession`, `TransferItem`, `TransferStatus`.
- `kivo/bridge/crypto.py`: Ephemeral key generation, HMAC-SHA256, SAS code derivation.
- `kivo/bridge/qr.py`: QR code payload generation and in-memory PNG rendering.
- `kivo/bridge/quarantine.py`: Streaming file quarantine manager in `$XDG_RUNTIME_DIR/kivo/bridge/quarantine/`.
- `kivo/bridge/server.py`: Ephemeral FastAPI/Uvicorn server bound strictly to LAN IP.
- `kivo/bridge/service.py`: `PhoneBridgeService` owning server lifecycle, approval gates, and transfers.
- `kivo/ipc/protocol.py` & `kivo/ipc/handlers.py`:
  - Operations: `bridge_get_status`, `bridge_start`, `bridge_stop`, `bridge_approve_pairing`, `bridge_reject_pairing`, `bridge_resolve_transfer`, `bridge_revoke_device`.
  - Events: `bridge_status_changed`, `bridge_pairing_requested`, `bridge_transfer_requested`, `bridge_transfer_progress`, `bridge_transfer_completed`.
- Tests: `tests/test_bridge_crypto.py`, `tests/test_bridge_quarantine.py`, `tests/test_bridge_service.py`, `tests/test_bridge_ipc.py`.

#### Acceptance Criteria
1. Server binds strictly to LAN IP (`192.168.1.7`), never `0.0.0.0`.
2. QR code generates an ephemeral pairing token and 6-digit SAS code.
3. Connection is refused unless desktop explicitly approves pairing.
4. Incoming files stream into quarantine; never committed to `~/Downloads` without explicit desktop acceptance.
5. SHA-256 streaming checksum verified upon transfer completion.
6. Revoking a device drops its session instantly; stopping the bridge unbinds the listening port.

---

### Stage 13.4 — Unified Dynamic Notch & Dashboard Integration

#### Objective
Expose the three local productivity utilities in Quickshell, seamlessly integrating with Dynamic Notch and KIVO Dashboard while preserving existing media and telemetry interfaces.

#### Deliverables
- `shell/services/KivoIpc.qml`:
  - Added properties and methods for clipboard, file shelf, and phone bridge.
- `shell/components/DynamicNotch.qml`:
  - Compact transfer chip (`↑`/`↓` progress) and pairing request badge.
  - Expanded quick productivity drawer (recent clipboard items, staged files).
- `shell/components/DashboardClipboard.qml`:
  - Full clipboard history viewer, search bar, text/image preview, pin/delete/wipe actions.
- `shell/components/DashboardFileShelf.qml`:
  - Full file shelf grid, drag-and-drop `DropArea`, metadata display, open/reveal/send actions.
- `shell/components/DashboardPhoneBridge.qml`:
  - QR code pairing display, active devices list, incoming transfer approval cards.
- Tests: `shell/tests/test_notch.py` updates, QML linting (`qmllint`), end-to-end integration tests.

#### Acceptance Criteria
1. `qmllint` exits 0 with zero errors across all new and modified components.
2. Dynamic Notch expands cleanly without clipping or exceeding screen height.
3. Media playback drawer takes precedence when media is active.
4. Desktop health diagnostic passes 9/9.
5. All 10 verification gates pass.

---

## 3. Engineering Quality Gates per Milestone

Every stage (13.1 – 13.4) must pass all verification checks before progressing:
1. Focused unit and adversarial tests.
2. Full backend test suite (`pytest tests/`).
3. Shell test suite (`pytest shell/tests/`).
4. Qt6 QML lint (`qmllint`).
5. Desktop health diagnostic (`./scripts/desktop-health.sh`).
6. Whitespace check (`git diff --check`).
7. Python compilation (`py_compile`).
8. Subprocess and thread audit (no leaked threads, no orphaned processes).
