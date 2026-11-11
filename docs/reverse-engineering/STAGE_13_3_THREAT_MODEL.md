# KIVO — Stage 13.3.0 Threat Model: Phone-to-PC Bridge

```
Document:   Stage 13.3.0 Threat Model: Phone-to-PC Bridge
Status:     Approved Adversarial Threat Model
Date:       2026-11-09
Baseline:   HEAD 1062c0d; Stage 12.2 and Stages 13.1–13.2 completed
Focus:      Network Attacks, Cryptographic Boundaries, Quarantine Isolation, File Ingress/Egress
```

---

## 1. Trust Boundaries & Adversary Capabilities

```
┌────────────────────────────────────────────────────────────────────────┐
│ Public / Shared Local Area Network (Wi-Fi 192.168.1.0/24)              │
│  Adversary Capabilities:                                               │
│  • Passive packet sniffing on unencrypted LAN traffic                  │
│  • Active ARP spoofing / cache poisoning / default gateway impersonation│
│  • Rogue Wi-Fi Access Point / Evil Twin / DNS hijacking                │
│  • Attempting arbitrary HTTP/TCP connections to desktop IP             │
└───────────────────────────────┬────────────────────────────────────────┘
                                │ TLS 1.3 (Certificate Fingerprint Pinned)
┌───────────────────────────────▼────────────────────────────────────────┐
│ Phone Client (Native Companion App)                                    │
│  Trust: Untrusted until QR scan + SAS match + Desktop Approval Gate    │
└───────────────────────────────┬────────────────────────────────────────┘
                                │ Authenticated REST / WebSocket (LAN Only)
┌───────────────────────────────▼────────────────────────────────────────┐
│ Bridge Server (FastAPI + Uvicorn)                                      │
│  Bound strictly to physical LAN IP (192.168.1.7:8443)                  │
└───────────────────────────────┬────────────────────────────────────────┘
                                │ Isolated Internal Quarantine Store
┌───────────────────────────────▼────────────────────────────────────────┐
│ Quarantine Manager (Tier 1: tmpfs <50MB; Tier 2: Cache Disk <2GB)      │
│  Mode 0600 files; mode 0700 dir; random opaque names; SHA-256 stream   │
└───────────────────────────────┬────────────────────────────────────────┘
                                │ Explicit Desktop User "Accept" Action
┌───────────────────────────────▼────────────────────────────────────────┐
│ Persistent Storage (~/Downloads/)                                      │
│  Collision-safe atomic move; never overwrite                           │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Adversarial Threat Matrix (19 Threats Analyzed)

### T01 — Passive LAN Attacker (Eavesdropping on Wi-Fi)
- **Attack Path**: An attacker on the same unencrypted or WPA-shared Wi-Fi network captures packets using Wireshark / tcpdump.
- **Impact**: Exposure of transferred files, credentials, or metadata.
- **Mitigation**: All communications use TLS 1.3 with cipher suites enforcing Forward Secrecy (`ECDHE` + `AES-256-GCM` or `ChaCha20-Poly1305`). Packet sniffing yields only ciphertext.
- **Remaining Risk**: Metadata analysis (timing, packet sizes) can reveal that a file transfer occurred.
- **Required Test**: Assert plain HTTP connections are refused with immediate socket termination.

### T02 — Malicious Wi-Fi Client (Port Scanning & Probing)
- **Attack Path**: A rogue host scans subnet `192.168.1.0/24` on port `8443` and attempts unauthorized REST queries.
- **Impact**: Service discovery, probe spam.
- **Mitigation**: All endpoints (except `/api/v1/pair/handshake`) require a valid `Bearer <session_token>`. Unauthenticated requests receive `401 Unauthorized` without leaking server state. Rate limits restrict probes.
- **Remaining Risk**: Port 8443 is detectable as an open TCP port on the LAN.
- **Required Test**: Fuzz unauthenticated requests against every endpoint; assert `401 Unauthorized`.

### T03 — Active ARP Cache Poisoning (LAN MITM)
- **Attack Path**: Attacker broadcasts forged ARP replies, routing traffic between phone and PC through attacker's laptop.
- **Impact**: Attacker intercepts or modifies data in flight.
- **Mitigation**: The phone app does not rely on OS CA trust; it pins the exact SHA-256 fingerprint of KIVO's ephemeral certificate embedded in the QR code. The TLS handshake fails immediately if the certificate presented does not match the pinned fingerprint.
- **Remaining Risk**: Denial of Service (attacker can drop packets, preventing transfer).
- **Required Test**: Connect with a mismatched certificate; assert connection terminates in TLS alert `bad_certificate`.

### T04 — Local DNS Spoofing / Hijacking
- **Attack Path**: Attacker answers DNS queries with a malicious IP address.
- **Impact**: Phone connects to malicious server.
- **Mitigation**: The QR code communicates the desktop's literal IPv4 address (`192.168.1.7`), bypassing DNS resolution entirely.
- **Remaining Risk**: None (DNS is not queried).
- **Required Test**: Assert QR URI contains raw IPv4, not a hostname.

### T05 — TLS Interception via Proxy / Corporate Inspection
- **Attack Path**: Corporate proxy or attacker attempts TLS termination with a custom CA.
- **Impact**: Decryption of payload.
- **Mitigation**: Certificate fingerprint pinning in the companion app validates the leaf certificate directly; corporate root CAs installed in the phone OS trust store are bypassed.
- **Remaining Risk**: If user uses an untrusted app build.
- **Required Test**: Unit test companion app's `X509TrustManager` with a certificate signed by a valid system CA but differing fingerprint; assert rejection.

### T06 — Modified Browser JavaScript (Stage 13.0 Vulnerability Eliminated)
- **Attack Path**: Attacker intercepts unauthenticated HTTP traffic and rewrites JavaScript to exfiltrate secrets.
- **Impact**: Complete bypass of client encryption.
- **Mitigation**: The zero-install HTTP browser model was completely discarded in Stage 13.3. The native companion app has compiled, signed code that cannot be altered over the network.
- **Remaining Risk**: None (no web scripts served over plain HTTP).
- **Required Test**: Verify KIVO serves zero unauthenticated HTML/JS over plain HTTP.

### T07 — QR Token Theft / Shoulder Surfing
- **Attack Path**: An observer photographs the QR code on the desktop monitor or intercepts it from across the room.
- **Impact**: Attacker attempts pairing from an unauthorized device.
- **Mitigation**:
  1. The QR pairing secret `T_pair` is single-use and expires in 5 minutes.
  2. The desktop displays an explicit confirmation dialog: `"Incoming pairing from 'Pixel 8' (192.168.1.45), Code: 482 910. Approve?"`
  3. The physical desktop user must manually click "Approve". An unauthorized connection is simply rejected.
- **Remaining Risk**: Social engineering if user blindly clicks "Approve".
- **Required Test**: Mock pairing handshake without desktop approval; assert request times out after 60s and token is burned.

### T08 — Pairing Handshake Replay Attack
- **Attack Path**: Attacker captures handshake packets and re-sends them to establish a duplicate session.
- **Impact**: Unauthorized session activation.
- **Mitigation**: Handshake includes a random client nonce $N_c$ and timestamp. `T_pair` is burned immediately after first use regardless of outcome. Replayed handshakes fail with `401 Unauthorized`.
- **Remaining Risk**: None.
- **Required Test**: Attempt replay of an identical handshake payload; assert second attempt fails with `401`.

### T09 — Device Impersonation (Spoofed MAC/IP)
- **Attack Path**: Attacker assigns their device the same IP or MAC address as the paired phone.
- **Impact**: Attacker attempts to hijack session.
- **Mitigation**: Authentication is based on a high-entropy 256-bit cryptographically random bearer token transmitted over pinned TLS 1.3, not IP or MAC address.
- **Remaining Risk**: None (IP spoofing does not yield the bearer token).
- **Required Test**: Send requests from a different IP with valid token vs same IP with invalid token; assert token validity is the sole authenticator.

### T10 — Session Hijacking via Token Stealing
- **Attack Path**: Malware on the phone attempts to extract the session token.
- **Impact**: Attacker can upload or download files within session TTL.
- **Mitigation**: Token is stored in Android KeyStore (hardware-backed Keystore / Secure Enclave). Sessions enforce 2-hour max TTL and are instantly revocable on desktop.
- **Remaining Risk**: Fully compromised phone OS with root/kernel exploits.
- **Required Test**: Revoke session on desktop; assert subsequent requests immediately return `401`.

### T11 — Upload Abuse (Flooding PC with Unsolicited Files)
- **Attack Path**: A paired phone attempts to upload 100 unwanted files in rapid succession.
- **Impact**: Disk spam, user annoyance.
- **Mitigation**: Max 2 concurrent transfers enforced. Every received file remains in quarantine and requires desktop user "Accept" before being written to persistent storage.
- **Remaining Risk**: Quarantined temporary storage consumption.
- **Required Test**: Send 10 concurrent upload requests; assert requests beyond 2 receive `429 Too Many Requests`.

### T12 — Malicious Filenames (Path Traversal / Shell Injection)
- **Attack Path**: Client sends filename `../../../../etc/shadow` or `test\x00.exe` or bidi override `doc\u202epdf.exe`.
- **Impact**: Path traversal, file overwrite outside Downloads.
- **Mitigation**:
  1. Internal storage uses `uuid.uuid4().hex` exclusively. The client's filename is never used as an open path.
  2. When moving to `~/Downloads/`, filename is sanitized with `os.path.basename`, stripped of control codes/bidi overrides, and validated against `[A-Za-z0-9_.\ -]{1,128}`.
- **Remaining Risk**: None.
- **Required Test**: Send traversal filenames (`../`, `..\\`, `/etc/`, null bytes); assert output in `~/Downloads` is safely normalized.

### T13 — Filesystem Traversal via Symlinks in Quarantine
- **Attack Path**: Attacker crafts a zip or tar upload with a symlink targeting `/etc/passwd`.
- **Impact**: Unauthorized file overwrite or disclosure.
- **Mitigation**: Ingress streams write raw bytes to a single regular file descriptor opened with `O_CREAT | O_EXCL | O_NOFOLLOW | O_WRONLY`. KIVO does not automatically unpack archives.
- **Remaining Risk**: User manually unpacking malicious archive later (out of scope for transport bridge).
- **Required Test**: Assert quarantine manager rejects symlinks.

### T14 — Storage Exhaustion (tmpfs vs Physical Disk)
- **Attack Path**: Attacker sends a 500 GB file stream to fill the desktop's RAM or disk.
- **Impact**: Out-of-memory crash, system freeze.
- **Mitigation**:
  1. Hard file size ceiling: 2 GiB.
  2. Pre-transfer check via `os.statvfs`: upload rejected with `507 Insufficient Storage` if free space < (declared size + 1 GiB).
  3. Dynamic quarantine tiering: Files > 50 MiB are routed away from `/run/user/1000` (`tmpfs`, 1.6 GB) to physical disk (`$XDG_CACHE_HOME`, 18 GB free).
  4. Streaming byte counter: if actual bytes exceed declared length, connection is severed and quarantine file unlinked.
- **Remaining Risk**: Legitimate disk full state if disk was already at 99%.
- **Required Test**: Mock upload with declared size larger than free disk space; assert `507` response.

### T15 — Concurrent Transfer Resource Exhaustion
- **Attack Path**: Client opens 50 parallel streaming connections to exhaust file descriptors and worker threads.
- **Impact**: Uvicorn worker starvation.
- **Mitigation**: Server-side semaphore limits concurrent active uploads to 2 and active downloads to 2. Excess connections receive `503 Service Unavailable`.
- **Remaining Risk**: None.
- **Required Test**: Exceed concurrent transfer limit; assert `503`.

### T16 — Interrupted Transfers / Orphaned Quarantine Files
- **Attack Path**: Wi-Fi disconnects midway through a 1.5 GB transfer.
- **Impact**: Leftover `.part` files waste storage space.
- **Mitigation**:
  1. Stalled transfers with no chunk activity for >30 seconds are timed out and unlinked.
  2. Unaccepted quarantine files older than 15 minutes are purged automatically.
  3. Service `initialize()` and `close()` wipe the quarantine directories unconditionally.
- **Remaining Risk**: Sudden system power failure before cleanup runs (cleaned on next boot).
- **Required Test**: Abort upload midway; assert `.part` file is deleted after timeout.

### T17 — Quarantine Escape (Direct Execution / Agent Ingestion)
- **Attack Path**: Transferred file contains an executable `.sh` script or malicious LLM prompt injection text.
- **Impact**: Code execution or agent takeover.
- **Mitigation**:
  1. Quarantine files are non-executable (mode `0600`).
  2. File Bridge is strictly isolated from LLM prompt contexts and agent tool registries.
  3. Received files are never automatically executed or opened upon receipt.
- **Remaining Risk**: User manually executes malicious file after accepting to Downloads.
- **Required Test**: Verify zero agent tools registered for bridge; assert mode 0600 on quarantine files.

### T18 — Unauthorized File Download from PC
- **Attack Path**: Client sends `GET /api/v1/download?path=/home/muzzu/.ssh/id_rsa`.
- **Impact**: Critical host credential theft.
- **Mitigation**:
  1. The API does not accept filesystem paths.
  2. Downloads require an explicit `ticket_id` generated by the desktop user on KIVO Dashboard.
  3. Tickets are bound to a single pre-authorized file, single-use, and expire in 5 minutes.
- **Remaining Risk**: None.
- **Required Test**: Send GET request with arbitrary path parameters; assert `404 / 400`.

### T19 — Server Shutdown During Active Transfer
- **Attack Path**: Desktop user closes KIVO or shuts down system while transfer is in-flight.
- **Impact**: Hang on shutdown, corrupted state.
- **Mitigation**: `PhoneBridgeService.close(timeout=3.0)` stops Uvicorn server gracefully, cancels active transfer tasks, unlinks incomplete `.part` files, and joins worker threads within bounded 3.0s window.
- **Remaining Risk**: None.
- **Required Test**: Call `close()` during active mock upload; assert thread terminates within 3.0s and zero socket leaks.

---

## 3. Residual Risks and Future Quality Gates

| ID | Residual Risk | Risk Level | Architectural Justification |
|---|---|---|---|
| **R1** | Physical access to unlocked desktop can approve pairing | Low | Desktop lock screen is the physical boundary; same-user trust model applies. |
| **R2** | User blindly approves malicious executable file into Downloads | Low | User-initiated action; KIVO sanitizes names and never executes files automatically. |
| **R3** | Android Companion requires APK installation | Low | Necessary trade-off to eliminate fatal browser WebCrypto insecure-context vulnerabilities. |
