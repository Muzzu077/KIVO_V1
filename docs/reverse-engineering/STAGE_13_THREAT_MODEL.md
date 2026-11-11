# KIVO — Stage 13 Threat Model: Local Productivity Utilities

```
Document:   Stage 13.0 Threat Model
Status:     Proposed (Research & Specification Stage)
Date:       2026-11-09
Baseline:   HEAD 1062c0d; Stage 12.2 completed
Features:   Clipboard Intelligence, File Shelf, Phone-to-PC Bridge
Scope:      Trust boundaries, assets, threat enumeration (T1–T16), mitigations, residual risks
```

---

## 1. Security Objectives

1. **No Unintended Secret Retention.** Passwords, authentication codes, API keys, and private credentials copied to the clipboard must not be captured, displayed in cleartext, or written to persistent history.
2. **No Arbitrary File Execution or Injection.** Dropping a file onto the File Shelf or receiving a file from a phone must never trigger automatic execution, terminal interpretation, or injection into AI model context.
3. **No Silent File Overwrites or Traversals.** Incoming files must never overwrite existing files without explicit collision resolution and must never escape designated quarantine boundaries.
4. **No Unauthorized Local Network Access.** The Phone Bridge must never expose an open, unauthenticated HTTP server or listen on public/all interfaces (`0.0.0.0`).
5. **Mandatory Desktop Consent Gates.** The desktop user must explicitly approve phone pairing (with SAS code confirmation) and must explicitly accept each received file before it reaches persistent storage.
6. **Workspace Sandboxing Intact.** The productivity utilities must not bypass `FileIntelligenceService` or `MutationService` boundaries.

---

## 2. Assets and Data Classification

| Asset | Classification | Security Concern |
|---|---|---|
| **Clipboard History** | High / Confidential | May contain passwords, private keys, authentication tokens, personal messages |
| **Pinned Clipboard Items** | Confidential | Long-term stored text/images on local disk (`$XDG_DATA_HOME/kivo`) |
| **Staged File References** | Sensitive / Local | File paths, metadata, and workspace mappings |
| **Incoming Quarantine Files** | Untrusted Data | Files received over the network prior to desktop user acceptance |
| **Pairing Secrets & Tokens** | High / Secret | Ephemeral 256-bit keys authorizing phone session |
| **Short Authentication String (SAS)** | Verification Data | 6-digit confirmation code verifying peer identity |
| **Local Network Socket** | Attack Surface | Ephemeral TCP port bound to LAN interface |
| **KIVO IPC Socket** | Protected Local IPC | Unix stream socket (`$XDG_RUNTIME_DIR/kivo/kivo.sock`, mode `0600`) |

---

## 3. Trust Boundaries

```
┌────────────────────────────────────────────────────────────────────────┐
│ Local Area Network (Wi-Fi / Subnet)                                     │
│  Untrusted: Other devices, rogue APs, ARP spoofers                     │
└───────────────────────────────┬────────────────────────────────────────┘
                                │ HTTPS / WSS (LAN IP only, e.g. 192.168.1.7)
┌───────────────────────────────▼────────────────────────────────────────┐
│ Phone Web Client (Zero-Install PWA)                                    │
│  Partially Trusted: Authenticated after QR scan + SAS code approval   │
└───────────────────────────────┬────────────────────────────────────────┘
                                │ FastAPI / Uvicorn (Ephemeral Server)
┌───────────────────────────────▼────────────────────────────────────────┐
│ KIVO Backend Daemon (Python 3.14, same user UID 1000)                  │
│  Trusted Core: ClipboardService, FileShelfService, PhoneBridgeService  │
└───────────────────────────────┬────────────────────────────────────────┘
                                │ AF_UNIX stream (kivo.sock, mode 0600)
┌───────────────────────────────▼────────────────────────────────────────┐
│ Quickshell UI (Qt6 / Wayland Layer-Shell)                              │
│  Presentation only: Dynamic Notch, Dashboard                           │
└───────────────────────────────┬────────────────────────────────────────┘
                                │ Screen / Pointer
┌───────────────────────────────▼────────────────────────────────────────┐
│ Desktop User                                                           │
│  Ultimate Authority: Approves pairing, accepts transfers, pins items   │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Threat Enumeration & Architectural Mitigations (T1 – T16)

### T1 — Clipboard Secret Leakage
- **Scenario**: User copies a master password from Bitwarden, an AWS secret key, or a GitHub token. The clipboard daemon records it, displaying it in history or writing it to disk.
- **Impact**: Permanent credential exposure in local logs or unencrypted cache files.
- **Likelihood**: High (occurs during daily computer use).
- **Mitigations**:
  1. Inspect MIME types for `x-kde-passwordManagerHint: secret` and `application/x-secret`.
  2. Shannon entropy analysis: strings >24 chars with entropy > 4.5 are flagged as sensitive.
  3. Pattern scanning for private keys, JWTs, AWS keys (`AKIA`), and bearer tokens.
  4. Flagged items receive `is_sensitive=True`, mask previews as `••••••••••••`, are excluded from persistent pinned storage, and cannot be sent to remote clients.

### T2 — Malicious Clipboard Content
- **Scenario**: Clipboard contains malicious terminal escape sequences (`\x1b[...`), hidden control characters, or Unicode right-to-left override (bidi) tricks designed to hide commands.
- **Impact**: Terminal code execution when pasted by user; prompt injection if text is fed to an LLM.
- **Likelihood**: Medium.
- **Mitigations**:
  1. All clipboard previews strip C0/C1 control characters and Unicode bidi overrides (`U+202A`–`U+202E`, `U+2066`–`U+2069`).
  2. Clipboard contents are strictly isolated from KIVO agent prompt context.

### T3 — Clipboard Monitoring Abuse
- **Scenario**: A malicious process or misconfigured tool polls clipboard continuously, consuming CPU and logging every keystroke copy.
- **Impact**: Battery drain, user surveillance, log bloat.
- **Likelihood**: Medium.
- **Mitigations**:
  1. Zero busy-loop polling: KIVO watches `~/.cache/cliphist/db` modification via `inotify` with a 250 ms debounce.
  2. Subprocess execution via `run_bounded` with 0.75 s timeout and 16 KiB output limit.
  3. No raw clipboard text is logged to disk or console.

### T4 — Malicious Filenames in Staged or Transferred Files
- **Scenario**: Transferred file named `../../../../etc/shadow` or containing null bytes `file.txt\x00.exe` or terminal control sequences.
- **Impact**: Arbitrary file overwrite, path traversal, shell confusion.
- **Likelihood**: High.
- **Mitigations**:
  1. Strict filename sanitization using `os.path.basename` and regex allowlist `[A-Za-z0-9_.\ -]{1,128}`.
  2. Path separators (`/`, `\`), null bytes (`\x00`), and leading dots (`.`) are stripped or replaced with underscores.

### T5 — Symlink and Path Traversal Attacks
- **Scenario**: User drags a symlink pointing to `~/.ssh/id_rsa` onto the File Shelf, or phone attempts to download a symlink.
- **Impact**: Leakage of private files outside authorized folders.
- **Likelihood**: Medium.
- **Mitigations**:
  1. File Shelf resolves canonical paths using `os.path.realpath`.
  2. File descriptors opened with `O_NOFOLLOW | O_NONBLOCK`.
  3. Symlinks pointing outside approved paths or to sensitive system files (`/etc`, `~/.ssh`, `~/.gnupg`) are flagged and refused for transfer.

### T6 — Silent File Overwrite
- **Scenario**: Phone sends `report.pdf`. An existing `~/Downloads/report.pdf` is silently replaced.
- **Impact**: Data loss of user documents.
- **Likelihood**: High.
- **Mitigations**:
  1. Moving from quarantine uses collision-safe naming: if `file.pdf` exists, saves as `file (1).pdf`, `file (2).pdf`.
  2. `os.replace` is only used after explicit user collision confirmation.

### T7 — Unauthorized File Access via File Shelf
- **Scenario**: Staging a file on the File Shelf is treated by an agent or external client as permission to read/write the entire parent directory.
- **Impact**: Privilege escalation and unauthorized file access.
- **Likelihood**: High.
- **Mitigations**:
  1. Staging is a reference to a single file, NOT an authorization grant for the containing directory.
  2. KIVO agent tools do NOT gain automatic access to the File Shelf.
  3. Moving files to workspaces requires explicit `MutationService` validation against `workspaces.json`.

### T8 — Pairing Token Theft & Shoulder Surfing
- **Scenario**: Someone takes a photo of the desktop screen showing the QR code while the user is away.
- **Impact**: Rogue device attempts pairing.
- **Likelihood**: Low to Medium.
- **Mitigations**:
  1. Ephemeral pairing secrets: QR code expires after 5 minutes if unclaimed.
  2. Mandatory desktop approval gate: Even with the QR code, the desktop pops an approval dialog displaying the 6-digit SAS code, client IP, and device name.
  3. Physical user must click "Approve" on the desktop.

### T9 — Local Network Attacks (ARP Spoofing / Rogue Wi-Fi)
- **Scenario**: Attacker on public coffee-shop Wi-Fi intercepts HTTP traffic or spoofs DNS/ARP to inspect transferred files.
- **Impact**: Confidentiality loss of transferred files.
- **Likelihood**: High on untrusted networks.
- **Mitigations**:
  1. Server binds strictly to the active private LAN IP (`192.168.x.x`), never `0.0.0.0`.
  2. WebCrypto End-to-End Encryption (E2EE): Files and messages are encrypted with AES-256-GCM using the 256-bit key from the QR code fragment before traversing the network.
  3. Since URL hash fragments (`#k=...`) are never transmitted in HTTP headers, network sniffers cannot acquire the encryption key.

### T10 — Replay Attacks
- **Scenario**: Attacker captures signed transfer request packets on the LAN and re-sends them later.
- **Impact**: Repeated file transfers or unauthorized session activation.
- **Likelihood**: Low.
- **Mitigations**:
  1. Every request carries a monotonic nonce and timestamp.
  2. Session tokens are invalidated immediately on bridge disconnect or device revocation.

### T11 — Unauthorized File Uploads
- **Scenario**: A paired phone attempts to upload 50 files in the background without user knowledge.
- **Impact**: Unwanted files, disk spam.
- **Likelihood**: Medium.
- **Mitigations**:
  1. Every incoming file is held in quarantine (`$XDG_RUNTIME_DIR/kivo/bridge/quarantine/`).
  2. A desktop UI dialog prompts for every single transfer batch.
  3. Files remain in quarantine until user clicks "Accept".
  4. Unaccepted files are purged upon session timeout (default 5 minutes).

### T12 — Malicious File Downloads from PC
- **Scenario**: Phone sends a forged GET request attempting to download `/etc/shadow` or `~/.ssh/id_rsa`.
- **Impact**: Critical host compromise.
- **Likelihood**: High if unmitigated.
- **Mitigations**:
  1. Phone cannot query or browse the PC filesystem.
  2. Phone can download ONLY files for which the desktop user generated an explicit single-use ticket via the File Shelf or Dashboard.
  3. Download tickets expire in 5 minutes and are single-use.

### T13 — Oversized Transfers & Storage Exhaustion
- **Scenario**: Phone attempts to upload a 500 GB file or flood the network with junk streams to fill disk.
- **Impact**: Denial of Service, filesystem full.
- **Likelihood**: Medium.
- **Mitigations**:
  1. Strict maximum file size cap (default 2 GB).
  2. Pre-transfer disk space check via `os.statvfs`: upload rejected if free space < (file size + 1 GB safety margin).
  3. Streaming chunk limit: if received bytes exceed declared size, connection is aborted immediately.

### T14 — Interrupted Transfers & Quarantine Leakage
- **Scenario**: File transfer drops midway (Wi-Fi disconnected). Incomplete files accumulate in `$XDG_RUNTIME_DIR`.
- **Impact**: Memory/tmpfs exhaustion in `/run/user/1000`.
- **Likelihood**: High.
- **Mitigations**:
  1. Incomplete transfers are saved as `.part` files in quarantine.
  2. Active transfers track a heartbeat; if stalled >30 seconds, connection drops and `.part` file is deleted.
  3. Service startup and shutdown automatically wipe the quarantine directory.

### T15 — Resource & Process Exhaustion
- **Scenario**: Rapid connection attempts flood the bridge server, exhausting sockets and memory.
- **Impact**: Backend freeze or crash.
- **Likelihood**: Low to Medium.
- **Mitigations**:
  1. Maximum concurrent connected mobile clients: 2.
  2. Maximum concurrent file transfers: 2.
  3. Rate limit: Max 5 connection requests per minute per IP.
  4. Ephemeral server uses asynchronous non-blocking ASGI loop in a dedicated background worker thread.

### T16 — Device Impersonation & Session Revocation Failures
- **Scenario**: User revokes a paired phone, but the phone continues to make requests using cached credentials.
- **Impact**: Continued unauthorized access.
- **Likelihood**: Medium.
- **Mitigations**:
  1. Session tokens are held in-memory in the backend's `PairingManager`.
  2. Clicking "Revoke" deletes the session token instantly; subsequent requests receive `401 Unauthorized`.
  3. Stopping the bridge closes the TCP server socket completely.

---

## 5. Residual Risks & Accepted Limitations

| ID | Residual Risk | Why Accepted | Follow-up / Guidance |
|---|---|---|---|
| **R1** | Physical access to unlocked desktop can approve pairing | Physical desktop access already grants full user privileges | Desktop lock screen (niri/waylock) is the security boundary |
| **R2** | `cliphist` on disk is unencrypted BoltDB | Existing user environment baseline; KIVO does not rewrite host cliphist | Document recommendation to use disk encryption (LUKS) |
| **R3** | Clipboard text briefly visible in memory | Required for rendering UI previews | Sensitive items are masked; raw bytes purged after presentation |
| **R4** | Browser certificate warnings if using direct TLS without PWA hash | Self-signed certs lack CA signatures on local IP | WebCrypto over HTTP fragment (#key) avoids cert warnings while providing E2EE |
| **R5** | Large clipboard images take memory | Bitmaps can consume several megabytes | 5 MiB cap enforced; thumbnail downscaled for UI |

---

## 6. Implementation Security Gates

Before any Stage 13 code is deployed to production:
1. **Secret Scanning**: Test suite validates that passwords, private keys, and tokens are masked and not stored in pinned history.
2. **Path Traversal Gate**: Tests prove that `../`, symlinks to `/etc`, and null bytes in file shelf / bridge transfers fail safely.
3. **Quarantine Isolation Gate**: Tests verify that incoming bridge files remain in quarantine until desktop explicit approval is executed.
4. **Shutdown Teardown Gate**: Tests verify that stopping the bridge service completely frees listening sockets and leaves zero orphaned subprocesses.
