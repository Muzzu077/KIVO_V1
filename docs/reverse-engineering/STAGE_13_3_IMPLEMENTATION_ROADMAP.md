# KIVO — Stage 13.3 Implementation Roadmap: Phone-to-PC Bridge & File Shelf Review

```
Document:   Stage 13.3 Implementation Roadmap
Status:     Approved for Engineering Execution
Date:       2026-11-09
Baseline:   HEAD 1062c0d; Stage 12.2 and Stages 13.1–13.2 completed
Milestone:  Secure Local Network File Transfer Subsystem
```

---

## 1. Roadmap Overview & Sequencing

Following the security redesign in `STAGE_13_3_SECURE_ARCHITECTURE.md`, the implementation is organized into four testable stages:

```
┌─────────────────────────────────────────────────────────────┐
│ Stage 13.3.1 — Crypto Engine, TLS 1.3 & QR Pairing          │
│  Ephemeral X.509 cert, HKDF SAS, QR generation, handshake   │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│ Stage 13.3.2 — Quarantine Manager & Streamed Transfer Engine│
│  Two-tier quarantine, chunked upload/download, SHA-256 stream│
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│ Stage 13.3.3 — Bridge Service, Lifecycle & IPC Integration  │
│  FastAPI/Uvicorn LAN server, Desktop approval gate, IPC     │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│ Stage 13.3.4 — Native Android Companion Reference Client    │
│  Kotlin app with certificate pinning, QR scan, transfers    │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. Milestone Details

### Stage 13.3.1 — Cryptographic Engine, Ephemeral TLS & QR Pairing

#### Objective
Implement the local cryptographic foundations: in-memory X.509 certificate generation with SHA-256 fingerprinting, HKDF key derivation, 6-digit Short Authentication String (SAS) generation, and QR code rendering.

#### Deliverables
- `kivo/bridge/__init__.py`: Package entry and exports.
- `kivo/bridge/models.py`: Immutable data models (`PairingSession`, `PairingRequest`, `BridgeStatus`).
- `kivo/bridge/crypto.py`:
  - `generate_ephemeral_certificate(lan_ip)`: 256-bit ECDSA X.509 cert on `SECP256R1` valid for 24h.
  - `calculate_cert_fingerprint(cert)`: SHA-256 hex digest.
  - `derive_sas_code(pairing_secret, client_nonce, session_id)`: HKDF-SHA256 6-digit SAS code.
  - `generate_pairing_secret()`: 32 cryptographically secure random bytes (base64url).
- `kivo/bridge/qr.py`:
  - Renders `kivo-pair://` URI payload into in-memory PNG bytes using `qrcode` and `PIL.Image`.
- `kivo/bridge/pairing.py`:
  - `PairingManager`: Session state machine (pending, approved, active, revoked, expired).
- Tests: `tests/test_bridge_crypto.py`, `tests/test_bridge_pairing.py`.

#### Acceptance Criteria
1. Ephemeral certificates generate with valid SAN matching the active LAN IPv4.
2. SHA-256 certificate fingerprint matches DER byte digest.
3. SAS derivation produces identical 6-digit strings on both parties for identical inputs and nonces.
4. QR code payload strictly adheres to versioned URI schema.
5. Inactive pairing sessions expire after 5 minutes; tokens are burned on use.

---

### Stage 13.3.2 — Quarantine Manager & Streamed Transfer Engine

#### Objective
Implement two-tier quarantine storage, incremental SHA-256 streaming file reception, single-use download tickets, and collision-safe commitment into `~/Downloads/`.

#### Deliverables
- `kivo/bridge/quarantine.py`:
  - `QuarantineManager`:
    - Tier 1: `$XDG_RUNTIME_DIR/kivo/bridge/quarantine/` for files $\le 50$ MiB.
    - Tier 2: `$XDG_CACHE_HOME/kivo/bridge/quarantine/` for files $> 50$ MiB up to 2 GiB.
    - Directory permissions mode `0700`; file permissions mode `0600`.
    - Pre-transfer `statvfs` check enforcing `free_space > size + 1 GiB`.
    - Incomplete `.part` file cleanup on timeout or abort.
    - Startup and shutdown cleanup sweeps.
- `kivo/bridge/transfer.py`:
  - `TransferManager`:
    - Tracks active ingress and egress transfers (max 2 concurrent).
    - Chunked streaming (64 KiB) with rolling SHA-256 calculation.
    - `DownloadTicket` generation with 5-minute single-use TTL.
    - Safe commitment to `~/Downloads/` with collision avoidance (`file (1).ext`).
- Tests: `tests/test_bridge_quarantine.py`, `tests/test_bridge_transfer.py`.

#### Acceptance Criteria
1. Files $\le 50$ MiB stream to tmpfs; files $> 50$ MiB stream to disk cache, preventing tmpfs exhaustion.
2. Stalled or interrupted transfers delete `.part` files automatically after 30s timeout.
3. Transferred files are never committed without explicit approval.
4. Existing user files in `~/Downloads/` are never overwritten.
5. Path traversal filenames (`../../etc/shadow`, null bytes) are stripped to safe basenames.

---

### Stage 13.3.3 — Bridge Service, Lifecycle & IPC Integration

#### Objective
Build the ephemeral Uvicorn/FastAPI server bound strictly to the LAN IP, integrate desktop approval gates into `KivoApp`, and expose IPC contracts.

#### Deliverables
- `kivo/bridge/server.py`:
  - Ephemeral ASGI server with TLS 1.3 configuration.
  - Endpoints:
    - `POST /api/v1/pair/handshake`
    - `GET /api/v1/ping`
    - `POST /api/v1/upload`
    - `GET /api/v1/download/{ticket_id}`
    - `DELETE /api/v1/session`
- `kivo/bridge/service.py`:
  - `PhoneBridgeService`:
    - Physical interface discovery (binds to `192.168.1.7:8443`, filters out Docker bridges).
    - Network interface change detection.
    - Service lifecycle (`start()`, `close()`).
- `kivo/ipc/protocol.py` & `kivo/ipc/handlers.py`:
  - Operations: `bridge_start`, `bridge_stop`, `bridge_get_status`, `bridge_approve_pairing`, `bridge_reject_pairing`, `bridge_resolve_transfer`, `bridge_revoke_device`.
  - Events: `bridge_status_changed`, `bridge_pairing_requested`, `bridge_transfer_requested`, `bridge_transfer_progress`, `bridge_transfer_completed`.
- `kivo/app.py`: Lifecycle integration (`_init_bridge`, shutdown cleanup).
- Tests: `tests/test_bridge_server.py`, `tests/test_bridge_ipc.py`, `tests/test_bridge_service.py`.

#### Acceptance Criteria
1. Server binds strictly to LAN IP; binding to `0.0.0.0` is prevented.
2. Unauthenticated REST calls return `401 Unauthorized`.
3. Pairing requires explicit desktop `bridge_approve_pairing` call.
4. Stopping the bridge completely closes listening sockets and terminates threads within 3.0s.
5. All verification gates pass (769+ backend tests, shell tests, qmllint, desktop health 9/9).

---

### Stage 13.3.4 — Native Android Companion Reference Client

#### Objective
Provide an open-source, minimal native Android application (Kotlin) establishing pinned TLS 1.3 pairing, QR code scanning, and background file transfers.

#### Deliverables
- `android-companion/`:
  - `MainActivity.kt`: Camera QR scanner via ZXing/CameraX.
  - `TlsPinner.kt`: Custom `X509TrustManager` pinning the SHA-256 fingerprint from QR.
  - `PairingViewModel.kt`: SAS code display and approval waiting screen.
  - `TransferService.kt`: Android Foreground Service managing chunked upload/download.
  - `KeystoreHelper.kt`: Encrypted storage of session bearer token.
- `docs/reverse-engineering/ANDROID_COMPANION_SPEC.md`: Full architectural specification for building, testing, and distributing the companion APK via F-Droid or direct download.

---

## 3. Focused Security Review of Existing File Shelf (Phase 11)

### 3.1 The Writable-Path TOCTOU Race Window Analysis
In Stage 13.2, `FileShelfService.open_item(item_id)` was implemented as follows:
```python
# 1. Revalidate filesystem identity before opening
valid = self._authorizer.verify_existing_identity(
    item._canonical_path, item._identity
)
if not valid:
    return False, "stale_reference", "File replaced or deleted"

# 2. Launch via bounded subprocess using xdg-open
result = self._runner([self._xdg_open, item._canonical_path], timeout=2.0)
```

#### Detailed Vulnerability Breakdown
1. Inside `verify_existing_identity()`, the path is traversed using `O_NOFOLLOW | O_DIRECTORY`, opened with `O_NOFOLLOW | O_NONBLOCK`, verified via `os.fstat(fd)` against `(dev, ino, mode, size)`, and **then the file descriptor `fd` is closed**.
2. Immediately afterwards, `xdg-open` is invoked with `item._canonical_path` as a string argument.
3. **The Race Window**:
   - If the parent directory is writable by another local user or compromised process (e.g. `/tmp`, `/var/tmp`, or a shared project folder without `0700` permissions), an attacker can execute `unlink(path)` followed by `symlink(target, path)` or replace the file **in the microseconds between `verify_existing_identity` closing `fd` and `xdg-open` opening the pathname**.
   - `xdg-open` operates on pathnames in user-space and does its own resolution.

### 3.2 Mitigation & Architectural Hardening Plan
To completely eliminate this pathname TOCTOU race:
1. **Directory Permission Constraint (Immediate Policy)**:
   - File Shelf `PathAuthorizer` must reject staging files located in world-writable or group-writable directories (e.g. `/tmp` or `/var/tmp`) unless the directory has the sticky bit (`01000`) and is owned by the current user.
2. **File Descriptor Portal Passing (Stage 13.4 Integration)**:
   - On Wayland / freedesktop desktops, the standard application opening portal is `org.freedesktop.portal.OpenURI.OpenFile`.
   - `OpenFile` accepts an **open file descriptor (`fd`) passed over D-Bus**, completely bypassing path resolution!
   - KIVO can open the file with `O_NOFOLLOW`, verify `fstat(fd)`, and pass the open `fd` directly to the desktop portal without ever exposing a pathname window.
   - This architectural improvement will be scheduled for Stage 13.4 when desktop portal integration is implemented.

---

## 4. Quality Gates for Stage 13.3

Every implementation sub-stage must pass:
1. Focused bridge unit and adversarial tests (`pytest tests/test_bridge_*.py`).
2. Full backend regression test suite (`pytest tests/`).
3. Shell test suite (`pytest shell/tests/`).
4. Desktop health diagnostic (`./scripts/desktop-health.sh` 9/9 PASS).
5. Clean git diff whitespace check (`git diff --check`).
6. Python syntax and bytecode compilation (`py_compile`).
7. Network socket audit (`ss -tulpn | grep 8443` confirms 0 sockets leaked on shutdown).
