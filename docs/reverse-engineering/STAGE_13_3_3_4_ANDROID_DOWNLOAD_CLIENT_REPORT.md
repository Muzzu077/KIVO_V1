# STAGE 13.3.3.4: Android Secure Download Client Report

**Date:** 2026-11-11
**Status:** Implemented and verified with JVM/unit, build, lint, and desktop gates
**Verdict:** **PASS WITH LIMITATIONS**

---

## 1. Executive Summary & Verdict

### 1.1 Objective

Implement the Android side of the KIVO desktop-to-Android download path. The
client must consume a narrow, versioned `kivo-download://v1` ticket handoff,
reuse the already-verified transfer authentication and replay protection,
stream and verify bounded chunks into app-private staging, publish only through
the Storage Access Framework (SAF) after explicit user selection, recover
interrupted work from encrypted durable state, and never fabricate progress or
completion.

### 1.2 Delivered

| Area | Delivered |
|---|---|
| Ticket handoff | Strict `kivo-download://v1` parser in Kotlin, byte-compatible with the Python builder/parser; duplicates, unknown parameters, foreign schemes, unsupported versions, malformed tickets, oversized payloads, and device mismatches are rejected. |
| Download engine | `DownloadCoordinator` implements claim → signed status/chunk streaming → per-chunk digest verification → whole-file digest verification → signed desktop complete → explicit SAF destination → publication → staging deletion. |
| Staging | App-private `filesDir/kivo-downloads`, random 128-bit names, exact-size reservation, per-chunk `fsync`, symlink and canonical-path rejection, bounded 64 KiB I/O, orphan cleanup. |
| Publication | `ACTION_CREATE_DOCUMENT` with `CATEGORY_OPENABLE`; copy-with-hash, flush/sync/close, optional reopen verification, destination deletion on failure/cancel, staging retained until success. |
| Recovery | Encrypted `kivo_download_state.enc`; restart projects `INTERRUPTED` or `READY_TO_PUBLISH`; resume reconciles against signed desktop status; publication is never automatic. |
| Background execution | Reuses the existing single `connectedDevice` foreground service for network downloads; local SAF publication deliberately does not start it. |
| UI | QR scan → offer card → truthful status card → explicit destination picker; no ticket, token, key, or HMAC material is rendered. |
| Interoperability | Deterministic Python/Kotlin fixtures for init, status, chunk, complete, cancel, signed errors, and handoff. |
| Compatibility | The only protocol-adjacent Android change is widening the signed response `tid` bound from 64 to 68 characters so a `tkt_<64 hex>` ticket identity can be carried by the existing envelope. No desktop route or schema was added or redesigned. |

### 1.3 Not Delivered (Explicitly Out of Scope or Deferred)

- No desktop QML/QR authorization screen. Stage 13.4 owns the user-facing
  desktop ticket issuance UI; this stage uses synthetic authenticated handoff
  fixtures and the already-verified backend.
- No upload protocol redesign, no new desktop download routes, no second
  backend, and no second shell connection.
- No physical-device or emulator validation. `adb devices` reports no attached
  device, so instrumentation, real SAF providers, real background eviction,
  and measured on-device throughput are unavailable.
- No claim that a download can resume across a desktop backend restart. The
  client reconciles only against the signed status the desktop actually
  supports.
- No claim of bidirectional real-device transfer. Upload and download are each
  verified by JVM tests and shared protocol fixtures, not by an authorized
  physical-device run.

### 1.4 Final Verdict

**PASS WITH LIMITATIONS.** The Android client is implemented, compiles, passes
213 JVM unit tests (65 download-related), passes Android lint with zero errors,
builds debug and unsigned release APKs, and all desktop verification gates pass.
The missing physical-device/instrumentation run and the absent Stage 13.4
desktop QR UI are the reasons the verdict is not an unqualified PASS.

---

## 2. Scope, Architecture & Boundary Compliance

### 2.1 Component Map

| Component | File | Responsibility |
|---|---|---|
| Ticket parser | `android-companion/.../transfer/DownloadTicketParser.kt` | Strict handoff parsing; returns `DownloadOffer` metadata only. |
| Download engine | `android-companion/.../transfer/DownloadCoordinator.kt` | Claim, stream, verify, complete, publish, cancel, recover. |
| Durable state | `android-companion/.../transfer/DownloadState.kt` | `PersistedDownload`, validation, chunk geometry, recovery store, orphan cleanup. |
| Staging | `android-companion/.../transfer/DownloadStager.kt` | Random app-private stage, reserve, append, hash, truncate, delete. |
| Publication | `android-companion/.../transfer/DownloadDestination.kt` | Publisher, SAF abstraction, optional destination re-hash. |
| SAF adapter | `android-companion/.../transfer/AndroidDownloadDestination.kt` | `content://` resolver, sink, reopen, delete. |
| Runtime integration | `android-companion/.../transfer/TransferRuntime.kt` | Process-wide single-operation ownership, recovery, unpair cleanup. |
| Foreground service | `android-companion/.../transfer/TransferService.kt` | Existing `connectedDevice` service and notification state. |
| ViewModel | `android-companion/.../transfer/TransferViewModel.kt` | Handoff acceptance, confirmation, resume, cancel, publish, dismiss. |
| UI | `android-companion/.../ui/screens/TransferScreens.kt` | Scanner, offer card, status card, destination picker. |
| Activity wiring | `android-companion/.../MainActivity.kt` | Collects snapshots and connects UI callbacks. |
| Crypto/transport | `TransferCrypto.kt`, `TransferTransport.kt`, `TransferResponseVerifier.kt` | Existing authenticated request/response layer, bounded download contexts. |

### 2.2 Data Flow

```
Desktop (Stage 13.3.3.3 backend)
  │  kivo-download://v1?ticket=…&device=…&name=…&size=…&sha256=…
  ▼
Android QR scanner
  │  DownloadTicketParser.parse(raw, localDeviceId)
  ▼
DownloadOffer (metadata only; no request yet)
  │  user taps Download
  ▼
DownloadCoordinator.start(offer)
  │  init (claim) → signed status → chunks 0..N-1 → whole-file hash
  │  → signed complete → READY_TO_PUBLISH (durable)
  ▼
ACTION_CREATE_DOCUMENT picker (user chooses destination)
  ▼
DownloadPublisher: copy + hash → flush/sync/close → optional reopen verify
  ▼
Mark completed → delete app-private staging
```

### 2.3 Boundary Compliance

- **No new desktop routes.** The existing routes
  `/api/v1/transfers/downloads/init`,
  `/api/v1/transfers/downloads/{id}`,
  `/api/v1/transfers/downloads/{id}/chunks/{index}`,
  `/api/v1/transfers/downloads/{id}/complete`, and
  `/api/v1/transfers/downloads/{id}/cancel` are reused unchanged.
- **No desktop QML.** All new UI is Android Compose.
- **No upload redesign.** Upload classes are untouched except for the shared
  transaction gate and the `tid` bound clarification.
- **No raw paths over IPC.** The handoff contains display metadata and an opaque
  ticket only. The desktop receives no Android destination path.
- **No broad storage permission.** The manifest declares no
  `READ_EXTERNAL_STORAGE`, `WRITE_EXTERNAL_STORAGE`, or
  `MANAGE_EXTERNAL_STORAGE`.
- **No second backend/shell.** Android continues to use one paired session and
  one process-wide runtime.
- **No commits or pushes.** Working tree retained; no Git history was changed.

---

## 3. Threat Model & Security Invariants

### 3.1 Threats Addressed

| Threat | Control |
|---|---|
| Forged or replayed QR handoff | Versioned scheme, strict field set, local device binding, opaque ticket syntax, no authorization from scanning alone. |
| Ticket theft or cross-session use | Desktop binds the ticket to one session and paired device; Android verifies signed `device_id`, `download_id`, filename, size, and digest. |
| Request replay or sequence reuse | Existing `K_up` request MAC with persisted monotonic sequence, fresh 16-byte nonce, timestamp window, and one shared request gate. |
| Response tampering | Every response envelope is verified with `K_down`, including context, session, sequence, nonce, payload digest, and MAC. |
| Chunk substitution / truncation / reordering | Signed chunk index, offset, length, and `chunk_sha256` are checked before write; whole-file SHA-256 is checked before completion. |
| Path traversal / staging escape | Random stage names matched by regex; symlink rejection; canonical parent equality; no user-controlled stage path. |
| Silent overwrite | Publication always uses `ACTION_CREATE_DOCUMENT`; the provider owns name collision behavior; the app never derives a filesystem path. |
| Partial destination reported as success | Destination is written, flushed, synced, closed, and optionally reopened/re-hashed before `COMPLETED`. |
| Duplicate committed data | Completion is requested before the picker; publication is not automatic; interrupted publication returns to the picker. |
| Disk exhaustion | 2 GiB protocol maximum, exact-size reservation, and a 32 MiB free-space reserve before staging. |
| Background microphone/recording | Download does not touch audio capture; only the existing connected-device transfer service is used. |
| Secret leakage into UI/logs | No ticket, bearer token, key, nonce, or HMAC is displayed; offer/status cards show only display metadata. |

### 3.2 Security Invariants

1. Scanning a QR code never sends a request. Only `confirmDownload()` after the
   offer card starts an operation.
2. The ticket is opaque and is never accepted as standalone authorization.
3. `K_up` signs requests and `K_down` verifies responses. The pairing key, poll
   key, and bearer token are never used as MAC keys.
4. At most one upload or download operation runs per process. The shared
   `TransferRequestGate` serializes authenticated requests because upload and
   download share one durable sequence counter and one desktop replay window.
5. Every committed chunk is fsynced and its durable record is updated before
   progress is shown.
6. `READY_TO_PUBLISH` is not `COMPLETED`. The UI must not claim a saved file
   until the destination write finishes.
7. Cancellation removes app-private staging; a completed desktop ticket is not
   un-completed locally, but publication never proceeds without user action.
8. Session change or unpair removes staging and encrypted download state.

---

## 4. Ticket Handoff (`kivo-download://v1`)

### 4.1 Payload

The desktop builder emits exactly:

```
kivo-download://v1?ticket=<tkt_…>&device=<client_…>&name=<url-encoded>&size=<decimal>&sha256=<64 hex>
```

| Parameter | Meaning | Validation |
|---|---|---|
| `ticket` | Opaque 256-bit capability reference | `^tkt_[0-9a-f]{64}$` |
| `device` | Target paired Android device | `^client_[A-Za-z0-9_.-]{1,57}$` and local-device equality |
| `name` | Display filename only | Existing `validateDisplayFilename`; no path separators or controls |
| `size` | Exact byte length | Canonical decimal, `0..2 GiB` |
| `sha256` | Whole-file digest | 64 lowercase hex characters |

The payload carries no desktop path, session ID, bearer token, session key,
nonce, or file bytes. The desktop UI that renders it is Stage 13.4; this stage
uses the shared deterministic fixture.

### 4.2 Strict Parser Rules

`DownloadTicketParser` mirrors `kivo/bridge/download_handoff.py`:

- exact scheme and exact `v1`; any other `kivo-download://` version returns
  `unsupported_version`;
- no surrounding whitespace, no fragment, no empty query;
- exactly one `=` per component; no empty key/value;
- strict percent-decoding with malformed escapes rejected;
- duplicate keys rejected (`duplicate_parameter`);
- the parameter set must be exactly `ticket`, `device`, `name`, `size`,
  `sha256`; unknown or missing keys are rejected;
- payload bounded to 2048 UTF-8 bytes before parsing;
- non-canonical integers (`01`, `+1`, whitespace) rejected;
- local device binding is enforced when the expected device ID is supplied;
- errors never echo the rejected payload.

### 4.3 No Browsing or Listing

The Android client has no ticket list, ticket browser, or ticket enumeration
operation. A ticket enters the app only through an explicit scan and is held in
memory as a pending offer until the user confirms or dismisses it. The offer is
cleared on confirmation, dismissal, or ViewModel reset.

### 4.4 Adversarial Coverage

`DownloadTicketParserTest` (11 tests) covers the shared vector, duplicate
parameters, unknown/missing parameters, unsupported versions and foreign
schemes, fragments and malformed separators, malformed tickets, size/digest
bounds, strict percent encoding (including path and control smuggling),
oversized payload, device mismatch, and a check that the payload contains no
path, secret, or key material. The Python side has the matching
`tests/test_bridge_download_handoff.py` suite and a shared fixture assertion.

---

## 5. Download Lifecycle & State Machine

### 5.1 User-Visible Phases

| Phase | Meaning | Terminal |
|---|---|---|
| `IDLE` | No download state. | — |
| `OFFER` | Model-level offer state; the UI holds the pending offer separately. | — |
| `PREPARING` | Claiming the ticket, checking signed status, reserving staging. | — |
| `DOWNLOADING` | Receiving verified chunks. | — |
| `RECONNECTING` | Bounded retry or signed status reconciliation. | — |
| `VERIFYING` | Whole-file SHA-256 comparison. | — |
| `READY_TO_PUBLISH` | Whole file verified and desktop completion acknowledged; destination not yet chosen. | — |
| `PUBLISHING` | Writing the verified copy to the selected SAF URI. | — |
| `COMPLETED` | Destination write finished and closed; optional re-hash result recorded. | Yes |
| `CANCELLED` | User cancelled; staging removed; destination removed where possible. | Yes |
| `EXPIRED` | Signed desktop/session expiry or terminal not-found. | Yes |
| `FAILED` | Terminal local/protocol failure; staging removed. | Yes |
| `INTERRUPTED` | Retryable loss of connectivity or app restart; staging and verified progress retained. | No |

### 5.2 Ordering

The ordering is deliberate and enforced in code:

1. **Claim** with `POST /downloads/init` using the opaque ticket.
2. **Stream** chunks in order, verifying every signed chunk digest before
   writing.
3. **Verify** the staged file against the signed whole-file SHA-256.
4. **Acknowledge completion** with the signed desktop `complete` route *before*
   any destination is chosen.
5. **Choose destination** through `ACTION_CREATE_DOCUMENT`.
6. **Publish** the verified copy.
7. **Delete staging** only after successful publication.

A destination is never selected or written before desktop completion. A failed
publication therefore cannot duplicate a partially committed remote transfer.

### 5.3 Durable Record

`PersistedDownload` stores:

- `sessionId`, `ticketId`, `deviceId`
- display `filename`, optional `fileType`
- exact `size` and signed `sha256Hex`
- `nextChunkIndex`, `receivedBytes`, `chunkBytes`
- random `stageName`
- `state`, `completeAcknowledged`, optional `publishedName`, `errorCode`
- `createdAt`, `updatedAt`, `leaseExpiresAt`

The record is encrypted by `KeystoreSecretStorage` in
`kivo_download_state.enc`. `fromJson` rejects wrong record types/versions,
malformed identifiers, path-like filenames, invalid digests, inconsistent chunk
geometry, unknown states, invalid timestamps, and verified states with
incomplete bytes.

### 5.4 Empty Files

An empty file has `size = 0`, `totalChunks = 0`, and the SHA-256 of empty input.
No chunk request is made. Staging is prepared at length zero, the whole-file
digest is verified, desktop completion is requested, and publication writes an
empty destination. `DownloadCoordinatorTest` and `DownloadPublisherTest` each
cover the empty-file path explicitly.

### 5.5 Lease and Expiry

From the desktop contract:

- ticket redemption TTL: **300 s**;
- active-transfer lease: **1800 s**, never extended;
- idle timeout: **30 s**, unchanged;
- terminal retention: **300 s**;
- maximum concurrent downloads: **2**; maximum active downloads per session:
  **8**; registry maximum: **32**.

Android treats signed `active_expires_at` as the effective lease when present
and falls back to `expires_at`. It does not extend the lease. A signed expiry,
idle timeout, session revocation, or not-found result is terminal and cleans up
local staging.

---

## 6. Authentication, Replay Protection & Response Integrity

### 6.1 Reused Cryptography

No new key hierarchy was introduced.

| Item | Use |
|---|---|
| `K_up` | Request MAC for init/status/chunk/complete/cancel. |
| `K_down` | Response envelope MAC verification. |
| Bearer token | Transport authentication only; never a MAC key. |
| Pairing key | Not used for download requests. |
| Poll key | Not used for download requests. |

Kotlin uses the existing `TransferCrypto.buildCanonicalRequest`,
`computeRequestSignature`, `TransferSequenceManager`, and
`TransferResponseVerifier`. The download init body is exactly:

```json
{"ticket_id":"tkt_<64 hex>"}
```

Status, chunk, complete, and cancel use an empty body with content length 0 and
the SHA-256 of empty input.

### 6.2 Request Binding

Each authenticated request binds:

- protocol version `1` and exact endpoint;
- session ID;
- HTTP method;
- 16-byte nonce;
- monotonic sequence reserved and persisted before transmission;
- timestamp;
- content length and content SHA-256;
- `K_up` HMAC over the canonical bytes.

The shared `TransferRequestGate` holds a process-wide lock around sequence
reservation and request execution, so an upload and a download cannot race the
same durable sequence counter. `TransferRuntime` additionally permits only one
active operation at a time.

### 6.3 Response Verification

Every response is parsed only after `TransferResponseVerifier` validates:

- envelope version, context, session ID, sequence, and nonce;
- payload length and SHA-256;
- `K_down` MAC over the canonical response;
- bounded payload size before decoding.

Download contexts are `download_init`, `download_status`, `download_chunk`,
`download_complete`, and `download_cancel`.

### 6.4 `tid` Compatibility Clarification

Upload responses name `tr_<24 hex>` (27 characters). A download response names
the opaque `tkt_<64 hex>` ticket (68 characters). The verifier previously
rejected any transfer ID over 64 characters. It now accepts up to 68 and leaves
exact identity enforcement to each engine:

- upload rejects anything other than the expected `tr_…`;
- download requires `response.transferId == record.ticketId` and
  `payload.download_id == record.ticketId`, plus `device_id`, filename, size,
  SHA-256, state, chunk geometry, and progress consistency.

This is a bounded validation correction, not a protocol redesign.

### 6.5 Signed Errors

A signed non-2xx response is authenticated and routed by error code:

| Code family | Android result |
|---|---|
| `download_completed` | Verify staged copy; if valid, offer publication without re-downloading. |
| `download_expired`, `download_not_found`, `download_not_claimed`, `download_idle_timeout`, `session_expired`, `session_invalid` | `EXPIRED`, cleanup. |
| `download_forbidden`, `download_source_unavailable`, `download_source_changed`, `download_integrity_failed`, `download_unavailable`, `concurrent_download_limit` | `FAILED`, cleanup. |
| `download_incomplete`, `chunk_out_of_order` | Bounded reconciliation, then fail if unresolved. |

Unsigned or malformed bodies never reach the engine; they raise a protocol
failure and staging is cleaned before any success is shown.

---

## 7. HTTP Contract & Interoperability Fixtures

### 7.1 Routes Reused

| Operation | Method and path | Request body |
|---|---|---|
| Init/claim | `POST /api/v1/transfers/downloads/init` | `{"ticket_id":"tkt_…"}` |
| Status | `GET /api/v1/transfers/downloads/{ticket_id}` | empty |
| Chunk | `GET /api/v1/transfers/downloads/{ticket_id}/chunks/{index}` | empty |
| Complete | `POST /api/v1/transfers/downloads/{ticket_id}/complete` | empty |
| Cancel | `POST /api/v1/transfers/downloads/{ticket_id}/cancel` | empty |

All routes require the existing paired bearer token plus a valid `K_up`
request signature. No Android-specific desktop route was added.

### 7.2 Response Payload

The signed `public_download` payload includes:

```
download_id, device_id, device_name, filename, file_type, size, sha256,
state, total_chunks, chunk_size, next_chunk_index, bytes_sent,
progress_percent, created_at, updated_at, expires_at,
active_expires_at, claim_expires_at, error_code
```

Chunk responses additionally include:

```
chunk_index, offset, length, chunk_sha256, chunk_b64
```

The 68-character ticket ID is carried in the envelope `tid`; the payload does
not add a separate `transfer_id`.

### 7.3 Deterministic Fixtures

`tests/fixtures/download_interop_vectors.json` contains fixed, synthetic
vectors:

- init, status, chunk, complete, and cancel request canonical bytes and `K_up`
  signatures;
- signed success envelopes for init, status, chunk, and complete;
- signed cancel and expired-error envelopes;
- fixed 1234-byte pattern file with chunk digest and whole-file digest;
- fixed session, device, ticket, timestamp, nonce, and sequence values.

`DownloadInteropTest` (7 tests) proves byte-for-byte agreement for the init
body, all request canonical bytes and MACs, all signed response verifications,
tamper rejection, chunk/file digest consistency, path-free public metadata, and
that a chunk response stays inside the bounded download envelope.

`tests/fixtures/download_handoff_vectors.json` contains the exact QR payload
and parameters. `DownloadTicketParserTest` and
`tests/test_bridge_download_handoff.py` consume the same vector.

Focused backend evidence:

- `tests/test_bridge_download.py`
- `tests/test_bridge_download_handoff.py`
- `tests/test_bridge_download_ipc.py`

combined: **56 passed**.

- `tests/test_download_interop_fixtures.py` plus the shared Android interop
  fixture checks: **9 passed**.

---

## 8. Android Transport & Bounded Memory

### 8.1 Transport Limits

| Limit | Value | Purpose |
|---|---|---|
| Protocol chunk | 1 MiB (`1_048_576`) | Maximum plaintext chunk held for one request. |
| Download chunk payload | 1.5 MiB (`1_572_864`) | Maximum signed JSON payload for a chunk. |
| Download response envelope | 2 MiB (`2_097_152`) | Bounded HTTP response read before parse. |
| Control response envelope | 64 KiB | Init/status/complete/cancel bound. |
| Chunk base64 string | 1.5 million chars | Rejected before decoding. |
| Copy buffer | 64 KiB | Staging hash, publication copy, destination verify. |
| Maximum download | 2 GiB | Matches desktop protocol maximum. |
| Minimum free reserve | 32 MiB | Checked before reserving staging. |
| Network retries | 5 | Per operation, with 500 ms → 8 s capped backoff. |

### 8.2 Memory Behavior

- The client never loads a whole file into memory.
- A download holds at most the current network chunk plus its decoded byte
  array and the signed envelope text; both are bounded by the limits above.
- Disk staging is the file-sized store.
- Hashing and publication use fixed 64 KiB buffers.
- The response reader reads at most `maxResponseBytes` and aborts oversized
  bodies.
- The durable state file is small metadata only.

### 8.3 Measured vs. Bounded

These are design bounds, not profiled measurements. No Android device or
emulator was available, so no heap dump or `dumpsys meminfo` result is claimed.
A device run must confirm that transient decode allocations remain within the
chosen 2 MiB envelope under real ART behavior.

---

## 9. App-Private Staging & Chunk Integrity

### 9.1 Stage Creation and Reservation

- Directory: `filesDir/kivo-downloads`.
- Name: `kivo_download_<32 lowercase hex>.part` from 16 random bytes.
- The directory must exist or be creatable, be a directory, and not be a
  symlink.
- `prepare(stage, size)` validates the name and stage, checks the 2 GiB bound,
  checks `usableSpace >= size + 32 MiB`, sets the exact file length, and fsyncs.
- Empty files reserve length zero without a free-space failure.

### 9.2 Chunk Writes

- The verified chunk is written at exactly `chunkIndex * 1 MiB`.
- The write must stay inside the reserved length.
- `RandomAccessFile` seeks to the offset, writes the chunk, and calls
  `FileDescriptor.sync()`.
- Only after the fsync does the durable record advance
  `nextChunkIndex`/`receivedBytes` and emit progress.
- A stale or unverified tail can be truncated with fsync during recovery.

### 9.3 Verification

- `hashStage` streams 64 KiB reads and may cancel between buffers.
- `verifyStage` checks exact length and compares SHA-256 in constant time.
- A mismatch before completion removes staging and reports
  `download_integrity_failed`; nothing is marked complete or published.
- A mismatch after the desktop reports completion is also refused.

### 9.4 Path Safety

- `stageByName` accepts only the regex-validated name.
- It requires a regular file, rejects symlinks, and compares the canonical
  parent to the canonical staging directory.
- `cleanupOrphans` deletes only unreferenced files matching the stage pattern.
- Deleting by name cannot touch another file.

### 9.5 Adversarial Coverage

`DownloadStagerTest` (12 tests) covers random names, exact reservation,
insufficient space, empty files, byte-exact append/verify, geometry overflow,
tail truncation, directory escape/symlink rejection, orphan retention,
name-scoped deletion, cancellation during hash, symlinked staging directory,
and parent-traversal names.

---

## 10. SAF Publication & Destination Verification

### 10.1 Destination Selection

`CreateDownloadDocumentContract` creates:

```kotlin
Intent(Intent.ACTION_CREATE_DOCUMENT)
    .addCategory(Intent.CATEGORY_OPENABLE)
    .setType(suggestedMime(fileType, filename))
    .putExtra(Intent.EXTRA_TITLE, suggestedFilename)
```

The returned `content://` URI is passed immediately to
`DownloadCoordinator.publish(uri)`. It is not written to the durable record, not
logged, and not retained after the operation. There is no filesystem path
derivation and no broad storage permission.

### 10.2 Publication Order

1. Require `completeAcknowledged`, a ready state, and exact chunk geometry.
2. Resolve the stage by durable name.
3. Re-verify the staged file's length and whole-file SHA-256.
4. Persist state `publishing`.
5. Resolve the SAF destination.
6. Copy with a 64 KiB buffer while hashing; reject growth beyond expected size.
7. `flushAndSync()`, then close.
8. Check the written digest and byte count.
9. Reopen the destination when the provider supports reading and re-hash it.
10. Persist `completed`; emit `COMPLETED`; delete staging; clear the record.

### 10.3 Truthful Verification Result

`PublicationResult.destinationVerified` is:

- `true` when the destination could be reopened and matched;
- `false` when the provider rejected the verification read after a successful
  write and flush;
- never fabricated as `true` when no read was possible.

The UI distinguishes these cases. A completed publication with
`destinationVerified = false` is explicitly labeled as unverified by the
provider, not as a verified hash match.

### 10.4 Failure and Cancellation

- A staging length mismatch is rejected before the destination is touched.
- A write error closes the sink, deletes the destination, and reports failure.
- A digest mismatch deletes the destination.
- A cancellation during publication deletes the destination where the provider
  allows and retains/cleans staging according to whether the desktop already
  acknowledged completion.
- A failed or interrupted publication returns to `READY_TO_PUBLISH` with the
  verified staging copy intact so the user can choose a destination again.
- Publication is never retried automatically and never overwrites implicitly.

### 10.5 Provider Limits

SAF providers are not assumed to support atomic replace or read-back. The app
does not claim atomicity. If `ACTION_CREATE_DOCUMENT` returns a new document,
the provider controls collision handling; the app never writes to an existing
path it derived itself.

### 10.6 Adversarial Coverage

`DownloadPublisherTest` (10 tests) covers verified publication, unverified
provider reads, destination mismatch deletion, changed staging bytes, staging
length mismatch, write failure cleanup, cancellation cleanup, sink-open
failure, bounded buffer validation, and empty-file publication.
`DownloadCoordinatorTest` adds publication failure retention and interrupted
publication returning to the picker without overwrite.

---

## 11. Recovery, Cancellation & Session Reconciliation

### 11.1 Durable Recovery

On process start, `TransferRuntime.restorePersistedState()`:

1. Loads credentials and the encrypted download record.
2. If an upload record exists, discards stale download staging so only one
   resume is offered.
3. If credentials are absent or the record belongs to another session, deletes
   staging and clears the record.
4. Otherwise cleans orphan stages while retaining the referenced stage and
   projects a truthful snapshot:
   - `preparing`/`claimed`/`downloading`/`verified` → `INTERRUPTED`,
     `resumable = true`;
   - `ready_to_publish` → `READY_TO_PUBLISH`;
   - `publishing` → `READY_TO_PUBLISH` with a message that publication was
     interrupted and nothing is overwritten automatically;
   - `completed`/`cancelled`/`expired`/`failed` → matching terminal state.

Restoration performs no network request, no ticket issuance, and no
publication.

### 11.2 Resume Reconciliation

On explicit `resumeDownload()`:

1. Load the durable record and usable credentials.
2. Reject a record bound to another session or device.
3. Send the existing signed init/status request to reconcile.
4. Use `min(localNextChunkIndex, serverNextChunkIndex)` as the next chunk.
5. Re-request only the missing range.
6. If the desktop already reports `completed`, verify the staged copy locally
   and move to `READY_TO_PUBLISH` without re-downloading.
7. If the desktop reports an unsupported state, fail locally and clean up.

This prevents duplicate committed data. It does **not** claim resume across a
desktop backend restart; the client relies on the signed status the desktop
actually returns.

### 11.3 Cancellation

- `requestCancel()` sets the flag and calls
  `cancelActiveCall()` on the active transport.
- The engine checks cancellation between status, chunks, verification, and
  completion.
- `cancelNow()` is used when no operation coroutine is running.
- Local cancellation is authoritative: staging is deleted, the record is
  cleared, and a best-effort signed cancel is sent when usable credentials
  exist.
- If the desktop already acknowledged completion, no cancel is sent; local
  cancellation removes the staged copy and reports that the selected
  destination was removed where the provider allowed it.
- If the signed cancel is lost, the desktop expires or idle-times out the
  ticket; the client does not retry indefinitely.

### 11.4 Unpair and Session Change

`shutdownForUnpair()` cancels both engines, joins the active job with a bounded
timeout, attempts signed cleanup, then removes any remaining upload spool and
download stage. `reconcileSession()` and `reconcileWithoutSession()` purge
records from other pairings or when no live credentials remain.

### 11.5 Runtime Coverage

`TransferRuntimeTest` now has 17 tests, including 7 download-specific tests:

- restored interrupted download is resumable and keeps its staging copy;
- a record from a different pairing is purged with its stage;
- a verified download awaiting publication restores without starting the
  service;
- publishing a verified download is a local copy and never starts the
  foreground service;
- an upload is refused while a download record awaits action;
- a download is refused while an upload record is durable;
- unpair shutdown removes download state without a live session.

---

## 12. Background Execution, Permissions & Privacy

### 12.1 Background Mechanism

Downloads reuse the existing `TransferService` foreground service with
`foregroundServiceType="connectedDevice"`. The service:

- starts for `startDownload()` and `resumeDownload()` because those perform
  network transfer;
- starts for `cancelDownloadNow()` so the best-effort signed cancel can run;
- does **not** start for `publishDownload()` because publication is a local SAF
  copy, not a connected-device network transfer;
- observes both upload and download snapshots and renders one notification;
- stops when the single active operation completes.

A WorkManager/data-sync job is deliberately not used for the transfer. The
durable record supports a later manual resume if the process is evicted.

### 12.2 Manifest and SDK

| Item | Value |
|---|---|
| `compileSdk` | 35 |
| `minSdk` | 29 |
| `targetSdk` | 35 |
| `versionCode` | 1 |
| `versionName` | 0.1.0 |
| Foreground service type | `connectedDevice` |
| Service exported | `false` |

### 12.3 Permissions

| Permission | Why |
|---|---|
| `INTERNET` | Authenticated TLS transfer. |
| `ACCESS_NETWORK_STATE` | Existing connectivity checks. |
| `CAMERA` | QR handoff scanning. |
| `FOREGROUND_SERVICE` | Existing transfer service. |
| `FOREGROUND_SERVICE_CONNECTED_DEVICE` | `connectedDevice` service type. |
| `CHANGE_NETWORK_STATE` | Existing network handling. |
| `POST_NOTIFICATIONS` | Progress/completion notification on Android 13+. |

No storage permission is declared. SAF grants access per selected document.
No Bluetooth permission or service type is present. Because `targetSdk = 35`,
the newer `ACCESS_LOCAL_NETWORK` permission does not yet apply.

### 12.4 Privacy

- Voice capture is untouched; no microphone path is involved in downloads.
- The app does not display, log, or persist ticket IDs in UI text, nor bearer
  tokens, session keys, MACs, or nonces.
- The QR scanner only forwards the raw string to the strict parser.
- Publication URIs are used once and are not stored; only a display name may be
  persisted for the completed UI.
- Error messages shown to the user are mapped from structured codes and do not
  include signed envelope material.
- No raw file bytes or credentials cross IPC from Android; the desktop already
  holds the source file.

### 12.5 Notification Truthfulness

`DownloadNotificationFactory` maps each phase to explicit text. It never shows
100% while the file is merely staged. `READY_TO_PUBLISH` tells the user to
choose a destination; `COMPLETED` appears only after publication returns.

---

## 13. Android UI & Truthful Progress

### 13.1 Scan and Offer

- The paired screen offers a “Scan download QR from desktop” action when no other transfer
  or download is active.
- The scanner returns raw text to `acceptDownloadHandoff`.
- `DownloadTicketParser.parse(raw, localDeviceId)` enforces device binding.
- Scanning only stores a pending `DownloadOffer`; it sends no request.
- The offer card shows:
  - “Download offered by KIVO”;
  - desktop host address and pinned TLS fingerprint suffix;
  - filename and size (or “Empty file”);
  - an explicit warning that the file will be staged in app-private storage,
    verified against the signed SHA-256, and saved only to a chosen destination;
  - `Download` and `Dismiss`.
- The opaque ticket is never rendered.

### 13.2 Status Card

| Phase | UI behavior |
|---|---|
| `PREPARING` | Indeterminate progress; “Claiming the download ticket…” |
| `DOWNLOADING` | Determinate percent plus bytes verified; progress advances only after signed digest and disk write succeed. |
| `RECONNECTING` | Indeterminate; explains signed progress reconciliation and bounded retry. |
| `VERIFYING` | Full bar; explains whole-file SHA-256 verification before completion. |
| `READY_TO_PUBLISH` | Full bar; “Choose destination” and “Discard”; never says saved. |
| `PUBLISHING` | Indeterminate; “Writing the verified copy…” |
| `COMPLETED` | “Saved as …”; distinguishes verified read-back from provider-unverified. |
| `INTERRUPTED` | “Resume download” and “Discard”; explains signed reconciliation. |
| `CANCELLED` | States staging was removed and no destination file was left behind. |
| `EXPIRED`/`FAILED` | Shows mapped message and structured code; no fake success. |

`DownloadSnapshot.progressPercent` caps unverified progress at 99 and only
returns 100 for verified phases or completion. `COMPLETED` additionally requires
the destination write path to finish.

### 13.3 Mutual Exclusion in UI

The selection UI is hidden while a transfer is active, an interrupted upload
blocks, a download offer is pending, or a download is active/ready. The scan
button is available only in idle or terminal download states. This mirrors the
runtime’s single-operation rule rather than relying on UI state alone.

### 13.4 No Device Screenshot

No emulator or physical device was available, so this report contains no
screenshots and makes no visual-layout claim beyond code inspection and
compilation.

---

## 14. Adversarial Test Matrix & Verification Gates

### 14.1 Android JVM Tests

Command:

```bash
cd android-companion
./gradlew :app:testDebugUnitTest --console=plain
```

Result: **BUILD SUCCESSFUL**, **213 tests, 0 failures, 0 errors, 0 skipped**.

| Suite | Tests | Focus |
|---|---:|---|
| `DownloadTicketParserTest` | 11 | Handoff syntax, duplicates, versions, bounds, device binding, secret absence |
| `DownloadInteropTest` | 7 | Python/Kotlin request bytes, MACs, signed response verification, bounded chunk envelope |
| `DownloadStagerTest` | 12 | Random names, reservation, empty files, geometry, symlinks, orphans, cancellation |
| `DownloadPublisherTest` | 10 | Flush, read-back, mismatch deletion, write failure, cancellation, empty file |
| `DownloadCoordinatorTest` | 18 | Happy path, empty file, tamper, metadata/device/digest mismatch, expiry, ordering, lost ack, resume, publication retry, cancel, cross-device/session resume |
| `TransferRuntimeTest` (download subset) | 7 | Restore, cleanup, single-operation exclusion, no FGS for publication, unpair |
| **Download-related total** | **65** | |

The remaining 148 tests cover pairing, crypto, TLS pinning, keystore, upload
coordinator/source, upload validation, UI models, and the pre-existing runtime
upload paths. All 213 pass together.

### 14.2 Adversarial Categories

| Category | Evidence |
|---|---|
| Ticket/handoff | Parser tests plus shared Python fixture; duplicates, unknown/missing fields, fragments, malformed escapes, oversized payload, wrong device, foreign scheme/version. |
| Auth/replay | Interop MAC vectors, response verifier tests, sequence gate tests in runtime/coordinator, tampered envelope rejection. |
| Chunks | Tampered bytes, out-of-order response, wrong offset/length/index, lost acknowledgement, signed status reconciliation. |
| Storage | Insufficient space, geometry overflow, symlink/path escape, orphan cleanup, encrypted-state validation. |
| Publication | Digest mismatch, changed staging, provider without read-back, write failure, cancellation, interrupted publication, no overwrite. |
| Background | One FGS, no start for local publication, single-operation exclusion, unpair cleanup. |

### 14.3 Desktop Gates

| Gate | Command | Result |
|---|---|---|
| Backend tests | `.venv/bin/python -m pytest tests/ -q` | **1110 passed**, 8 warnings, 176 subtests |
| Shell tests | `.venv/bin/python -m pytest shell/tests/ -q` | **16 passed** |
| QML lint | `qmllint` over `shell/**/*.qml` | **exit 0**; warnings only (see limitations) |
| Desktop health | `./scripts/desktop-health.sh` | **9/9 PASS**, 0 fail, 0 warning |
| Compileall | `.venv/bin/python -m compileall -q kivo shell` | **exit 0** |
| Whitespace | `git diff --check` | **exit 0** |
| Focused download backend | download/handoff/IPC tests | **56 passed** |
| Deterministic fixtures | interop fixture suites | **9 passed** |

### 14.4 Android Build, Lint, and Size

Command:

```bash
cd android-companion
./gradlew :app:testDebugUnitTest :app:lintDebug :app:assembleDebug
./gradlew :app:assembleRelease
```

| Artifact | Size |
|---|---|
| `app-debug.apk` | **26,871,811 bytes** (25.63 MiB / 26.87 MB) |
| `app-release-unsigned.apk` | **21,658,974 bytes** (20.65 MiB / 21.66 MB) |

Lint result: **0 errors, 11 warnings**:

- 7 `GradleDependency` (newer library versions available);
- 2 `UsableSpace` (suggests `StorageManager` allocatable-bytes APIs);
- 1 `OldTargetApi` (target 35 is not the newest API level);
- 1 `CustomX509TrustManager` (existing pinned-TLS implementation).

AGP 8.5.2 also prints a compileSdk-35 compatibility warning because it was
tested up to compileSdk 34. The build still completes successfully.

### 14.5 Device Availability

```text
$ adb devices
List of devices attached
```

No device is attached. Therefore:

- no instrumentation test was run;
- no real `content://` SAF provider was exercised;
- no real foreground-service eviction/restart was exercised;
- no on-device throughput or heap measurement exists;
- no physical-device bidirectional transfer is claimed.

---

## 15. Limitations, Deferred Validation & Conclusion

### 15.1 Limitations

1. **No physical device or emulator.** All Android evidence is JVM unit tests,
   compilation, lint, and APK assembly. `AndroidDownloadDestination` (the real
   `ContentResolver` adapter) is not covered by instrumentation.
2. **No Stage 13.4 desktop QR UI.** The backend handoff builder/parser and
   fixtures are verified, but the desktop user-facing ticket authorization screen
   does not exist yet. Tests use synthetic authenticated handoffs.
3. **No desktop-restart resume.** Android reconciles only against signed
   desktop status. It does not claim that a ticket survives a desktop backend
   restart or that chunks can always be resumed across that boundary.
4. **Provider-dependent destination verification.** Some SAF providers do not
   allow reopening a newly created document. The app reports
   `destinationVerified = false` rather than inventing a hash match.
5. **No measured throughput or heap profile.** Bounds are enforced in code
   (1 MiB chunks, 2 MiB response envelope, 1.5 MiB payload, 64 KiB buffers),
   but no device measurement is available.
6. **Lint/QML warnings.** Android lint has 11 warnings and qmllint emits
   pre-existing warnings (unqualified access, plugin-uncreatable `PanelWindow`,
   `Theme` singleton declaration). Both commands exit successfully; no new QML
   work was part of this stage.
7. **AGP compileSdk warning.** AGP 8.5.2 warns that it was tested only through
   compileSdk 34 while the project targets 35. The build and tests succeed.
8. **No commit/push.** The working tree remains uncommitted as required.

### 15.2 What the Evidence Supports

- The Android client consumes exactly the Stage 13.3.3.3 route and signing
  contract.
- Ticket parsing is strict and path-free, and scanning alone performs no I/O.
- Chunk and whole-file integrity are enforced before desktop completion and
  before publication.
- Staging is app-private, bounded, fsynced, and cleaned on failure/cancel.
- Publication uses SAF only, verifies where possible, and deletes a failed or
  cancelled destination.
- Recovery is durable and encrypted, never automatic, and reconciles against
  signed status.
- One transfer operation runs at a time, with the existing FGS used only for
  network transfer.
- The UI distinguishes verified, ready-to-publish, publishing, completed,
  interrupted, expired, failed, and cancelled states truthfully.

### 15.3 What the Evidence Does Not Support

- “Verified on a real Android phone.”
- “End-to-end bidirectional transfer demonstrated.”
- “Tested against a real SAF document provider.”
- “Measured throughput of X MB/s.”
- “Survives desktop restart.”
- “Desktop QR authorization UI complete.”

### 15.4 Final Verdict

**PASS WITH LIMITATIONS.**

The Stage 13.3.3.4 implementation is complete for the Android client boundary,
passes all executable gates, and preserves the existing KIVO architecture. The
remaining limitations are validation gaps (no device, no real SAF provider, no
Stage 13.4 desktop QR issuance UI) rather than unresolved implementation
defects. A subsequent authorized physical-device run plus instrumentation tests
is required before claiming real-device download completion.
