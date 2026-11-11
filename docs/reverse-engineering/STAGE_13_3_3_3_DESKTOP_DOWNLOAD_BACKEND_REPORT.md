# STAGE 13.3.3.3: Secure Desktop-to-Android Download Backend Report

**Document ID**: `STAGE_13_3_3_3_DESKTOP_DOWNLOAD_BACKEND_REPORT`  
**Date**: November 11, 2026  
**Status**: COMPLETE / VERIFIED (desktop backend only)  
**Final Verdict**: **PASS WITH LIMITATIONS** — the desktop-side authenticated download backend, explicit export authorization, ticket lifecycle, chunk streaming, IPC contracts, fixtures, and regression gates are implemented and verified. The Android download client (Stage 13.3.3.4) and real device-to-device transfer remain out of scope and unverified; bidirectional transfer is **not** claimed.

---

## 1. Executive Summary & Verdict

### 1.1 Objective

Stage 13.3.3.3 establishes the secure desktop half of PC-to-Android file download. The desktop must be able to:

1. Accept an explicit desktop-user authorization for one already-staged File Shelf item.
2. Issue an opaque, short-lived, device-bound download ticket for exactly that file.
3. Stream the authorized file to the paired Android device in authenticated 1 MiB chunks.
4. Reject path substitution, source mutation, ticket theft, replay, cross-device claims, and resource exhaustion.
5. Expose only path-free metadata and bounded controls to local IPC and the LAN client.
6. Document the Android client contract required by Stage 13.3.3.4.

### 1.2 Delivered

- **Explicit export authorization** via `FileShelfService.authorize_export()`. A staged File Shelf item alone is not authorization; the desktop must pass `authorize: true` to `bridge_create_download_ticket`.
- **Retained descriptor security**: the authorized file descriptor is opened with `O_NOFOLLOW`, identity-verified, and retained for the ticket lifetime. Replacing the path after authorization cannot substitute streamed bytes.
- **256-bit download tickets** (`tkt_` + 64 lowercase hex characters), bound to one session, device ID, file identity, size, SHA-256, and logical transfer, with a five-minute absolute expiry.
- **Authenticated chunk streaming** over the existing transfer protocol: `K_up` request MACs, `K_down` signed response envelopes, 16-byte fresh nonces, monotonic sequences, ±60 s timestamps, shared bounded replay window, and canonical path binding of ticket ID and chunk index.
- **Five signed LAN routes** under `/api/v1/transfers/downloads/...`: `init`, status, chunk, `complete`, `cancel`.
- **Source stability enforcement**: full pre-stream SHA-256, 1 MiB `pread` reads at deterministic offsets, per-chunk SHA-256, identity/size/mtime/mode/mount checks before and after every chunk, incremental whole-file SHA-256, and abort on any change.
- **Bounded resource management**: 2 GiB maximum file, 1 MiB chunks, two concurrent downloads, eight active tickets per session, 32-ticket registry, 30 s idle timeout, 300 s terminal retention, bounded control bodies, and cleanup on expiry, revocation, cancellation, server stop, and service close.
- **IPC contracts**: `bridge_create_download_ticket`, `bridge_get_downloads`, `bridge_cancel_download`, plus `bridge_download_requested`, `bridge_download_progress`, and `bridge_download_completed` events.
- **Tests and fixtures**: 61 focused download tests, a deterministic synthetic download fixture, loopback TLS integration tests, and privacy/isolation assertions.
- **Android contract**: complete Stage 13.3.3.4 client contract including SAF `ACTION_CREATE_DOCUMENT`, ticket delivery boundary, retry/resume rules, and finalization requirements.

### 1.3 Not Delivered (Explicitly Out of Scope)

- Android download UI, SAF destination picker implementation, Android download service, and Android-side chunk verifier.
- Any QML / Dynamic Notch / Dashboard download UI.
- Public ticket issuance, LAN ticket listing, directory browsing, or path-based source selection.
- Automatic file approval, automatic opening, or automatic publication.
- A new authentication protocol, new dependencies, a new listener, firewall changes, or a public internet relay.
- Physical Android device testing or real bidirectional transfer claims.

### 1.4 Final Verdict: PASS WITH LIMITATIONS

| Dimension | Result |
| :--- | :--- |
| Desktop download backend implementation | **PASS** |
| Explicit desktop export authorization | **PASS** |
| Ticket security and lifecycle | **PASS** |
| Authenticated chunk streaming | **PASS** |
| Source replacement/mutation resistance | **PASS** |
| IPC contracts and path-free metadata | **PASS** |
| Focused download tests | **61 / 61 PASS** |
| Full backend regression | **1090 passed, 176 subtests passed** |
| Shell regression | **16 / 16 PASS** |
| QML lint | **Exit 0** (pre-existing warnings only) |
| Desktop health | **9 / 9 PASS** |
| Android unit/lint regression | **148 / 148 PASS, 0 lint errors** |
| `compileall` / `git diff --check` | **Clean** |

**Why the limitation exists:** Stage 13.3.3.3 is intentionally the desktop backend slice. There is no Android download client, no emulator, and no physical Android device attached (`adb devices` is empty). The desktop routes were exercised with synthetic files over loopback TLS only. Bidirectional transfer cannot be claimed until Stage 13.3.3.4 and authorized device testing are complete.

---

## 2. Scope, Architecture & Boundary Compliance

### 2.1 Component Map

| Component | File | Responsibility |
| :--- | :--- | :--- |
| Path authorization | `kivo/shelf/path_policy.py` | `AuthorizedFileDescriptor`, retained `O_NOFOLLOW` descriptor traversal, mount-ID capture, symlink/sensitive-path rejection |
| Explicit export grant | `kivo/shelf/service.py` | `ShelfExportGrant`, `FileShelfService.authorize_export()`, identity revalidation, stale marking, descriptor ownership |
| Download validation | `kivo/bridge/download_validation.py` | Limits, `tkt_`/`shelf_` patterns, chunk geometry, authorization flag |
| Ticket manager | `kivo/bridge/download.py` | Ticket lifecycle, claims, chunk `pread` streaming, source stability, cleanup, events |
| LAN routes | `kivo/bridge/server.py` | Signed download routes, request authentication, body binding, signed errors, session revocation |
| Service orchestration | `kivo/bridge/service.py` | `DownloadManager` wiring, desktop ticket creation, listing, cancellation, shutdown |
| Local IPC | `kivo/ipc/protocol.py`, `kivo/ipc/handlers.py` | Operations, aliases, strict parameter validation, handlers |
| App lifecycle | `kivo/app.py` | Shelf injection into the bridge service, download event forwarding |

### 2.2 Data Flow

```
Desktop user
   │  (File Shelf item already staged)
   ▼
IPC bridge_create_download_ticket { item_id, authorize: true }
   │
   ▼
FileShelfService.authorize_export(item_id)
   │  revalidate dev/inode/size/mtime/mode/mount
   │  open retained O_NOFOLLOW descriptor
   ▼
DownloadManager.create_ticket(grant)
   │  hash full file in 1 MiB buffers
   │  issue tkt_<256-bit> bound to session/device/file
   ▼
Desktop UI displays ticket capability (Stage 13.4 delivery surface)
   │  out-of-band ticket ID to Android (no LAN enumeration)
   ▼
Android: POST /downloads/init { ticket_id }        (K_up, signed body)
   │  claim: ISSUED -> CLAIMED (idempotent, bound session/device)
   ▼
Android: GET /downloads/{id}/chunks/{index}        (K_up, path-bound)
   │  pread 1 MiB at index * 1 MiB
   │  per-chunk SHA-256 + K_down signed envelope
   ▼
Android: POST /downloads/{id}/complete             (K_up)
   │  verify incremental SHA-256 == authorized SHA-256
   ▼
COMPLETED; descriptor released; no quarantine reservation consumed
```

### 2.3 Boundary Compliance

| Boundary | Status |
| :--- | :--- |
| No Android download implementation | **Honored** — no Android source changes for downloads; `ACTION_CREATE_DOCUMENT` documented only |
| No QML / Dynamic Notch / Dashboard changes | **Honored** — Stage 13.3.3.3 added no QML |
| No arbitrary browsing or path input | **Honored** — LAN client supplies only an opaque ticket ID; desktop supplies only an opaque shelf item ID |
| No public ticket issuance | **Honored** — ticket creation is available only through the local authenticated Unix-socket IPC |
| No automatic approval or opening | **Honored** — explicit `authorize: true` and explicit desktop action are required |
| No upload redesign | **Honored** — upload state machine, quarantine, and acceptance gates unchanged |
| No new dependencies | **Honored** — standard library plus existing FastAPI/httpx test stack |
| No new physical listener | **Honored** — existing `BridgeServer`; tests bind loopback only |
| No agent tools | **Honored** — no download tools exposed to the agent/tool executor |
| No commits or pushes | **Honored** — working tree changes only |

---

## 3. Threat Model & Security Invariants

### 3.1 Threats Addressed

| Threat | Attack Path | Control |
| :--- | :--- | :--- |
| Unauthorized file download | Client requests a path or guesses an item | No path parameter exists; source is selected only by desktop-authorized shelf item; ticket is unguessable |
| Path traversal | `../`, absolute paths, encoded separators | Shelf IDs and ticket IDs are regex-validated; paths never cross the LAN boundary; descriptor-relative traversal on desktop |
| Symlink substitution | Replace staged file with a symlink before export | `O_NOFOLLOW` traversal, `lstat` checks, identity revalidation, `symlink_not_allowed` |
| Sensitive-file export | Stage `~/.ssh/id_rsa` or credential files | Existing `_is_sensitive_component` policy rejects sensitive names/directories/extensions at staging and revalidation |
| Source replacement after authorization | Rename a malicious file over the staged path | Retained descriptor pins the original inode; replacement cannot change streamed bytes |
| In-place source mutation | Modify bytes while streaming | Size/mtime/mode/mount checked before/after each chunk; per-chunk and full-file SHA-256 abort on mismatch |
| Ticket theft / cross-device claim | Another paired session uses a leaked ticket ID | Ticket bound to `session_id` + `client_id`; cross-device claim returns signed 403 without file metadata |
| Replay | Reuse a nonce or sequence | Shared bounded replay window per session; fresh 16-byte nonce and monotonic sequence required; replay returns signed 409 |
| Timestamp replay | Old captured request | ±60 s freshness tolerance |
| MAC forgery | Tamper with method, path, ticket, chunk index, body | Canonical transcript binds version, session, method, exact path, sequence, nonce, timestamp, length, and body digest; constant-time HMAC comparison |
| Response tampering | Alter chunk bytes or metadata in transit | Every response signed with `K_down`; payload SHA-256 and length bound in the signature |
| Resource exhaustion | Huge files, many tickets, many concurrent claims, oversized bodies | 2 GiB cap, 1 MiB chunks, 2 concurrent downloads, 8 active per session, 32-ticket registry, 8 KiB control body, 30 s receive timeout |
| FD exhaustion | Open many tickets and never stream | Non-terminal ticket cap plus 5-minute absolute expiry; terminal transitions close descriptors immediately |
| Quarantine abuse | Use downloads to consume upload storage | `DownloadManager` has no quarantine dependency and reserves no storage |
| Metadata leakage | Inspect IPC/events/signed errors for paths or identity | Public payloads are path-free and identity-free; signed error metadata is scoped to the authenticated device |
| Shutdown hang / leaked sockets | Close during transfer | Server stop and service close cancel tickets, release descriptors, and join bounded threads |

### 3.2 Security Invariants

1. A File Shelf item is **not** an export authorization. Only `authorize_export()` plus an explicit `authorize: true` IPC action creates a ticket.
2. The Android client never supplies a path, directory, shelf item ID, or filename to select a source.
3. Every ticket is bound to one session, one device ID, one file identity, one size, one SHA-256, and one logical transfer.
4. Ticket IDs are 256-bit random capabilities generated with `secrets.token_hex(32)`.
5. A ticket can only be claimed by its bound session/device; repeat claims by that device are idempotent.
6. Chunks are read from the retained authorized descriptor, never from a re-opened path.
7. The full source is hashed before issuance and again incrementally while streaming; completion requires both digests to agree.
8. Any source identity, size, mtime, mode, mount, short-read, or digest change fails the download.
9. Every request and response is MAC-protected; the bearer token is only a session identifier.
10. Paths, inodes, device numbers, mount IDs, shelf item IDs, bearer tokens, and MAC keys never appear in public download payloads or events.
11. A terminal ticket releases its descriptor immediately and cannot be resumed.
12. Downloads never modify the source file and never consume upload quarantine storage.

---

## 4. Desktop Authorization & File Selection

### 4.1 Staging Is Not Authorization

`FileShelfService.add_items()` stages references for preview and local actions. It does **not** grant remote export. The new `authorize_export()` method is a distinct desktop action:

1. Look up the staged item by opaque `shelf_<12 hex>` ID.
2. Re-run `PathAuthorizer.open_authorized_descriptor()` with `O_NOFOLLOW` traversal.
3. Reject symlinks, non-regular files, blocked system prefixes, sensitive path components, and workspace policy violations.
4. Compare the descriptor's `dev`, `ino`, `size`, `mtime_ns`, `mode`, and `mnt_id` against the identity captured at staging.
5. Mark the item stale and raise `stale_reference` if the file changed.
6. Transfer ownership of the retained read descriptor to a `ShelfExportGrant`.

The grant exposes only item metadata and descriptor ownership. It is closed on every failure path and on every ticket terminal transition.

### 4.2 File Identity

`FileIdentity` now includes the Linux mount ID obtained via `statx(STATX_MNT_ID)`:

```python
@dataclass(frozen=True)
class FileIdentity:
    dev: int
    ino: int
    size: int
    mtime_ns: int
    mode: int
    mnt_id: int | None
```

`mnt_id` is `None` only when the kernel/filesystem does not support `STATX_MNT_ID`; when available, a mount change aborts the download.

### 4.3 IPC Authorization Contract

`bridge_create_download_ticket` accepts exactly:

```json
{
  "item_id": "shelf_0123456789ab",
  "authorize": true
}
```

- `item_id` must match `^shelf_[0-9a-f]{12}$`.
- `authorize` must be the boolean `true`; `false`, `null`, `"true"`, and `1` are rejected with `authorization_required`.
- Unknown fields (including `path`, `filename`, `uri`, `canonical_path`) are rejected by the protocol validator.
- The handler calls `FileShelfService.authorize_export()`; it never accepts a path from IPC.

Ticket creation additionally requires an active paired session. Without one, `create_download_ticket` returns `no_active_session` and closes the grant.

---

## 5. Download Ticket Lifecycle & State Machine

### 5.1 Ticket Model

```python
@dataclass
class DownloadTicket:
    download_id: str          # tkt_<64 hex> = 256 bits
    session_id: str
    device_id: str
    device_name: str
    item_id: str              # desktop-only, never public
    filename: str
    file_type: str
    size: int
    sha256: bytes
    identity: FileIdentity
    state: DownloadState
    created_at: float
    updated_at: float
    expires_at: float         # created_at + 300 s
    claimed_at: float
    last_activity: float
    bytes_sent: int
    next_chunk_index: int
    error_code: str | None
    grant: ShelfExportGrant | None
    chunk_digests: list[bytes]
    stream_hasher: sha256
```

### 5.2 State Machine

```
                 authorize_export + create_ticket
ISSUED ──────────────────────────────────────────────┐
  │ claim (bound session/device, idempotent)         │
  ▼                                                  │
CLAIMED ─── first successful chunk ───► STREAMING    │
  │                                        │         │
  │ complete when all chunks + digest OK   │         │
  └────────────────────────────────────────┴────────► COMPLETED

Any non-terminal state ──► EXPIRED   (absolute 300 s ticket expiry)
Any non-terminal state ──► REVOKED   (session revocation)
Any non-terminal state ──► FAILED    (idle timeout, source change, integrity failure)
Any non-terminal state ──► CANCELLED (client or desktop cancellation)
```

Terminal failure/abort states are `EXPIRED`, `REVOKED`, `FAILED`, and `CANCELLED`. `COMPLETED` is the successful terminal state and is idempotent for the bound device.

### 5.3 Claim Semantics

- Claim is performed under the manager's `threading.RLock`, so two concurrent `init` calls cannot both create a first claim.
- The first claim transitions `ISSUED → CLAIMED` and counts against `MAX_CONCURRENT_DOWNLOADS = 2`.
- A repeated claim by the same session/device returns the same ticket and does not create a second transfer.
- A claim by another session or device returns `403 download_forbidden` and is not told the ticket's filename, size, digest, progress, or state.
- A third concurrent claim returns `429 concurrent_download_limit`.
- Expired tickets transition to `EXPIRED` and return `410 download_expired`.
- Revoked tickets return `403 download_revoked`.

### 5.4 Resume Semantics

Resume is **one logical download**, not a second transfer:

- `init` with the same ticket ID returns the same `download_id`, state, `next_chunk_index`, and `bytes_sent`.
- The Android client queries status or re-inits after a lost response.
- A duplicate chunk request at an index below `next_chunk_index` re-reads the retained descriptor and compares the digest with the recorded digest; it does not advance progress.
- A chunk request above `next_chunk_index` is rejected with `409 chunk_out_of_order`.
- No route exists to change the source file, item, or ticket binding.

### 5.5 Limits and Expiry

| Limit | Value | Behavior |
| :--- | :--- | :--- |
| Ticket entropy | 256 bits | `secrets.token_hex(32)` |
| Ticket TTL | **300 s absolute** | Issued ticket expires whether or not claimed |
| Idle timeout | **30 s** | Active ticket fails if no successful chunk activity |
| Concurrent downloads | **2** | Third claim rejected with 429 |
| Active tickets per session | **8** | New ticket rejected with 429 |
| Ticket registry | **32** | New ticket rejected with `download_registry_full` |
| Terminal retention | **300 s** | Terminal tickets pruned after retention |
| Max file size | **2 GiB** | `2,147,483,648` bytes |
| Chunk size | **1 MiB** | `1,048,576` bytes |

### 5.6 Cleanup and Revocation

- A background cleanup thread runs every 5 s and transitions expired, idle, or session-invalid tickets.
- `DownloadManager.cancel_session()` revokes all non-terminal tickets for a session.
- `PhoneBridgeService.revoke_device()` and `DELETE /api/v1/session` both call session cancellation.
- `BridgeServer.stop()` and `PhoneBridgeService.close()` cancel all active tickets.
- Every terminal transition closes the `ShelfExportGrant` descriptor immediately.
- `_prune_locked()` removes old terminal tickets and closes any residual descriptors.

---

## 6. Authentication, Replay Protection & Response Integrity

### 6.1 Reused Transfer Cryptography

No new authentication protocol was introduced. Downloads use the existing Stage 13.3.3.1 transfer transcript:

- **Requests**: HMAC-SHA256 with the session's `K_up`.
- **Responses**: HMAC-SHA256 with the session's `K_down`.
- **Bearer token**: identifies the session only; it is never a MAC key.
- **Protocol version**: `"1"` (`TRANSFER_PROTOCOL_VERSION`).

### 6.2 Request Headers

| Header | Required | Meaning |
| :--- | :--- | :--- |
| `Authorization: Bearer <token>` | Yes | Session lookup only |
| `X-Kivo-Protocol: 1` | Yes | Transfer protocol version |
| `X-Kivo-Session: <sess_id>` | Yes | Session ID |
| `X-Kivo-Sequence: <int>` | Yes | Monotonic positive sequence, durably reserved |
| `X-Kivo-Nonce: <b64url>` | Yes | Fresh 16-byte nonce, unpadded base64url |
| `X-Kivo-Timestamp: <float>` | Yes | Unix epoch seconds, ±60 s |
| `X-Kivo-Content-SHA256: <hex>` | Yes | SHA-256 of exact body bytes; empty hash for GET/bodyless |
| `X-Kivo-Signature: <hex>` | Yes | HMAC-SHA256 over canonical request with `K_up` |
| `Content-Length` | Init: yes; GET: optional/0 | Exact body length |

### 6.3 Canonical Binding

The request transcript binds all of the following:

1. Protocol version (`"1"`).
2. Session ID.
3. Uppercase HTTP method.
4. Exact canonical request path, including `download_id` and `chunk_index`.
5. Sequence.
6. Fresh nonce.
7. Timestamp.
8. Content length.
9. Content SHA-256.

Because the ticket ID and chunk index are part of the signed path, a client cannot change the requested chunk or ticket without invalidating the MAC. The chunk offset is deterministically derived from the signed chunk index (`index * 1 MiB`).

### 6.4 Replay Window

Downloads reuse `TransferManager`'s bounded replay window per session:

- Sequence window: **128**.
- Nonce set: **256**.
- Request acceptance checks sequence bounds and nonce uniqueness.
- Replay state is committed only after body digest validation and before the download operation.
- Uploads and downloads share the same session replay window. An Android client must therefore use **one durable sequence allocator across all transfer endpoints**, not separate upload/download counters.

Replay failures return a **signed** HTTP 409 with `error_code` of `replay_nonce_detected` or `replay_sequence_invalid`. Authentication/MAC failures return an **unsigned** HTTP 401 with no signed envelope.

### 6.5 Response Envelope

Every authenticated download response is a signed envelope:

```json
{
  "ok": true,
  "v": "1",
  "ctx": "download_chunk",
  "sid": "<session_id>",
  "seq": 17,
  "nonce_b64": "<request nonce, url-safe unpadded>",
  "http_status": 200,
  "state": "streaming",
  "tid": "tkt_<64 hex>",
  "payload_b64": "<standard base64 of canonical JSON>",
  "payload_sha256": "<lowercase hex>",
  "sig": "<lowercase hex HMAC-SHA256 with K_down>"
}
```

The signature binds version, response context, session, HTTP status, sequence, request nonce, state, transfer/download ID, payload length, and payload SHA-256. The Android client must verify the envelope before reading any payload field.

Allowed download response contexts:

- `download_init`
- `download_status`
- `download_chunk`
- `download_complete`
- `download_cancel`

### 6.6 Signed Error Policy

| Failure Class | HTTP | Signed? | Example Codes |
| :--- | :--- | :--- | :--- |
| Missing/invalid bearer, expired session, bad protocol, bad MAC, stale timestamp | 401 | **No** | `missing_authorization`, `invalid_signature`, `stale_timestamp` |
| Malformed request body or body digest mismatch | 400 | **Yes** | `invalid_params`, `body_hash_mismatch` |
| Replay sequence/nonce | 409 | **Yes** | `replay_nonce_detected`, `replay_sequence_invalid` |
| Unknown ticket / malformed ID | 404 | **Yes** | `download_not_found`, `invalid_download_id` |
| Cross-device claim | 403 | **Yes**, metadata-free | `download_forbidden` |
| Ticket expired | 410 | **Yes** | `download_expired` |
| Ticket revoked | 403 | **Yes** | `download_revoked` |
| Invalid state / out-of-order / incomplete | 409 | **Yes** | `invalid_download_state`, `chunk_out_of_order`, `download_incomplete` |
| Idle timeout | 408 | **Yes** | `download_idle_timeout` |
| Source changed / integrity failure | 422 | **Yes** | `download_source_changed`, `download_integrity_failed` |
| Limits exceeded | 429 | **Yes** | `concurrent_download_limit`, `session_download_limit`, `download_registry_full` |
| Downloads unavailable | 503 | **Yes** | `download_unavailable`, `download_source_unavailable` |

Signed error metadata is scoped to the authenticated session/device. A different paired device receives only the error code, message, and ticket ID it supplied, never another ticket's filename, size, digest, progress, or state.

---

## 7. HTTP Route Contract

All routes are under the existing TLS 1.3 bridge server and require the standard transfer authentication headers.

| Method | Path | Body | Success | Response Context |
| :--- | :--- | :--- | :--- | :--- |
| `POST` | `/api/v1/transfers/downloads/init` | `{"ticket_id":"tkt_..."}` (≤ 8 KiB) | 200 | `download_init` |
| `GET` | `/api/v1/transfers/downloads/{download_id}` | none | 200 | `download_status` |
| `GET` | `/api/v1/transfers/downloads/{download_id}/chunks/{chunk_index}` | none | 200 | `download_chunk` |
| `POST` | `/api/v1/transfers/downloads/{download_id}/complete` | none | 200 | `download_complete` |
| `POST` | `/api/v1/transfers/downloads/{download_id}/cancel` | none | 200 | `download_cancel` |

### 7.1 Init

The init body is a compact JSON object:

```json
{"ticket_id":"tkt_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}
```

Rules:

- The exact bytes sent must match `X-Kivo-Content-SHA256`.
- Unknown fields are rejected.
- `ticket_id` must match `^tkt_[0-9a-f]{64}$`.
- The ticket must be bound to the authenticated session and device.
- The operation is idempotent for the bound device.
- The response payload is `public_download(ticket)` with state `claimed`, `streaming`, or `completed`.

### 7.2 Status

Returns path-free public metadata:

```json
{
  "download_id": "tkt_...",
  "filename": "report.pdf",
  "file_type": "pdf",
  "size": 1048576,
  "sha256": "<lowercase hex>",
  "bytes_sent": 1048576,
  "progress_percent": 100,
  "state": "streaming",
  "device_name": "Pixel",
  "chunk_size": 1048576,
  "next_chunk_index": 1,
  "total_chunks": 1,
  "created_at": 1762800000.0,
  "updated_at": 1762800001.0,
  "expires_at": 1762800300.0,
  "error_code": null
}
```

No path, inode, device number, mount ID, shelf item ID, bearer token, or key material is included.

### 7.3 Chunk

The chunk response payload extends the status payload with:

```json
{
  "chunk_index": 0,
  "offset": 0,
  "length": 1048576,
  "chunk_sha256": "<lowercase hex>",
  "chunk_b64": "<standard base64>"
}
```

- Chunks must be requested in order.
- Duplicate requests at an already-committed index are idempotent and digest-checked.
- The last chunk may be shorter than 1 MiB; `length` is exact.
- A zero-length file has `total_chunks == 0` and completes without chunk requests.

### 7.4 Complete

Requires:

- All chunks committed (`next_chunk_index == total_chunks`).
- `bytes_sent == size`.
- Incremental SHA-256 equals the ticket's authorized SHA-256.
- Source descriptor still matches the authorized identity/size/mtime/mode/mount.

On success the ticket transitions to `COMPLETED`, the descriptor is closed, and `complete` is idempotent.

### 7.5 Cancel

Cancels a non-terminal ticket for the bound device. Cancellation is idempotent and releases the descriptor. Any later chunk request receives a signed 409 `invalid_download_state`.

### 7.6 Transport Bounds

- Control body limit: **8 KiB**.
- Body receive timeout: **30 s**.
- Status/complete/cancel/chunk request body limit: **0 bytes** (bodyless requests).
- Server: existing TLS 1.3 bridge; no new listener or port.

---

## 8. Chunk Streaming, Source Stability & Resource Bounds

### 8.1 Descriptor-Backed Reads

`DownloadManager` streams from the retained `ShelfExportGrant` descriptor using `os.pread(fd, length, offset)`:

- Never re-opens the path after authorization.
- Never uses the file's current offset.
- Reads exactly the declared length or fails on short read.
- Reads at most 1 MiB per chunk.
- Holds no whole-file buffer.

### 8.2 Pre-Stream Hashing

`create_ticket()` computes the full-file SHA-256 before issuing the ticket:

1. `fstat` the retained descriptor.
2. Compare `dev`, `ino`, `size`, `mtime_ns`, `mode`, and `mnt_id` with the authorized identity.
3. Verify regular-file mode and size ≤ 2 GiB.
4. Read 1 MiB at a time with `pread` and update SHA-256.
5. Re-check the descriptor signature after hashing.
6. Abort with `download_source_changed` if anything changed.

This pre-hash is performed synchronously in the local IPC handler thread. It is bounded in memory but can take time for large files; see Section 12.

### 8.3 Streaming-Time Integrity

For every chunk:

1. Check source signature before reading.
2. `pread` the exact expected length.
3. Check source signature after reading.
4. Compute the chunk SHA-256.
5. If the index is new, append the digest, update the incremental whole-file hasher, advance `next_chunk_index`, and add `bytes_sent`.
6. If the index is a duplicate, compare the new digest with the previously recorded digest; do not advance progress.
7. On any mismatch, transition to `FAILED` and close the descriptor.

At `complete`:

- Compare `stream_hasher.digest()` with the ticket's pre-authorized SHA-256 using `hmac.compare_digest`.
- Transition to `FAILED` with `download_integrity_failed` on mismatch.

### 8.4 Source Substitution and Mutation Cases

| Case | Behavior |
| :--- | :--- |
| Path replaced by `rename`/`os.replace` after authorization | Retained descriptor still streams the original inode; replacement bytes are never sent |
| Path replaced by symlink | Export revalidation rejects `stale_reference`; if replaced after issuance, descriptor still pins original |
| In-place content modification | mtime/size signature changes or digest mismatch aborts with `download_source_changed` |
| File truncated or extended | Size mismatch aborts |
| Mount changed / remounted | `mnt_id` mismatch aborts |
| File deleted | Descriptor remains readable on Unix; identity/size remain; streaming can complete from the pinned inode; path is irrelevant |
| Hardlink modification | Detected by mtime/size/digest checks |
| Short read | Aborts with `download_source_changed` |
| Source is a directory/device/socket | Rejected at authorization and export |

The source file is never opened for write, never truncated, never renamed, and never modified by the download path.

### 8.5 Ordering and Retry

- First expected index is 0.
- `index > next_chunk_index` → 409 `chunk_out_of_order`.
- `index < next_chunk_index` → idempotent re-read and digest check.
- `index == next_chunk_index` → commit and advance.
- A lost response can be recovered by re-requesting the same index with a fresh sequence/nonce; the server re-reads and verifies the digest, then returns the same bytes without double-counting.
- A lost `init` response can be recovered by re-calling `init` with the same ticket ID; claim is idempotent.

### 8.6 Resource Bounds

| Resource | Bound |
| :--- | :--- |
| Per-chunk memory | 1 MiB read buffer + output bytes |
| Full-file memory | Not buffered; only SHA-256 state and per-chunk digests |
| Per-ticket chunk digests | 2048 × 32 bytes at 2 GiB |
| Retained descriptors | Non-terminal tickets only; max 8 per session; immediate close on terminal |
| Concurrent downloads | 2 |
| Registry | 32 tickets |
| Control bodies | 8 KiB |
| Terminal retention | 300 s |
| Cleanup thread join | 2 s |
| Source modification | None |

### 8.7 Quarantine Isolation

`DownloadManager` is constructed without `QuarantineManager`. It does not reserve space, create `.part` files, write into the quarantine roots, or affect upload capacity. Downloads and uploads share only session auth/replay state.

---

## 9. IPC Contracts, Events & Privacy

### 9.1 Operations

| Operation | Aliases | Parameters | Result |
| :--- | :--- | :--- | :--- |
| `bridge_create_download_ticket` | `bridge.create_download_ticket` | `item_id`, `authorize: true` | `{ok, download}` or structured error |
| `bridge_get_downloads` | `bridge.get_downloads` | none | `{ok, downloads: [...]}` |
| `bridge_cancel_download` | `bridge.cancel_download` | `download_id` | `{ok, download}` or structured error |

Validation:

- `item_id` must match `^shelf_[0-9a-f]{12}$`.
- `authorize` must be exactly `true`.
- `download_id` must match `^tkt_[0-9a-f]{64}$`.
- Unknown parameters are rejected with `invalid_params`.
- `bridge_get_downloads` accepts no parameters.
- Aliases are allowlisted in `REQUEST_OPERATIONS` and normalized by replacing `.` with `_`.

### 9.2 Events

| Event | Aliases | When | Payload |
| :--- | :--- | :--- | :--- |
| `bridge_download_requested` | `bridge.download_requested` | Ticket issued | Path-free public ticket |
| `bridge_download_progress` | `bridge.download_progress` | Claim, each chunk, state change | Path-free public ticket |
| `bridge_download_completed` | `bridge.download_completed` | Any terminal transition | Path-free public ticket with terminal state |

Event payloads are produced by `public_download()` and contain only:

- `download_id`, `filename`, `file_type`, `size`, `sha256`
- `bytes_sent`, `progress_percent`, `state`, `device_name`
- `chunk_size`, `next_chunk_index`, `total_chunks`
- `created_at`, `updated_at`, `expires_at`, `error_code`

They never contain paths, canonical paths, inodes, device numbers, mount IDs, shelf item IDs, bearer tokens, or MAC keys.

### 9.3 Handler Behavior

`IPCHandlers`:

- Returns `service_unavailable` when the bridge is not initialized.
- Delegates explicit authorization to `PhoneBridgeService.create_download_ticket`.
- Returns structured `{ok: false, error_code, error_message}` on validation or download errors.
- Never returns a traceback or raw exception text from internal failures.

### 9.4 App Wiring

- `kivo/app.py` initializes the File Shelf before the bridge.
- The shelf service is injected into `PhoneBridgeService`.
- Download events are forwarded to the existing IPC event bus alongside upload events.
- No download operation is registered as an agent tool or exposed to the AI model/tool executor.
- No QML consumer was added in this stage.

---

## 10. Android Download Client Contract (Stage 13.3.3.4)

> **Status:** This section specifies the required Android behavior. **No Android download client is implemented in Stage 13.3.3.3.**

### 10.1 Ticket Delivery Boundary

There is **no LAN endpoint that lists tickets** and no directory browsing. The Android client must receive the opaque `tkt_...` capability out-of-band from the desktop user:

- The desktop UI (Stage 13.4) may render the ticket as a QR code or short-lived deep link, for example `kivo://download?ticket=tkt_...`.
- The Android client may scan the QR or accept manual entry.
- The Android client must validate `^tkt_[0-9a-f]{64}$` before use.
- The ticket ID must be treated as a secret capability: do not log it, put it in notifications, or persist it longer than necessary.
- Ticket delivery UI and QR rendering are **not** part of Stage 13.3.3.3. Stage 13.3.3.4 must implement scanning/entry and may propose a separate reviewed delivery endpoint if QR/manual entry is rejected.

### 10.2 Required Request Authentication

The Android client must reuse the existing `K_up`/`K_down` transfer implementation:

- Bearer token from the paired session (identification only).
- Fresh 16-byte nonce per request, unpadded base64url.
- One durable, monotonically increasing sequence allocator **shared with uploads and downloads**; persist before send.
- Current Unix timestamp within ±60 s; require device clock synchronization.
- `X-Kivo-Content-SHA256` of the exact body bytes; empty-body hash for GET.
- HMAC-SHA256 over the canonical transcript with `K_up`.
- Exact request path in the signature, including ticket ID and chunk index.
- No query parameters, URL rewriting, redirects, or proxies.

The Android client must verify every response envelope with `K_down` **before** parsing payload fields:

1. `v == "1"`.
2. `sid` matches the session.
3. `seq` and `nonce_b64` match the request.
4. `ctx` matches the expected operation.
5. `http_status` matches the actual HTTP status.
6. `tid` matches the expected ticket ID (where applicable).
7. `payload_sha256` matches the decoded `payload_b64` bytes.
8. `sig` verifies with `K_down`.

Any failure aborts the download without using the payload.

### 10.3 Download Flow

1. **Receive ticket** from desktop QR/manual entry; validate pattern.
2. **Claim**: `POST /api/v1/transfers/downloads/init` with `{"ticket_id":"tkt_..."}`.
3. **Verify** signed `download_init` response; require state `claimed`, `streaming`, or `completed`, and record `filename`, `size`, `sha256`, `chunk_size`, `total_chunks`, and `expires_at`.
4. **Choose destination** with SAF `ACTION_CREATE_DOCUMENT` (Section 10.4). If the user cancels, call `cancel` and stop.
5. **Stream chunks** sequentially from `next_chunk_index`:
   - `GET /api/v1/transfers/downloads/{id}/chunks/{index}`.
   - Verify envelope, `download_id`, `chunk_index`, `offset == index * 1048576`, `length` (1 MiB except final), `chunk_sha256`, and `chunk_b64` decoding.
   - Write to an app-private temporary file at the declared offset.
   - Update progress only after chunk verification.
6. **Complete**: `POST /api/v1/transfers/downloads/{id}/complete`.
7. Verify signed `download_complete` response; require state `completed` and `bytes_sent == size`.
8. **Finalize**: copy the verified temp file to the SAF document URI, flush/fsync, close, and delete the temp file.
9. **Cancel/cleanup** on user cancellation, terminal error, session revocation, app shutdown, or timeout: call signed `cancel` if non-terminal, delete temp data, and delete any partial SAF document.

### 10.4 SAF Destination Selection

The Android client must use the Storage Access Framework, not broad filesystem permissions:

- Intent action: **`Intent.ACTION_CREATE_DOCUMENT`**.
- Category: `Intent.CATEGORY_OPENABLE`.
- Type: derive a MIME type from the verified filename/`file_type` using Android's `MimeTypeMap`; fall back to `application/octet-stream` when unknown.
- `Intent.EXTRA_TITLE`: sanitized ticket filename (no path separators, control characters, bidi overrides, or traversal; max 128 characters).
- Optional `Intent.EXTRA_MIME_TYPES` for provider filtering.
- Launch via `ActivityResultContracts.CreateDocument` or an equivalent Activity Result contract.
- No `MANAGE_EXTERNAL_STORAGE`, legacy `WRITE_EXTERNAL_STORAGE`, or hardcoded `/storage/emulated/0` paths.
- Use `ContentResolver.openOutputStream(uri, "wt")` or `openFileDescriptor(uri, "rw")` only after the full download is verified.
- Handle `OperationCanceledException`, provider `SecurityException`, and `IOException` without leaving partial user-visible files.
- If the provider cannot create the document, abort before streaming or clean up the temp file.

**Recommended finalization strategy:** stream to an app-private temp file (`context.cacheDir` or `context.filesDir`) with a free-space check (`size + 64 MiB` margin), verify the full SHA-256, call `complete`, then copy to the SAF URI. This avoids exposing partial content to other apps and supports integrity verification before the user-visible document exists. If direct SAF streaming is used instead, the client must delete or truncate the document on every failure path.

### 10.5 Retry and Resume Rules

- Never create a second ticket for a retry. Re-call `init` with the same ticket ID; claim is idempotent.
- After a network failure, query status or re-init with fresh sequence/nonce and resume from `next_chunk_index`.
- A chunk whose response was lost may be re-requested at the same index; the desktop re-reads and digest-checks it without advancing progress.
- On `409 chunk_out_of_order`, query status and reconcile `next_chunk_index`; do not skip ahead.
- On `409 replay_nonce_detected` or `replay_sequence_invalid`, generate a fresh nonce and advance the durable sequence; do not reuse the failed request.
- Retries must be bounded with exponential backoff; do not hot-loop.

### 10.6 Error Handling

| Response | Android behavior |
| :--- | :--- |
| Unsigned 401 | Treat as authentication/session failure; do not parse as signed; re-pair or stop |
| Signed 400 `body_hash_mismatch` | Client bug: signed body differs from sent body; abort and report |
| Signed 403 `download_forbidden` | Ticket belongs to another device; clear ticket and stop |
| Signed 403 `download_revoked` | Delete temp/partial; stop |
| Signed 404 `download_not_found` | Invalid/expired delivery; clear ticket and stop |
| Signed 408 `download_idle_timeout` | Terminal; delete temp; request a new ticket from desktop if needed |
| Signed 409 `chunk_out_of_order` | Query status and reconcile; resume from authoritative index |
| Signed 409 `download_incomplete` | Continue missing chunks; do not finalize |
| Signed 410 `download_expired` | Terminal; delete temp; request a new ticket |
| Signed 422 `download_source_changed` | Terminal; delete temp; inform user that the desktop file changed |
| Signed 422 `download_integrity_failed` | Terminal; delete temp; report integrity failure |
| Signed 429 limit | Back off; cancel another download or wait; do not spin |
| Signed 503 `download_unavailable` | Retry with backoff; if persistent, report desktop unavailable |

### 10.7 Android Privacy and Lifecycle

- No filesystem path, provider URI, or directory listing is ever sent to the desktop.
- No raw file bytes, ticket IDs, bearer tokens, or MAC keys in logs, notifications, analytics, or crash reports.
- Use the existing pinned TLS 1.3 client; do not disable hostname/IP verification.
- Disable redirects; never forward `Authorization` to another host.
- Run the transfer in a user-visible foreground service or WorkManager job appropriate for the Android version; stop cleanly on user cancellation, unpairing, or session revocation.
- On app shutdown, delete temp files and cancel any non-terminal ticket.
- Do not open/execute the downloaded file automatically. The user decides what to do with the SAF document.
- Do not persist ticket IDs in plaintext beyond the active transfer; clear them on completion, cancellation, or terminal failure.

### 10.8 Bidirectional Status

Stage 13.3.3.3 provides the desktop receiver. Until Stage 13.3.3.4 implements the Android client and an authorized device completes a real transfer, **KIVO does not claim verified PC-to-Android download capability**.

---

## 11. Test Matrix & Verification Evidence

### 11.1 Focused Download Suites

| Suite | File(s) | Tests | Result |
| :--- | :--- | ---: | :--- |
| Ticket/security/source/transfer unit tests | `tests/test_bridge_download.py` | 32 | **PASS** |
| Loopback TLS integration & adversarial tests | `tests/test_bridge_download_server.py` | 11 | **PASS** |
| IPC + service tests | `tests/test_bridge_download_ipc.py`, `tests/test_bridge_download_service.py` | 15 | **PASS** |
| Deterministic fixture integrity | `tests/test_download_fixtures.py` | 3 | **PASS** |
| **Focused total** | | **61** | **PASS** |

### 11.2 Regression Suites

| Suite | Files | Tests | Result |
| :--- | :--- | ---: | :--- |
| File Shelf policy/service/IPC | `tests/test_shelf_policy.py`, `tests/test_shelf_service.py`, `tests/test_shelf_ipc.py` | 45 | **PASS** |
| Upload transfer/crypto/quarantine regression | `tests/test_bridge_transfer.py`, `tests/test_bridge_transfer_server.py`, `tests/test_bridge_quarantine.py`, `tests/test_transfer_crypto.py` | 76 | **PASS** |
| Pairing/response integrity/bridge IPC/interop | `tests/test_bridge_pairing.py`, `tests/test_bridge_crypto.py`, `tests/test_bridge_ipc.py`, `tests/test_android_upload_interop.py` | 61 | **PASS** |
| Mutation authorization regression | `tests/test_file_mutations.py` | 117 tests + 69 subtests | **PASS** |
| Full backend suite | `tests/` | **1090** + 176 subtests | **PASS** |
| Shell suite | `shell/tests/` | 16 | **PASS** |

### 11.3 Mandatory Gates

| Gate | Command | Result |
| :--- | :--- | :--- |
| Backend tests | `.venv/bin/python -m pytest tests/ -q` | **1090 passed, 176 subtests passed, 7 warnings** |
| Shell tests | `.venv/bin/python -m pytest shell/tests/ -q` | **16 passed** |
| QML lint | `/usr/lib/qt6/bin/qmllint $(find shell -name '*.qml')` | **Exit 0**; pre-existing warnings only |
| Desktop health | `./scripts/desktop-health.sh` | **9 / 9 PASS** |
| Whitespace | `git diff --check` | **Clean** |
| Python syntax | `.venv/bin/python -m compileall -q kivo tests` | **Clean** |
| Android unit tests | `./gradlew testDebugUnitTest` | **148 / 148 PASS**, 0 failures/errors/skips |
| Android lint | `./gradlew lintDebug` | **0 errors**, 9 warnings |

### 11.4 Attack-Case Coverage

The focused suites exercise:

- 256-bit ticket format, uniqueness, and non-equality with shelf IDs.
- Ticket binding to session, device, file size, and SHA-256.
- Staging without authorization; missing/false/string/numeric `authorize`.
- Unknown item IDs, malformed IDs, and path-like IDs.
- Cross-session and cross-device claims; metadata-free signed 403.
- Idempotent claims and duplicate chunks.
- Ticket expiry and descriptor release.
- Session revocation and `DELETE /api/v1/session` revocation.
- Concurrent claim limit (third claim rejected).
- Per-session ticket limit and registry bound.
- Zero-length files.
- Out-of-order, invalid, and repeated chunk indices.
- Incomplete completion, cancellation, and post-cancel chunk rejection.
- In-place modification, path replacement, symlink replacement, and mount-ID changes.
- Sensitive paths and symlink staging rejection.
- Source immutability (mtime, size, mode, and digest unchanged).
- Files larger than 2 GiB.
- Public payload, event, fixture, and signed-error path-free assertions.
- Loopback TLS full flow, replay 409, tampered/missing auth 401, stale timestamp, body-digest binding, path-bound chunk index, signed 409/400 errors, and session revocation.

### 11.5 Fixtures

- `tests/fixtures/download_test_vectors.json` is a deterministic synthetic fixture:
  - 3 MiB + 17 bytes, `byte[i] = i % 251`.
  - 1 MiB chunk geometry and per-chunk SHA-256.
  - Opaque ticket/shelf/session IDs, route templates, states, and terminal states.
  - No user paths, credentials, keys, or real file contents.
- `tests/fixtures/tls_test_certs.json` was regenerated during this stage because the prior one-day `valid` certificate had expired. The new fixture uses test-only validity windows (2000–2100 `valid`, 2000–2001 `expired`, 2090–2100 `future`). This is test data maintenance; no application or Android source code was changed.

### 11.6 Environment Notes

- All tests used synthetic files in temporary directories.
- LAN integration tests bound **loopback only**; no physical LAN listener, firewall change, or real phone pairing was performed.
- `adb devices` lists **no devices**; no emulator or physical Android device was available.

---

## 12. Limitations, Residual Risks & Operational Notes

1. **Android client not implemented.** Stage 13.3.3.3 is desktop-only. The routes and contract are ready, but no Android download UI, SAF destination picker, chunk verifier, or foreground service exists yet.
2. **No real device verification.** Loopback TLS tests validate protocol behavior, not Wi-Fi radio behavior, Android SAF provider quirks, Doze/background limits, or real device clock skew.
3. **Ticket delivery UI is pending.** There is no LAN ticket-list endpoint by design. Stage 13.3.3.4 and the desktop UI must define QR/manual ticket delivery. Until then, an Android client cannot discover a ticket automatically.
4. **Absolute five-minute ticket lifetime.** The ticket expires 300 s after creation even during active streaming. A 2 GiB transfer therefore requires an average of at least ~57.3 Mbps (about 6.8 MiB/s) end-to-end. Slower links will hit `download_expired` and require a new desktop authorization. This is a deliberate security bound from the stage requirements; if longer transfers are required, a future stage must introduce an explicit reviewed extension policy rather than silently extending tickets.
5. **Synchronous pre-stream hashing.** Ticket creation hashes the full file before returning. A 2 GiB file can take seconds to tens of seconds depending on storage. The IPC handler runs in a per-client thread, so the backend event loop is not blocked, but the requesting shell client waits. The Stage 13.4 UI should show a bounded “authorizing/hashing” state.
6. **Ticket IDs are bearer capabilities.** They are unguessable (256 bits) and device-bound, but anyone who obtains the ID **and** the bound session credentials can claim it. Desktop UI and Android logs must treat ticket IDs as secrets.
7. **SHA-256 assumption.** Integrity relies on SHA-256 collision and preimage resistance, consistent with the rest of KIVO.
8. **Single active session architecture.** Tickets are bound to the current active paired session. Revoking or replacing the session revokes outstanding tickets; multi-device concurrency is not supported.
9. **No bandwidth throttling.** A paired device can consume LAN bandwidth within the 2-concurrent-download and 1 MiB chunk bounds. There is no per-device rate limit.
10. **Base64 response overhead.** Chunk data is transported inside the signed JSON envelope using standard base64, adding roughly 33% wire overhead. This is acceptable for correctness and integrity but is not the most bandwidth-efficient framing.
11. **Desktop cannot verify client-side finalization.** The desktop verifies the bytes it streamed and signs `complete`. It cannot prove the Android client wrote them correctly to SAF; the Android client must verify each chunk and the final digest before publishing the document.
12. **Shared upload/download replay window.** Android must use one durable sequence allocator for both uploads and downloads. Separate counters will produce `replay_sequence_invalid` or sequence-window failures.
13. **No at-rest encryption of the source.** The source remains the user's existing file on disk; downloads do not create a second persistent copy or change its permissions.
14. **No automatic opening or execution.** The SAF document is user-selected; KIVO does not open or execute it.
15. **SAF finalization occurs after completion.** The desktop marks the ticket `COMPLETED` and releases the descriptor before the Android client copies the temp file into the user-selected SAF document. A SAF copy failure cannot be retried against the same ticket; Stage 13.3.3.4 should surface this and request a fresh ticket. Alternatively, it may copy to SAF before calling `complete` and delete the document if `complete` fails, at the cost of a temporary user-visible file.

None of these limitations invalidate the desktop backend’s security properties. Items 1–3, 11, and 15 are the gating limitations for claiming end-to-end bidirectional transfer.

---

## 13. Requirements Traceability, Conclusion & Next Steps

### 13.1 Requirements Traceability

| Requirement | Implementation | Verification |
| :--- | :--- | :--- |
| Explicit desktop authorization per file | `authorize_export()`, `authorize: true` IPC | Service/IPC tests, stale-file tests |
| No remote paths or browsing | Opaque ticket only; no path params; no list route | Protocol/path-free assertions, loopback tests |
| Reuse `K_up`/`K_down` | Existing transfer transcript | Crypto/transfer regression, loopback TLS |
| Bind version/session/method/path/sequence/nonce/time/ticket/chunk/payload | Canonical request/response transcripts | Byte-level fixture, wrong-route/tampered tests |
| 256-bit ticket, 5-minute expiry | `secrets.token_hex(32)`, `expires_at` | Ticket format/expiry tests |
| Bind session/device/file/size/SHA-256 | Ticket fields + `_get_bound_locked` | Cross-device and binding tests |
| ISSUED→CLAIMED→STREAMING→COMPLETED; terminal states | `DownloadState` + transitions | State/transfer tests |
| Atomic claim, idempotent resume | RLock claim, duplicate chunk digest check | Claim/duplicate/out-of-order tests |
| No cross-device claim | `session_id` + `client_id` compare | Cross-device test |
| Preserve replay/timestamp/constant-time checks | Shared replay window + `hmac.compare_digest` | Replay/timestamp tests |
| Revoke tickets on session revocation | `cancel_session`, `end_session`, `revoke_device` | Server/service revocation tests |
| Routes under `/api/v1/transfers/downloads/...` | 5 signed routes | Loopback full-flow test |
| 1 MiB chunks, bounded memory, `pread` | `DOWNLOAD_CHUNK_BYTES`, `_pread_exact` | Geometry/source tests |
| Per-chunk SHA-256 + authenticated metadata | `chunk_sha256`, signed envelope | Chunk digest tests |
| Strict index/offset validation | `validate_chunk_index`, `chunk_length`, `offset` | Invalid/out-of-order tests |
| Cancellation, idle/connect timeouts, revocation | `cancel`, 30 s idle, 30 s receive | Cancel/idle/revocation tests |
| Source identity/hash before/after; abort on change | `_fd_signature`, pre-hash, incremental hash | Mutation/replacement/mount tests |
| Never buffer whole file; never modify source | Streamed hashing/reads; O_RDONLY | Source immutability/large-file tests |
| 2 GiB / 2 concurrent / bounded registries | Validation constants and manager caps | Limit/registry tests |
| Cleanup on expiry/revocation/shutdown | Cleanup thread + stop/close | Expiry/revocation/shutdown tests |
| No unbounded FD retention | Grant close on terminal; caps | Descriptor-release assertions |
| Downloads do not consume quarantine | No quarantine dependency | Service/unit tests |
| IPC operations/events; no paths/secrets | Protocol/handlers/events | IPC/service/privacy tests |
| Agent tools disabled; no QML | No tool registration; no QML changes | Boundary review |
| Android contract incl. `ACTION_CREATE_DOCUMENT` | Section 10 | Documentation review |
| Focused/regression/full gates | See Section 11 | All pass |
| No commits/pushes | Working tree only | Git status |

### 13.2 Conclusion

Stage 13.3.3.3 completes the desktop download backend with the required explicit authorization, opaque ticket binding, authenticated chunk streaming, source-stability enforcement, bounded resource use, and IPC privacy controls. The implementation reuses the existing transfer cryptography and bridge server rather than introducing a parallel protocol, and it preserves the completed upload stage.

The desktop backend is **PASS WITH LIMITATIONS**:

- **PASS** for all desktop implementation, security, IPC, fixture, and regression gates.
- **LIMITED** because the Android download client, SAF destination picker, ticket delivery UI, and real device testing belong to Stage 13.3.3.4 and the subsequent desktop UI stage. Bidirectional transfer is **not** claimed yet.

### 13.3 Next Steps

1. Implement Stage 13.3.3.4 Android download client:
   - Ticket QR/manual delivery and validation.
   - `ACTION_CREATE_DOCUMENT` SAF destination selection.
   - Envelope-verified init/status/chunk/complete/cancel.
   - App-private temp staging, full digest verification, and final SAF copy.
   - Foreground service/WorkManager lifecycle and cancellation.
2. Add the Stage 13.4 desktop authorization/download UI surface (QML) that invokes `bridge_create_download_ticket` with `authorize: true`, displays ticket delivery, and shows progress/cancel.
3. Execute authorized physical-device testing on the niri/Arch host with a real Android device:
   - Verify pinned TLS, clock skew, Wi-Fi interruption/resume, SAF providers, background limits, and 1 MiB chunk throughput.
   - Verify revocation, expiry, cancellation, and source-mutation behavior on hardware.
4. Only after those steps, update the project status to claim verified bidirectional KIVO transfers.

---

**Report prepared**: November 11, 2026  
**Implementation state**: uncommitted working tree; no commits or pushes were made.  
**Working tree verification**: `git diff --check` clean, `compileall` clean, full backend and shell suites green.
