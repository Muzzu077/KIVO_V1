# KIVO — Stage 13.3.3.1 Engineering Report: Desktop Upload Receiver, Authenticated Transfer Protocol & Quarantine

```
Document:       Stage 13.3.3.1 Engineering Report
Status:         PASS WITH LIMITATIONS
Date:           2026-11-10
Baseline:       HEAD 1062c0d; Stages 13.3.1–13.3.2.3 completed
Scope:          Desktop Backend Upload Receiver, Quarantine Storage, Publication, IPC Contracts
Target Android: Stage 13.3.3.2 (Mobile companion transfer client)
```

---

## Executive Summary

Stage 13.3.3.1 delivers the complete desktop backend upload receiver for KIVO's Phone-to-PC Bridge. An authenticated Android device can stream files up to 2 GiB over local TLS 1.3 into isolated quarantine storage under two explicit desktop consent gates:

1. **Gate A (`allow_receive`)**: Proposed upload metadata (filename, declared size, whole-file SHA-256 digest, optional MIME hint) must be explicitly approved by the desktop user before the server reserves disk capacity or accepts a single chunk byte.
2. **Gate B (`accept_file`)**: After all chunks stream into quarantine and the whole-file size and SHA-256 are verified on disk, the file enters `awaiting_final_acceptance`. The desktop user must explicitly accept the verified file before it is published into `~/Downloads/`.

Files are published using atomic no-overwrite semantics (`renameat2(RENAME_NOREPLACE)`) and automatic collision resolution (`report (1).pdf`), ensuring existing user files are never overwritten.

---

## 1. Initial Architecture Inspection

An audit of the baseline established in Stages 13.3.1 through 13.3.2.3 confirmed:

- **Session Credential Model**:
  - `PairedSession` holds directional 32-byte keys derived via HKDF-SHA256: `upstream_key` (`K_up`) and `downstream_key` (`K_down`), alongside a high-entropy `bearer_token` string (`sess_tok_...`).
  - `K_up` authenticates Android-to-desktop transfer requests.
  - `K_down` authenticates desktop-to-Android transfer responses.
  - `bearer_token` identifies the authenticated session in the HTTP `Authorization` header.
  - `T_pair` and `K_poll` are strictly isolated to the pairing/polling lifecycle and are never reused for file transfers.
- **Server Architecture**:
  - Ephemeral Uvicorn server running FastAPI on a dedicated background thread.
  - Bound strictly to the discovered LAN IPv4 address (e.g. `192.168.1.7:8443`), never to `0.0.0.0` or `::`.
  - Ephemeral TLS 1.3 self-signed certificate with ECDSA P-256 and SHA-256 fingerprint pinned via QR code.
- **Quarantine Tmpfs Constraint**:
  - `$XDG_RUNTIME_DIR` (`/run/user/1000`) is mounted on `tmpfs` with only ~1.6 GB capacity.
  - Files $\le 50$ MiB use `$XDG_RUNTIME_DIR/kivo/bridge/quarantine/` (fast RAM stream, zero flash wear).
  - Files $> 50$ MiB up to 2 GiB use `$XDG_CACHE_HOME/kivo/bridge/quarantine/` (physical disk).

---

## 2. Transfer Authentication Contract

### 2.1 Directional Key Separation

| Primitive | Secret / Key | Direction | Purpose |
|---|---|---|---|
| Request MAC | `K_up` (32 bytes) | Android $\to$ PC | Authenticates headers, sequence, nonce, timestamp, body SHA-256 |
| Response MAC | `K_down` (32 bytes) | PC $\to$ Android | Authenticates echoed sequence, nonce, state, HTTP status, payload SHA-256 |
| Session Token | `bearer_token` | Android $\to$ PC | Identifies active paired session in HTTP `Authorization` header |

Public session IDs and transfer IDs are routing identifiers; they are never accepted as proof of authorization.

### 2.2 Replay Protection & Concurrency Window

To handle concurrent chunk uploads and network retries:

- **Sequence Numbers**: Monotonic 64-bit unsigned integers starting at `1`.
- **Nonces**: Fresh 16-byte random values encoded as unpadded base64url.
- **Freshness Window**: $\pm 60.0$ seconds from system clock.
- **Bounded Sliding Window**: 128-entry sequence window tracks accepted sequences, permitting bounded out-of-order execution while rejecting duplicates and retired sequences.
- **Nonce Cache**: Most recent 256 nonces remembered per session.
- **State Safety**: Request signature is verified before reading the request body. Sequence and nonce are committed only after the body hash and length are verified.

---

## 3. Canonical Encoding Specifications

### 3.1 Canonical Transfer Request (`K_up`)

Binary transcript format (length-prefixed fields with big-endian integer encodings):

```
Prefix: "KIVO_BRIDGE_TRANSFER_REQUEST_V1\0" (32 bytes)
Fields:
  [4-byte BE length] + version (ASCII "1")
  [4-byte BE length] + session_id (ASCII)
  [4-byte BE length] + method (ASCII uppercase, e.g. "POST", "PUT", "GET")
  [4-byte BE length] + endpoint (ASCII absolute path)
  [8-byte BE uint64] + sequence (monotonic integer >= 1)
  [4-byte BE length] + nonce (16 raw bytes)
  [8-byte IEEE-754 BE double] + timestamp (epoch seconds)
  [8-byte BE uint64] + content_length (bytes)
  [4-byte BE length] + content_sha256 (32 raw bytes)
```

The client signs this with `HMAC-SHA256(K_up, transcript)` and supplies the 64-character lowercase hex string in `X-Kivo-Signature`.

### 3.2 Canonical Transfer Response (`K_down`)

Binary transcript format:

```
Prefix: "KIVO_BRIDGE_TRANSFER_RESPONSE_V1\0" (33 bytes)
Fields:
  [4-byte BE length] + version (ASCII "1")
  [4-byte BE length] + response_context (ASCII, e.g. "upload_init", "upload_chunk")
  [4-byte BE length] + session_id (ASCII)
  [2-byte BE uint16] + http_status (e.g. 200, 202, 400, 409)
  [8-byte BE uint64] + sequence (echoed request sequence)
  [4-byte BE length] + nonce (echoed 16 raw nonce bytes)
  [4-byte BE length] + state (ASCII, e.g. "awaiting_desktop_approval", "receiving")
  [4-byte BE length] + transfer_id (ASCII)
  [8-byte BE uint64] + payload_length (bytes of raw JSON payload)
  [4-byte BE length] + payload_sha256 (32 raw bytes of payload SHA-256)
```

The server signs with `HMAC-SHA256(K_down, transcript)`. The client verifies the signature before processing response fields or credentials.

---

## 4. Upload Lifecycle & State Machine

```
               [ POST /uploads/init ]
                         │
                         ▼
                     CREATED
                         │
                         ▼
            AWAITING_DESKTOP_APPROVAL ──(reject_receive)──► REJECTED (terminal)
                         │                                     ▲
                 (allow_receive)                               │ (timeout)
                         │                                     │
                         ▼                                  EXPIRED (terminal)
                     RECEIVING ──(cancel)───────────────► CANCELLED (terminal)
                         │                                     ▲
                    (all chunks)                               │ (stalled >30s)
                         │                                     │
                         ▼                                   FAILED (terminal)
                     VERIFYING (re-hash & fsync)
                         │
                         ▼
            AWAITING_FINAL_ACCEPTANCE ──(discard_file)──► DISCARDED (terminal)
                         │
                   (accept_file)
                         │
                         ▼
                     COMMITTED (published to Downloads)
```

### Transition Invariants
- Storage is only reserved on transition from `AWAITING_DESKTOP_APPROVAL` to `RECEIVING`.
- A rejected offer consumes zero quarantine storage.
- Chunks arriving before `allow_receive` are rejected with `409 Conflict`.
- Once all chunks arrive, the state transitions to `VERIFYING` then `AWAITING_FINAL_ACCEPTANCE`.
- Publication to `~/Downloads/` happens **only** upon explicit `accept_file` resolution.
- Any failure, timeout, or cancellation unlinks the quarantine file immediately and releases reserved disk capacity.

---

## 5. Chunked Streaming Protocol

- **Fixed Chunk Size**: 1 MiB (`1,048,576` bytes).
- **Endpoint**: `PUT /api/v1/transfers/uploads/{transfer_id}/chunks/{chunk_index}`
- **Streaming Ingestion**: The server streams chunks in bounded buffers up to 1 MiB, verifying Content-Length and body SHA-256 concurrently with network reading.
- **Bounded Concurrency**: Maximum 2 concurrent active transfers. Excess requests receive `429 Too Many Requests`.
- **Strict Final-Chunk Geometry**: The final chunk must match `size % 1 MiB` (or 1 MiB if evenly divisible).
- **Idempotent Chunk Retries**: If network drops occur, a retransmitted duplicate chunk index with matching SHA-256 succeeds without appending duplicate bytes.
- **Out-of-Order Rejection**: Chunks exceeding the expected monotonic index return `409 Conflict`.

---

## 6. Quarantine Implementation & Security Rules

Implemented in `kivo/bridge/quarantine.py`:

1. **Two-Tier Model**:
   - Tier 1: `$XDG_RUNTIME_DIR/kivo/bridge/quarantine/` ($\le 50$ MiB).
   - Tier 2: `$XDG_CACHE_HOME/kivo/bridge/quarantine/` ($> 50$ MiB up to 2 GiB).
2. **File Permissions**:
   - Directory mode `0700`, file mode `0600`.
   - Traversal via `O_NOFOLLOW | O_DIRECTORY | O_CLOEXEC` descriptor-relative operations.
   - Symlinked roots or replaced roots are detected and rejected.
   - Kernel Linux mount ID validated via `statx(STATX_MNT_ID)`.
3. **Randomized Internal Names**:
   - Format: `quarantine_<32-hex-uuid>.part`.
   - Incoming display filenames are never used as filesystem paths in quarantine.
4. **Safety Margins**:
   - Physical disk tier: Requires declared size + 1 GiB safety margin (`DISK_SAFETY_MARGIN_BYTES`).
   - Tmpfs tier: Requires declared size + 128 MiB safety margin (`TMPFS_SAFETY_MARGIN_BYTES`).
5. **Startup & Shutdown Purge**:
   - Bridge initialization safely unlinks all `quarantine_*.part` files owned by the current user. Unrelated files and symlinks are ignored.

---

## 7. Safe Downloads Publication

1. **User Directory Resolution**: Resolves Downloads path via `XDG_DOWNLOAD_DIR`, `~/.config/user-dirs.dirs`, or `~/Downloads/`.
2. **Same-Filesystem Publication**:
   - Atomic no-overwrite rename via `renameat2(RENAME_NOREPLACE)` syscall.
   - Collision resolution: Tries `name.ext`, `name (1).ext`, `name (2).ext` up to 1000 attempts without TOCTOU races.
   - `fsync()` directory descriptor after installation.
3. **Cross-Filesystem Publication**:
   - When quarantine is in tmpfs and Downloads is on ext4 disk:
     1. Creates random hidden temporary file in Downloads directory (`.kivo-publish-<hex>.tmp`) with `O_EXCL | O_NOFOLLOW | 0600`.
     2. Streams bytes from verified quarantine descriptor in 64 KiB buffers.
     3. Re-computes SHA-256 during copy and confirms exact byte count and digest.
     4. `fsync()` destination temporary file.
     5. Atomically renames temporary file to destination via `renameat2(RENAME_NOREPLACE)`.
     6. `fsync()` Downloads directory.
     7. Unlinks quarantine file only after successful atomic installation.

---

## 8. IPC Architecture & Operations

Three new IPC operations and three new events added to `kivo/ipc/protocol.py`:

### Operations

| Operation | Parameters | Description |
|---|---|---|
| `bridge_get_transfers` | `{}` | Returns public snapshot of all active/recent transfers |
| `bridge_resolve_transfer` | `{"transfer_id": "...", "decision": "..."}` | Applies Gate A (`allow_receive`/`reject_receive`) or Gate B (`accept_file`/`discard_file`) |
| `bridge_cancel_transfer` | `{"transfer_id": "..."}` | Cancels active transfer and unlinks quarantine file |

### Events

| Event | Data Payload |
|---|---|
| `bridge_transfer_requested` | Transfer metadata (ID, filename, size, device name, MIME) |
| `bridge_transfer_progress` | Progress bytes, progress percent, state |
| `bridge_transfer_completed` | Terminal state, published filename or error code |

### Privacy Invariants
IPC payloads never contain session keys (`K_up`, `K_down`), bearer tokens, quarantine filesystem paths, or internal file descriptors.

---

## 9. Android Interoperability Contract (Stage 13.3.3.2)

Deterministic test vectors created at `tests/fixtures/transfer_test_vectors.json`.

### 9.1 Android Request Headers

```http
POST /api/v1/transfers/uploads/init HTTP/1.1
Host: 192.168.1.7:8443
Authorization: Bearer sess_tok_buOWe22qTLQBHoVVlO-wrgLelKcUAzZL5FWjicDCdTk
X-Kivo-Protocol: 1
X-Kivo-Session: sess_0123456789abcdef
X-Kivo-Sequence: 1
X-Kivo-Nonce: EBESExQVFhcYGRobHB0eHw
X-Kivo-Timestamp: 1700000000.0
X-Kivo-Content-SHA256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
X-Kivo-Signature: <64-hex HMAC-SHA256 over canonical request with K_up>
Content-Length: 87
Content-Type: application/json

{"filename":"report.pdf","size":2097152,"sha256":"a591a6d40bf420404a011733cfb7b190d62c65bf0bcda32b57b277d9ad9f146e","mime":"application/pdf"}
```

### 9.2 Android Response Verification Sequence

```http
HTTP/1.1 202 Accepted
Content-Type: application/json

{
  "ok": true,
  "v": "1",
  "ctx": "upload_init",
  "sid": "sess_0123456789abcdef",
  "seq": 1,
  "nonce_b64": "EBESExQVFhcYGRobHB0eHw",
  "http_status": 202,
  "state": "awaiting_desktop_approval",
  "tid": "tr_aabbccddee112233aabbccdd",
  "payload_b64": "...",
  "payload_sha256": "...",
  "sig": "<64-hex HMAC-SHA256 over canonical response with K_down>"
}
```

The Android client must:
1. Confirm `ok == true` and `v == "1"`.
2. Confirm `ctx`, `sid`, `seq`, `nonce_b64`, and `http_status` match the request context.
3. Compute `SHA-256(base64_decode(payload_b64))` and confirm it matches `payload_sha256`.
4. Construct `CanonicalTransferResponse` and verify `sig` with `K_down` via constant-time comparison before parsing payload JSON.

### 9.3 Android Pairing Recovery Prerequisite

Before transfer functionality is exposed on Android:
1. If the blocking handshake HTTP connection terminates prematurely after submission, the Android client must **not** replay the original handshake.
2. The client derives `K_poll`, `K_up`, `K_down` from the consumed pairing secret and client nonce.
3. The client polls `POST /api/v1/pair/status` with fresh sequence, nonce, and timestamp signed with `K_poll`.
4. The client verifies the V2 response MAC and credential SHA-256 before persisting session keys to Android Keystore.

---

## 10. Verification Gates & Test Results

### 10.1 Test Suites Executed

| Suite | Tests | Result | Duration |
|---|---|---|---|
| Transfer Crypto & Transcripts (`test_transfer_crypto.py`) | 20 passed | PASS | 0.05s |
| Metadata Validation (`test_transfer_validation.py`) | 28 passed | PASS | 0.05s |
| Quarantine Storage & Security (`test_bridge_quarantine.py`) | 25 passed | PASS | 0.12s |
| Transfer Manager State Machine (`test_bridge_transfer.py`) | 31 passed | PASS | 0.15s |
| Transfer Server Loopback TLS (`test_bridge_transfer_server.py`) | 6 passed | PASS | 8.82s |
| Bridge IPC Operations (`test_bridge_ipc.py`) | 19 passed | PASS | 2.15s |
| Existing Bridge Integration (`test_bridge_integration.py`) | 12 passed | PASS | 4.85s |
| Existing Bridge Crypto & Pairing | 29 passed | PASS | 0.35s |
| **Total Bridge Subsystem Tests** | **170 passed** | **PASS** | **13.78s** |
| Android Companion Unit Tests (`./gradlew test`) | 54 passed | PASS | 5.0s |
| Android Companion Lint (`./gradlew lintDebug`) | 0 errors | PASS | 0.48s |
| File Shelf Integration Tests (`test_shelf_*.py`) | 45 passed | PASS | 4.30s |
| File Mutation Foundation Tests (`test_file_mutations.py`) | 117 passed (69 subtests) | PASS | 0.82s |
| **Full Backend Test Suite (`pytest tests/`)** | **1024 passed (176 subtests)** | **PASS** | **77.38s** |
| Frontend Shell Tests (`pytest shell/tests/`) | 16 passed | PASS | 15.07s |
| Qt6 QML Lint (`qmllint shell/...`) | 0 errors | PASS | exit 0 |
| Desktop Health Diagnostic (`desktop-health.sh`) | 9/9 PASS | PASS | exit 0 |
| Python compileall (`compileall kivo/ tests/`) | 0 errors | PASS | exit 0 |
| Whitespace & Syntax (`git diff --check`) | 0 warnings | PASS | exit 0 |

---

## 11. Security Audit & Threat Model Mitigations

| Threat ID | Threat Description | Mitigation in Stage 13.3.3.1 | Verification |
|---|---|---|---|
| **T11** | Upload flood / storage exhaustion | Max 2 concurrent transfers; max 2 GiB aggregate reservation; Gate A approval required before reserving space | `test_concurrent_transfer_limit`, `test_aggregate_limit` |
| **T12** | Filename path traversal | Filename validated against strict regex; separators (`/`, `\`), NUL, controls, and bidi overrides rejected; NFC normalized; display-only | `test_rejects_path_separators`, `test_rejects_bidi_controls` |
| **T13** | Symlinks in quarantine | Quarantine roots traversed with `O_NOFOLLOW`; directory and file modes `0700`/`0600`; Linux mount ID checked | `test_rejects_symlinked_quarantine_root`, `test_cleanup_ignores_symlinks` |
| **T14** | Tmpfs storage crash | Dynamic tiering routes files $> 50$ MiB to disk; requires free space > declared size + 1 GiB margin | `test_tier_for_size`, `test_insufficient_free_space` |
| **T16** | Abandoned / orphaned files | Stalled transfers timeout after 30s; service start/stop purges quarantine | `test_cleanup_removes_abandoned_parts` |
| **T17** | Direct execution / quarantine escape | Mode `0600` on files; zero agent tool registration; explicit Gate B desktop acceptance required | `test_create_file_mode_0600`, `test_no_secrets_in_public` |
| **T18** | Destination overwrite in Downloads | Atomic `renameat2(RENAME_NOREPLACE)`; collision names `report (1).pdf`; destination verified | `test_publish_collision_avoidance` |

---

## 12. At-Rest Confidentiality Limitations

- **Transit Security**: TLS 1.3 with ephemeral ECDSA P-256 certificate and pinned SHA-256 fingerprint provides cryptographic transit encryption and integrity.
- **At-Rest Storage**: Quarantined files are stored in plain text with file mode `0600` and directory mode `0700` restricted to UID 1000.
- **Distinction**: KIVO does **not** claim quarantine files are encrypted at rest with an ephemeral key. Any process running under the same user UID (or root) can read quarantined files. This matches the standard Linux desktop security boundary for downloaded files.

---

## 13. Limitations & Next-Stage Scope

1. **Desktop Backend Only**: This stage implements the server receiver and IPC contracts. The mobile Android client transfer implementation is deferred to Stage 13.3.3.2.
2. **Transfer Resumption across Process Restarts**: Transfers can be resumed across transient network disconnections while the bridge process remains active. Transfers do not survive desktop process restart; abandoned `.part` files are purged on startup.
3. **Android Polling Recovery**: The Android companion currently lacks automatic signed-poll recovery after an interrupted handshake response. Stage 13.3.3.2 must implement this recovery before exposing transfers in the mobile UI.

---

## Final Verdict

```
VERDICT: PASS WITH LIMITATIONS
```

**Justification**:
- Authenticated transfer protocol with length-prefixed transcripts and directional keys (`K_up`/`K_down`) implemented and verified.
- Bounded replay window, monotonic sequences, fresh nonces, and timestamp validation enforced.
- Two-tier quarantine architecture ($\le 50$ MiB tmpfs, $> 50$ MiB disk) with strict safety margins and atomic capacity reservations active.
- Two separate desktop consent gates (Gate A: `allow_receive`, Gate B: `accept_file`) strictly enforced.
- Whole-file SHA-256 verification and atomic no-overwrite Downloads publication verified.
- All 1,024 backend tests, 54 Android unit tests, 16 shell tests, 9/9 desktop health probes, QML lint, and `git diff --check` pass cleanly.
- The "WITH LIMITATIONS" qualification correctly reflects that end-to-end mobile file transfers await the Android implementation in Stage 13.3.3.2.
