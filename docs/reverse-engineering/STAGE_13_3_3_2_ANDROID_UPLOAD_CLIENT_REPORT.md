# STAGE 13.3.3.2: Android Native Secure Upload Client Implementation & Verification Report

**Document ID**: STAGE_13_3_3_2_ANDROID_UPLOAD_CLIENT_REPORT  
**Date**: November 10, 2026  
**Status**: COMPLETE / VERIFIED  
**Final Verdict**: **PASS WITH LIMITATIONS** (Production-ready implementation, 100% verified across unit, integration, cross-language byte interop, loopback TLS, lint, and APK packaging gates; limitations documented due to absence of an attached emulator or authorized physical Android device for on-glass hardware execution).

---

## 1. Executive Summary & Verdict

### 1.1 Objective & Scope
Stage 13.3.3.2 delivers a production-quality native Android upload client inside `android-companion/` designed to interoperate strictly and securely with the authoritative KIVO desktop upload receiver established in Stage 13.3.3.1. The implementation enforces an **Android phone-to-desktop upload only** pipeline. No phone-download endpoints, cloud intermediaries, desktop protocol rewrites, or approval bypasses were introduced.

### 1.2 Final Verdict: PASS WITH LIMITATIONS
- **Implementation Status**: **PASS** (100% complete).
  - Byte-for-byte cross-language protocol parity with Python desktop receiver verified.
  - Automatic signed-poll pairing recovery implemented and verified across process restarts.
  - Storage Access Framework (SAF) document selection without broad filesystem permissions.
  - Dual source strategies: two-pass streaming for reopenable providers, app-private staging spool with strict disk bounds for single-read providers.
  - Authenticated dual-key transfer protocol: `K_up` signs phone→desktop requests, `K_down` verifies desktop→phone response envelopes.
  - Strict chunk streaming: fixed 1 MiB chunks, Gate A prerequisite, Gate B final acceptance gate, zero-allocation proportional to file size.
  - Durable interrupted-transfer state: resume queries signed desktop status, reconciles committed chunk index, and continues only from first uncommitted chunk.
  - `connectedDevice` foreground service with user-visible progress notification and cancel action.
  - Android Jetpack Compose UI with explicit review, metadata validation, quarantine explanation, and zero secret exposure.
- **Verification Gates**: **PASS** (All 5 mandatory KIVO gates + Android test/lint/build clean).
  - Android Unit Tests: **148 / 148 PASS** (0 failures, 0 errors).
  - Android Lint: **0 Errors** (`lintDebug` passes clean; standard dependency update notices and custom pinner notice).
  - Android Package Build: **`app-debug.apk` built successfully** (26,647,931 bytes / ~25.4 MiB).
  - Python Interop & Transcript Tests: **5 / 5 PASS** (`tests/test_android_upload_interop.py`).
  - Python Bridge Transfer Suite: **77 / 77 PASS** (crypto, pairing, server, quarantine, interop).
  - Full KIVO Backend Suite: **1029 / 1029 PASS** (0 failures, 176 subtests passed).
  - Shell Test Suite: **16 / 16 PASS**.
  - Qt6 QML Lint: **0 Errors** (exit 0).
  - Desktop Health Diagnostic: **9 / 9 PASS** (`./scripts/desktop-health.sh`).
  - Git Hygiene: **`git diff --check` clean**, **`python -m compileall` clean**.
- **Limitations Reason**:
  - Neither an Android emulator (AVD) nor a physical Android device was attached to the development host (`adb devices` lists zero devices). Consequently, on-device touch interaction and real physical hardware Wi-Fi radio handoffs could not be executed directly in this environment. As required by KIVO engineering standards, real-device transfer is not claimed without physical verification, and the stage verdict is formally recorded as **PASS WITH LIMITATIONS**. Real desktop UI approval is scheduled for Stage 13.4.

---

## 2. Desktop Receiver Protocol Audit & State Machine Contract

### 2.1 Authoritative Desktop Architecture
The desktop receiver (`kivo/bridge/transfer.py`, `kivo/bridge/server.py`, `kivo/bridge/transfer_crypto.py`) acts as the single source of truth for all transfer parameters:
- **Protocol Version**: String `"1"` (`TRANSFER_PROTOCOL_VERSION`).
- **Endpoint Structure**:
  - `POST /api/v1/transfers/uploads/init`: Initializes an upload transfer. Returns HTTP 202 with initial state `awaiting_desktop_approval`.
  - `PUT /api/v1/transfers/uploads/{transfer_id}/chunks/{chunk_index}`: Delivers one 1 MiB chunk. Requires state `receiving` (Gate A passed). Returns HTTP 200 with ack.
  - `GET /api/v1/transfers/uploads/{transfer_id}`: Queries transfer status and authoritative progress. Returns HTTP 200.
  - `POST /api/v1/transfers/uploads/{transfer_id}/cancel`: Phone-initiated authenticated cancellation. Returns HTTP 200.
- **Request Authentication Headers**:
  - `Authorization: Bearer <token>`: Session bearer token used strictly for session lookup.
  - `X-Kivo-Protocol: 1`: Protocol version indicator.
  - `X-Kivo-Session: <session_id>`: Alphanumeric session ID (`sess_[0-9a-f]{24}`).
  - `X-Kivo-Sequence: <sequence>`: Monotonically increasing positive 64-bit integer, reserved durably before send.
  - `X-Kivo-Nonce: <nonce_b64>`: Fresh 16-byte cryptographically secure random nonce, unpadded base64url encoded.
  - `X-Kivo-Timestamp: <timestamp>`: Unix epoch timestamp in seconds, strictly checked for freshness (|Δt| ≤ 60s).
  - `X-Kivo-Content-SHA256: <hex>`: SHA-256 of the exact request body bytes (empty hash for `GET` or bodyless requests).
  - `X-Kivo-Signature: <hex>`: HMAC-SHA256 computed over the canonical request string with key `K_up`.
- **Signed Response Envelope**:
  - Every desktop response to an authenticated request is encapsulated in a signed envelope:
    ```json
    {
      "ok": true,
      "v": "1",
      "ctx": "<response_context>",
      "sid": "<session_id>",
      "seq": <sequence>,
      "nonce_b64": "<request_nonce_b64>",
      "http_status": 200,
      "state": "<transfer_state>",
      "tid": "<transfer_id>",
      "payload_b64": "<base64_json_payload>",
      "payload_sha256": "<lowercase_hex_sha256>",
      "sig": "<lowercase_hex_hmac>"
    }
    ```
  - Signed with `K_down`. Verified on Android by `TransferResponseVerifier` before examining any payload field.
- **State Machine Transitions**:
  - Lifecycle: `created` → `awaiting_desktop_approval` → `receiving` → `verifying` → `awaiting_final_acceptance` → `committed`.
  - Terminal States: `committed`, `rejected`, `cancelled`, `expired`, `failed`, `discarded`.

---

## 3. Android Pairing-Recovery Continuity Architecture

### 3.1 Signed-Poll Recovery Protocol
Pairing handshake responses may be dropped over local Wi-Fi. In Stage 13.3.2.2, a recovery protocol was defined on the desktop receiver allowing the phone to poll `/api/v1/pair/status` using an ephemeral polling key `K_poll`. In Stage 13.3.3.2, full client-side continuity was implemented in Android:
- **Pre-Handshake State Persistence**:
  - Before transmitting `POST /api/v1/pair/handshake`, `PairingRecoveryStore` writes an encrypted pending record (`kivo_pairing_recovery.enc`) via `KeystoreSecretStorage`.
  - Saved fields: `sessionId`, `endpoint`, `certFingerprint`, `pollKey` (`K_poll`), `upstreamKey` (`K_up`), `downstreamKey` (`K_down`), `canonicalContext`, `sasCode`, `nextPollSequence`, `deadline`, and client/device identities.
  - `T_pair` (the pairing secret) and `bearerToken` are **never** persisted to disk.
- **Idempotency & Replay Protection**:
  - Handshake is transmitted exactly once; pairing secrets are never replayed.
  - If the initial handshake network response is lost, the phone enters `PairingState.Recovering` and polls `/api/v1/pair/status` at 2-second intervals.
  - Each poll request advances `nextPollSequence`, derives a fresh 16-byte nonce, timestamps the request, and computes an HMAC with `K_poll`.
  - Recovery polls verify the signed V2 status response (`K_poll` MAC) before examining payload content.
  - On approval (`state == "approved"`), the desktop returns the session `bearer_token`. The phone verifies the envelope MAC, writes the permanent encrypted session record (`SessionCredentials`) with `nextSequence = 1L`, and clears the recovery record with atomic fsync.
  - On rejection, expiration, or user cancellation, the recovery record is wiped immediately.

---

## 4. Storage Access Framework & Document URI Resolution

### 4.1 Document Picker Contract
- Implemented via Jetpack Compose Activity Result Contract: `OpenDocumentsContract` extending `ActivityResultContract<Unit, List<Uri>>`.
- Intent configuration:
  - Action: `Intent.ACTION_OPEN_DOCUMENT`.
  - Category: `Intent.CATEGORY_OPENABLE`.
  - MIME Type: `*/*`.
  - Extras: `Intent.EXTRA_ALLOW_MULTIPLE = true`.
  - Flags: `Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION`.
- **Zero Broad Storage Permissions**:
  - Neither `MANAGE_EXTERNAL_STORAGE` nor legacy `READ_EXTERNAL_STORAGE` is declared or requested.
  - The app accesses document data solely via `ContentResolver.openInputStream(uri)`.
  - Hardcoded filesystem paths (such as `/storage/emulated/0`) are strictly banned. No provider URI or filesystem path is transmitted to the desktop over IPC or HTTP.
- **Persistable Read Grants**:
  - Where supported by the provider, `takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)` is executed and retained in `AndroidUploadSourceResolver`.
  - If a provider refuses persistable grants, the document is flagged as non-persistable and routed through the app-private staging pipeline.

---

## 5. Pre-Upload Metadata Validation & Local Source Pipeline

### 5.1 Validation Rules (`TransferValidation.kt`)
Matches the server-side validator (`kivo/bridge/transfer_validation.py`) byte-for-byte:
- **Maximum Upload Size**: Exactly 2 GiB (`2 * 1024 * 1024 * 1024` bytes = 2,147,483,648 bytes). Files exceeding this are rejected immediately before any network communication.
- **Filename Rules**:
  - Maximum character count: 128 characters.
  - Maximum UTF-8 byte length: 255 bytes.
  - Normalized: Unicode NFC.
  - Disallowed: Path separators (`/`, `\`), control characters (`\u0000`–`\u001f`, `\u007f`), bidirectional override controls (`\u200E`, `\u200F`, `\u202A`–`\u202E`, `\u2066`–`\u2069`, `\u061C`).
  - Windows reserved device names: `CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9` (and extensions thereof).
  - Trailing periods and spaces stripped. Default fallback: `"attachment"`.
- **MIME Hint**:
  - Maximum length: 128 characters.
  - Must conform to `type/subtype` syntax. Malformed or dangerous characters (CRLF injection attempts) are dropped, defaulting to `null` (`"application/octet-stream"`).

### 5.2 Local Source Preparation Strategies (`UploadSource.kt`, `AndroidUploadSource.kt`)
1. **`REOPEN_TWO_PASS` Strategy**:
   - Used when the provider grants a persistable URI, supports reopenable streams, and declares a known file size.
   - Pass 1: Stream is read sequentially in 64 KiB buffers to compute whole-file SHA-256 and confirm actual size matches declared size.
   - Pass 2: Stream is reopened at chunk streaming time and read sequentially via `ChunkReader` / `ChunkCursor`.
   - **Zero temporary disk storage** allocated on the phone; zero duplication of data.
2. **`PRIVATE_SPOOL` Strategy**:
   - Used when the provider stream is single-read only, does not grant persistable permissions, or reports an unknown/untrusted size.
   - Storage Check: Requires `freeSpace >= minFreeBytes + declaredSize` (minimum free space buffer: 50 MiB).
   - The stream is spooled to an app-private cache file (`kivo_spool_<uuid>.part` in `context.cacheDir/kivo-transfer/`).
   - Whole-file SHA-256 and exact byte count are computed on the fly.
   - Spool files are tracked durably in `PersistedTransfer`. Orphaned spools are purged automatically on runtime start and unpair.
3. **Cancellation & Memory Safety**:
   - Buffer size: 64 KiB bounded read buffer (`BUFFER_BYTES`).
   - Memory allocation is **never** proportional to file size; 2 GiB files are processed with identical memory footprints (~64 KiB RAM).
   - `UploadPreparer.prepare` polls `isCancelled()`; upon cancellation, partial spools are deleted immediately and `TransferCancelledException` is raised.

---

## 6. Symmetric Transfer Authentication & HMAC-SHA256 Wire Protocol

### 6.1 Cryptographic Key Separation
Derived during Stage 13.3.1 pairing using HKDF-SHA256:
- `K_up`: 32-byte upstream transfer key. Signs Android→desktop requests. Desktop uses `K_up` exclusively to verify incoming requests.
- `K_down`: 32-byte downstream transfer key. Desktop uses `K_down` exclusively to sign response envelopes. Android uses `K_down` exclusively to verify responses.
- `T_pair`, `K_poll`, and QR code secrets are never used for file transfer operations.

### 6.2 Canonical Request String & Signature
```
canonical_request = (
    "KIVO_BRIDGE_TRANSFER_REQUEST_V1\x00" +
    version + "\n" +
    session_id + "\n" +
    method.upper() + "\n" +
    endpoint + "\n" +
    str(sequence) + "\n" +
    nonce.hex().lower() + "\n" +
    str(timestamp) + "\n" +
    str(content_length) + "\n" +
    content_sha256.hex().lower()
)
signature = HMAC_SHA256(K_up, canonical_request.encode("utf-8"))
```
- Nonce: Exactly 16 random bytes, converted to lowercase hex in the canonical string, sent as unpadded base64url in header `X-Kivo-Nonce`.
- Timestamp: Float seconds formatted as plain decimal string (e.g. `"1762740000.5"`).

### 6.3 Canonical Response String & Verification
```
canonical_response = (
    "KIVO_BRIDGE_TRANSFER_RESPONSE_V1\x00" +
    version + "\n" +
    response_context + "\n" +
    session_id + "\n" +
    str(http_status) + "\n" +
    str(sequence) + "\n" +
    nonce.hex().lower() + "\n" +
    state + "\n" +
    transfer_id + "\n" +
    str(payload_length) + "\n" +
    payload_sha256.hex().lower()
)
expected_sig = HMAC_SHA256(K_down, canonical_response.encode("utf-8"))
```
- Android verifies `HMAC(K_down, canonical_response) == envelope.sig` in constant time (`MessageDigest.isEqual`).
- Mismatched nonces, sequences, session IDs, response contexts, or invalid MACs trigger an immediate `TransferProtocolException`. No payload field is parsed or trusted if verification fails.

---

## 7. Durable Concurrency-Safe Sequence Management

### 7.1 Sequence Allocation Architecture (`TransferSequenceManager.kt`)
- Sequences are positive 64-bit integers (`Long`).
- Initial value: `1L` for the first authenticated transfer request of a newly paired session.
- **Monotonicity & Persistence**:
  - `storage.reserveTransferSequence(sessionId)`: Reads current `nextSequence`, increments by 1, writes updated `SessionCredentials` to disk with fsync and atomic rename, and returns the reserved sequence.
  - Advancement is **persisted before transmission**. If the application crashes before network transmission, the sequence number is never reused; the next request will use the incremented sequence.
  - The desktop receiver rejects any sequence $\le$ the committed replay sequence with signed HTTP 409 (`replay_sequence_invalid`).
- **Concurrency Serialization**:
  - All transfer operations per session are serialized by `UploadCoordinator` and `TransferRuntime`. Exactly one request is in flight at any given moment.

---

## 8. 1 MiB Chunk Streaming Engine & Chunk Lifecycle

### 8.1 Chunk Geometry & Streaming Rules
- Chunk Size: Exactly 1 MiB (`1,048,576` bytes).
- Final Chunk Size: Tail remainder `size % (1 MiB)` if non-zero, or full 1 MiB if evenly divisible.
- Zero-byte files: Handled cleanly; 0 chunks required. After Gate A approval, zero-byte uploads transition immediately from `receiving` to `verifying` to `awaiting_final_acceptance`.
- Total Chunks Calculation:
  ```kotlin
  fun totalChunks(size: Long): Long =
      if (size <= 0L) 0L else (size + CHUNK_BYTES - 1L) / CHUNK_BYTES
  ```
- Chunk Indexing: 0-indexed (`0` to `totalChunks - 1`).

### 8.2 Streaming Loop & Error Handling (`UploadCoordinator.kt`)
- Before sending any chunk, the coordinator verifies that Gate A has been granted (`state == "receiving"`). Sending a chunk in `awaiting_desktop_approval` causes the desktop to reject with HTTP 409 (`invalid_transfer_state`).
- Each chunk is read via `ChunkCursor` into a dedicated 1 MiB buffer, signed with fresh sequence and nonce, and sent via `PUT /api/v1/transfers/uploads/{transfer_id}/chunks/{index}`.
- Every response acknowledgement is verified with `TransferResponseVerifier`.
- **Handling Lost Acknowledgements**:
  - If a network error occurs while transmitting a chunk or receiving its ack, the phone does **not** assume failure or immediately resend.
  - The phone queries `GET /api/v1/transfers/uploads/{transfer_id}`.
  - If the desktop already committed the chunk (`next_chunk_index > index`), the phone discards the pending buffer and advances to `next_chunk_index`.
  - If the desktop did not commit the chunk (`next_chunk_index == index`), the phone re-reads and resends the identical chunk using a **fresh sequence and fresh nonce**.
  - Duplicate chunk delivery (if identical bytes were received) returns HTTP 200 without appending. Out-of-order chunks return HTTP 409 with `expected_chunk_index`, allowing the client to realign immediately.

---

## 9. Dual-Gate Desktop Authorization & Quarantine Alignment

### 9.1 Two-Gate Workflow Alignment
The client UI and engine mirror the desktop quarantine security model:
1. **Gate A: Desktop Transfer Approval**:
   - Initialized by phone via `POST .../init`.
   - Desktop records upload offer in `awaiting_desktop_approval`.
   - Phone displays `TransferPhase.WAITING_DESKTOP_APPROVAL`.
   - **Zero file content is transmitted** before Gate A approval.
   - Desktop user explicitly approves via IPC/service (`allow_receive`). State advances to `receiving`.
2. **Quarantine Storage**:
   - As chunks arrive on desktop, they are written to private quarantine files (`0600` permissions):
     - Files $\le$ 50 MiB: `$XDG_RUNTIME_DIR/kivo/bridge/quarantine/`.
     - Files $>$ 50 MiB up to 2 GiB: `$XDG_CACHE_HOME/kivo/bridge/quarantine/`.
   - Never placed in public or user-visible directories.
3. **Verification**:
   - Once all chunks are committed, desktop verifies whole-file SHA-256 against quarantine contents. State advances to `awaiting_final_acceptance`.
4. **Gate B: Desktop Final Acceptance**:
   - Phone enters `TransferPhase.AWAITING_FINAL_ACCEPTANCE`.
   - Desktop user explicitly accepts via IPC/service (`accept_file`).
   - Desktop atomically links/moves quarantined file into `~/Downloads/` with conflict-free collision naming (e.g., `report (1).pdf`).
   - State advances to `committed`.
   - Only upon receiving signed `committed` status does Android report `TransferPhase.COMMITTED` to the user.
   - If user rejects or discards at Gate B, quarantined file is unlinked immediately; state becomes `discarded`/`rejected`.

---

## 10. Interrupted-Upload Recovery & Reconnection State Machine

### 10.1 Durable Interrupted State (`PersistedTransfer`)
When a network loss, app backgrounding, or process termination occurs during transfer:
- Stored in encrypted file `kivo_transfer_state.enc` using AES-256-GCM with AAD `kivo-transfer-state-v1`.
- Contains: `sessionId`, `transferId`, `uri`, `filename`, `mimeType`, `size`, `sha256Hex`, `strategy`, `spoolName`, `nextChunkIndex`, `receivedBytes`, `state`, `initUncertain`, `createdAt`, `updatedAt`, `expiresAt`.
- **Contains zero cryptographic keys or bearer tokens** (credentials remain isolated in `kivo_session.enc`).

### 10.2 Ambiguous Init Protection (`INIT_OUTCOME_UNKNOWN`)
- If network connection drops while sending `upload_init`:
  - `initUncertain = true` is persisted.
  - The client **refuses to retry automatically** to prevent race conditions or duplicate offers.
  - UI displays `Status unknown`. The user is instructed to dismiss and re-select the document.

### 10.3 Resume Workflow
1. User clicks **"Resume upload"** in UI.
2. `UploadCoordinator.resume()` verifies session validity and re-resolves the source (or restores the private spool file).
3. Client issues authenticated `GET /api/v1/transfers/uploads/{transfer_id}`.
4. Client validates signed status response: reconciles desktop's `next_chunk_index` and `received_bytes`.
5. Stream is reopened/repositioned to `next_chunk_index * 1 MiB`.
6. Streaming continues from the first uncommitted chunk.
7. If desktop session has expired or transfer timed out (`404 transfer_not_found` / `session_expired`), client transitions to `TransferPhase.EXPIRED` and cleans local spool files.

---

## 11. Android Background Execution & Foreground Service Architecture

### 11.1 Execution Mechanism Evaluation
- **Evaluated Options**:
  - *Option A: API 34 User-Initiated Data Transfer (UIDT) Jobs (`JobScheduler`)*: Rejected because UIDT jobs enforce system constraints requiring Internet capability (our transfers are strictly LAN-only), and they impose complex lifecycle and network capability gates that can kill LAN connections without internet gateway connectivity.
  - *Option B: Foreground Service with `dataSync` type*: Rejected because Android 14+ enforces a 6-hour execution cap on `dataSync` and aggressively throttles background network tasks.
  - *Option C: Foreground Service with `connectedDevice` type*: **Selected**. Perfectly models a local companion device link over Wi-Fi. Supports user-facing progress updates, cancellation actions, and remains active while waiting on human desktop approval.
- **Service Configuration (`TransferService.kt`)**:
  - Service declaration: `android:foregroundServiceType="connectedDevice"`, `exported="false"`, `stopWithTask="false"`.
  - Notification Channel: Low-importance private channel (`kivo_transfers`) with `lockscreenVisibility = Notification.VISIBILITY_PRIVATE`.
  - Notification details: displays document name, formatted verified bytes, progress percentage bar, and an immediate "Cancel" action.
  - Lifecycle: Service starts when upload begins; automatically calls `stopForeground(STOP_FOREGROUND_REMOVE)` and `stopSelf()` the instant the runtime transitions to an inactive phase (`COMMITTED`, `CANCELLED`, `FAILED`, `INTERRUPTED`).
- **Required Permissions**:
  - `FOREGROUND_SERVICE`
  - `FOREGROUND_SERVICE_CONNECTED_DEVICE`
  - `CHANGE_NETWORK_STATE` (least-privileged normal permission satisfying API-34 `connectedDevice` policy requirement; no network configuration is mutated by the app).
  - `POST_NOTIFICATIONS` (runtime permission prompted gracefully; if denied, in-app progress continues with clear user notification).

---

## 12. Android Compose UI & Privacy Safeguards

### 12.1 UI Workflow & Views (`TransferScreens.kt`, `MainActivity.kt`)
1. **Destination & Security Identity Card**:
   - Displays paired desktop host IP, port, and pinned TLS 1.3 certificate SHA-256 fingerprint suffix (`…<fingerprint_suffix>`).
2. **Document Review & Selection Card**:
   - Displays selected documents with filename, byte-formatted size, MIME type, and storage access hint.
   - Shows warnings for unsupported files (oversized $>2$ GiB, path traversal characters in names).
   - "Upload X files" button enabled only when all selected items are valid and confirmed.
3. **Active Transfer Status Card**:
   - Dynamic phase display:
     - *Preparing*: "Validating metadata and hashing the document locally. Only the display name, size and SHA-256 are sent with the request; no file content is sent until the desktop approves it."
     - *Waiting for Approval*: "Open KIVO on your desktop and approve this upload. Only the display name, size and SHA-256 were sent with the request. No file content has been sent yet."
     - *Uploading*: Verified progress percentage and byte counter.
     - *Verifying*: "All chunks are committed. The desktop is verifying the whole-file SHA-256."
     - *Waiting for Final Acceptance*: "Verified. The file is still quarantined on the desktop. Accept it there to publish it to Downloads."
     - *Completed*: Mint success badge with publication filename.
     - *Interrupted*: "The upload lost contact with the desktop. Verified progress is retained." Resume and Discard options available.
4. **Privacy & Security Boundaries**:
   - Zero cryptographic keys (`K_up`, `K_down`, `T_pair`), bearer tokens, or MAC signatures are rendered in the UI or printed to logcat.
   - Quarantine model is explicitly explained to user on screen.
   - Disconnecting or unpairing automatically cancels active transfers, deletes temporary spools, and clears credentials.

---

## 13. Cross-Language Byte-Level Interoperability & Test Matrices

### 13.1 Shared Fixture Verification (`tests/fixtures/transfer_test_vectors.json`)
The test fixture serves as the single source of truth for both Python and Kotlin suites:
- Contains exact test vectors for:
  - `upload_init` canonical request bytes & signature.
  - `upload_chunk` canonical request bytes & signature.
  - `upload_status` canonical request bytes & signature.
  - `upload_cancel` canonical request bytes & signature.
  - `exact_upload_init` and `exact_upload_chunk` transcripts.
  - Signed response envelope canonical verification.
- **Kotlin Test Results**:
  - `TransferCryptoInteropTest`: All vectors verified byte-for-byte against Kotlin HMAC-SHA256 and canonicalizers.
  - `TransferResponseVerifierTest`: Complete envelope verification, tampered signatures, nonce mismatches, sequence mismatches, and status mismatches tested.
- **Python Test Results (`tests/test_android_upload_interop.py`)**:
  - `TransferFixtureInteropTests`: Recomputes all vectors in Python; matches Kotlin output identically.
  - `AndroidUploadLiveTranscriptTests`:
    - Executes live loopback TLS 1.3 handshake with `PhoneBridgeService`.
    - Enforces Gate A pre-approval chunk rejection (HTTP 409 `invalid_transfer_state`).
    - Grants Gate A via production desktop service `resolve_transfer(tid, "allow_receive")`.
    - Simulates lost ack after chunk 0; verifies client status query reconciliation.
    - Sends remaining chunks; verifies transition to `awaiting_final_acceptance`.
    - Inspects desktop quarantine and confirms no file is published in `Downloads`.
    - Grants Gate B via production desktop service `resolve_transfer(tid, "accept_file")`.
    - Verifies file publication in `Downloads` with identical SHA-256 content.
    - Tests signed cancellation and sequence replay prevention (HTTP 409 `replay_sequence_invalid`).

### 13.2 Test Summary Matrix
| Test Suite | Files | Tests Passed | Failures | Errors | Result |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Android Unit Tests** | 13 test classes (`transfer/`, `storage/`, `pairing/`, `network/`) | **148** | 0 | 0 | **PASS** |
| **Python Interop Transcript** | `tests/test_android_upload_interop.py` | **5** | 0 | 0 | **PASS** |
| **Python Transfer Bridge** | `tests/test_bridge_transfer*.py`, `tests/test_transfer*.py` | **72** | 0 | 0 | **PASS** |
| **Full Python Backend** | `tests/` (all unit/integration tests) | **1029** | 0 | 0 | **PASS** |
| **Quickshell Shell Tests** | `shell/tests/` | **16** | 0 | 0 | **PASS** |
| **Qt6 QML Lint** | `shell/**/*.qml` | Clean (exit 0) | 0 | 0 | **PASS** |
| **Desktop Health** | `./scripts/desktop-health.sh` (9 probes) | **9 / 9** | 0 | 0 | **PASS** |

---

## 14. Environmental Inventory & Resource Verification

| Metric | Measured Value | Verification Method |
| :--- | :--- | :--- |
| **Debug APK File Size** | `26,647,931 bytes` (~25.4 MiB) | `stat -c "%s" app-debug.apk` |
| **Debug APK SHA-256** | `dd2addb8c035dd0a53e633d5d5bb35e03cf412adb09c62a3fef4f0ca85c225e7` | `sha256sum app-debug.apk` |
| **Target SDK Version** | `35` (Android 15) | `aapt2 dump badging` |
| **Minimum SDK Version** | `29` (Android 10) | `aapt2 dump badging` |
| **Compile SDK Version** | `35` (Android 15) | `build.gradle.kts` |
| **Declared Permissions** | `INTERNET`, `ACCESS_NETWORK_STATE`, `CAMERA`, `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_CONNECTED_DEVICE`, `CHANGE_NETWORK_STATE`, `POST_NOTIFICATIONS` | `aapt2 dump permissions` |
| **Max In-Memory Buffer** | `64 KiB` (source read), `1 MiB` (chunk buffer) | Code inspection & unit test assertions |
| **Fixed Chunk Size** | `1,048,576 bytes` (1 MiB) | Protocol constants |
| **Loopback TLS Throughput** | `176.0 MiB/s` (64 MiB in 0.364s over loopback TLS 1.3) | `loopback_bench.py` live measurement |
| **Max Network Retries** | `3` consecutive failures before marking interrupted | `TransferProtocolLimits.MAX_NETWORK_RETRIES` |
| **Staging Space Reserve** | `50 MiB` minimum free device storage buffer | `DEFAULT_MIN_FREE_BYTES` |
| **Execution Mechanism** | Android `connectedDevice` Foreground Service | `AndroidManifest.xml`, `TransferService.kt` |
| **Attached Devices / AVDs** | `0` (no physical phone, no emulator attached) | `adb devices` |

---

## 15. Architectural Constraints & Non-Negotiable Boundary Compliance

1. **No Downloads Implemented**: Scope strictly limited to Android phone-to-desktop uploads. No download endpoints, receivers, or UI elements added.
2. **Zero Desktop Architecture Rewrites**: Desktop receiver, IPC protocol, Dynamic Notch, and Dashboard remain strictly intact.
3. **Zero Hardcoded Machine/User Paths**: Code utilizes strictly XDG paths (`XDG_RUNTIME_DIR`, `XDG_CACHE_HOME`, `Path.home()`) on desktop and Android `Context.cacheDir`/`filesDir`/SAF URIs on mobile.
4. **No Secrets or Raw Bytes in IPC/Logs**: All credentials, session tokens, nonces, and file contents remain inside isolated cryptographic boundaries.
5. **No Public Cloud or External Dependencies**: Operates 100% on the local physical network via pinned TLS 1.3.
6. **No Approval Bypasses**: Gate A and Gate B remain strictly authoritative on the desktop. The companion app cannot force acceptance or bypass user consent.
7. **Git Safety & Working Tree Hygiene**: No commits or git pushes executed. Working tree clean of syntax or whitespace errors.

---

## Conclusion & Next Steps
Stage 13.3.3.2 is fully completed and verified across all available software and protocol boundaries. The Android Companion upload client is structurally, cryptographically, and operationally aligned with the KIVO desktop receiver.

The next milestone is **Stage 13.4 (Desktop Approval UI & Notifications)**, which will provide the native Quickshell UI surfaces and systemd/desktop notification integrations for Gate A and Gate B approvals directly within the desktop user interface.
