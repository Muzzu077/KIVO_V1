# KIVO — Stage 13.3.0 Secure Architecture Specification: Phone-to-PC Bridge

```
Document:   Stage 13.3.0 Secure Architecture Specification
Status:     Approved Architectural Baseline
Date:       2026-11-09
Baseline:   HEAD 1062c0d; Stage 12.2 and Stages 13.1–13.2 completed
Features:   Authenticated Local Phone-to-PC Bridge
```

---

## 1. Architecture Decision Record (ADR): Transport & Trust Model

### 1.1 Context and Problem Statement
KIVO requires a secure, offline-capable mechanism to transfer files between smartphones (Android / iOS) and the desktop PC over the local Wi-Fi network. The preliminary proposal in Stage 13.0 assumed a browser-based PWA delivered over plain HTTP with client-side WebCrypto encryption using a key in the URL fragment. As proven in `STAGE_13_3_RUNTIME_COMPATIBILITY.md`, this is technically impossible (modern mobile browsers disable `window.crypto.subtle` on non-localhost HTTP) and structurally vulnerable to active LAN code tampering.

We must select a defensible transport and trust architecture that meets KIVO's non-negotiable security standards.

### 1.2 Evaluation of Alternatives

#### Option A — Trusted HTTPS PWA (Public CA / Let's Encrypt / Split DNS)
- **Concept**: Assign each desktop a public DNS subdomain (e.g. `<machine-id>.kivo.local.net`) pointing to the local LAN IP (`192.168.1.7`), obtain an automated Let's Encrypt certificate using DNS-01 ACME challenge, and serve the PWA over HTTPS with a globally trusted public CA certificate.
- **Evaluation**:
  - *Pros*: Zero install on phone; trusted by mobile Chrome/Safari without security warnings; WebCrypto is enabled.
  - *Cons*: **Violates local-only and offline invariants**. Requires an active internet connection, a registered public domain, public DNS servers, and automated ACME renewals. Fails completely if the local router has DNS rebind protection (which blocks public domains resolving to RFC 1918 private IPs) or when offline.
  - *Verdict*: **Rejected** due to cloud dependency, privacy leakage (public DNS query logs), and offline failure.

#### Option B — Native Companion App with TLS Certificate Fingerprint Pinning
- **Concept**: A small, open-source native companion application (Android / iOS) that pairs with KIVO using a QR code. KIVO generates an ephemeral self-signed TLS 1.3 certificate on startup and includes its SHA-256 fingerprint in the QR code. The native companion directly connects to KIVO's LAN IP over TLS 1.3, pinning the certificate fingerprint.
- **Evaluation**:
  - *Pros*:
    - **100% Offline & Local**: Zero internet, zero external DNS, zero cloud coordination.
    - **Immune to Active LAN Attacks**: Certificate fingerprint pinning eliminates MITM risks entirely (even on public/rogue Wi-Fi).
    - **Native Cryptography & Performance**: Uses Android KeyStore and OS-native HTTP/WebSocket engines.
    - **Background Transfers**: Native mobile foreground service prevents OS sleep from terminating 2 GiB transfers.
    - **Zero Browser Security Warnings**: Clean, modern, trustworthy user experience.
  - *Cons*: Requires installing an APK on Android (via F-Droid or direct download) or TestFlight/App Store build on iOS.
  - *Verdict*: **ACCEPTED as the Primary Architecture**. Security and offline resilience take precedence over zero-install convenience.

#### Option C — Browser Companion with Manual Root CA Installation
- **Concept**: Desktop generates a local Certificate Authority (CA) and requires the user to download and install a root certificate profile into their smartphone's OS trust store.
- **Evaluation**:
  - *Pros*: Allows browser use without public DNS.
  - *Cons*: Installing custom root certificates on a smartphone severely degrades the user's overall mobile security posture (exposing all mobile web traffic to potential interception), requires complex manual multi-step configuration, and creates user friction far worse than installing a dedicated app.
  - *Verdict*: **Rejected** as a dangerous anti-pattern.

#### Option D — WebRTC Local PWA via Public Signaling Server
- **Concept**: PWA connects to a public STUN/TURN/signaling server (e.g. Snapdrop/PairDrop model) to negotiate local WebRTC DataChannels.
- **Evaluation**:
  - *Pros*: Browser-based zero install.
  - *Cons*: **Violates local-only invariant**. Requires public signaling server; fails when internet is disconnected. Metadata (IP addresses and connection events) leaks to signaling server.
  - *Verdict*: **Rejected**.

### 1.3 Decision Outcome: Option B (Native Companion with Certificate Pinning)

| Requirement | Stage 13.0 Initial Proposal | Selected Stage 13.3 Architecture (Option B) |
|---|---|---|
| **Mobile Installation** | None (Browser PWA) | **Lightweight Native Companion (Android APK)** |
| **Internet Access** | None | **None (100% Offline / Local LAN)** |
| **Trusted Public CA** | Not required | **Not required (Direct SHA-256 Fingerprint Pinning)** |
| **External Cloud Services** | None | **Zero (100% Peer-to-Peer)** |
| **Transport Security** | Plain HTTP (Vulnerable) | **TLS 1.3 with AES-256-GCM / ChaCha20-Poly1305** |
| **Phone Authenticates Desktop** | URL fragment `#k=` (Insecure Context) | **TLS Certificate Fingerprint pinned via QR Code** |
| **Desktop Authenticates Phone** | URL fragment token | **HMAC-SHA256 Challenge + Short Auth String (SAS) + Desktop Approval Gate** |
| **Background Transfers** | Tab sleep aborts | **Native Android Foreground Service / WorkManager** |

---

## 2. Component Architecture & Service Boundaries

```
┌────────────────────────────────────────────────────────────────────────┐
│ KivoApp (kivo/app.py)                                                 │
│  owns: PhoneBridgeService (alongside Telemetry, Mic, Shelf, Clipboard) │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ initialize() / close()
┌───────────────────────────────────▼────────────────────────────────────┐
│ PhoneBridgeService (kivo/bridge/service.py)                            │
│  Coordinates server lifecycle, pairing state, and transfer queues     │
└───────────────┬───────────────────┬───────────────────┬────────────────┘
                │                   │                   │
┌───────────────▼───────────┐ ┌─────▼───────────┐ ┌─────▼────────────────┐
│ PairingManager            │ │ TransferManager │ │ QuarantineManager    │
│  kivo/bridge/pairing.py   │ │  .../transfer.py│ │  kivo/bridge/        │
│  • Ephemeral X.509 cert   │ │  • Chunked stream│ │    quarantine.py     │
│  • QR payload generator   │ │  • SHA-256 verify│ │  • Tiered storage    │
│  • SAS 6-digit derivation │ │  • Download ticket│ │  • Mode 0600 files   │
│  • Token session store    │ │  • Quotas & limits│ │  • Auto-clean on exit│
└───────────────┬───────────┘ └─────┬───────────┘ └─────┬────────────────┘
                │                   │                   │
┌───────────────▼───────────────────▼───────────────────▼────────────────┐
│ BridgeServer (kivo/bridge/server.py)                                   │
│  • Ephemeral Uvicorn ASGI server with TLS 1.3                          │
│  • Binds STRICTLY to active LAN IPv4 (e.g. 192.168.1.7:8443)           │
│  • REST API: /api/v1/pair, /api/v1/upload, /api/v1/download, /ws       │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Unix-domain Socket IPC
┌───────────────────────────────────▼────────────────────────────────────┐
│ KIVO IPC Server & Quickshell UI                                        │
│  • Shows QR code, SAS confirmation prompt, and Incoming File prompts   │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Network Isolation & Dynamic Interface Binding

### 3.1 Strict Physical Interface Binding
- **Prohibited**: Binding to `0.0.0.0` or `::` is strictly forbidden.
- **Enforcement**:
  1. On `bridge_start`, `PhoneBridgeService` inspects routing tables using `psutil.net_if_addrs()` and socket connection probing to determine the default gateway interface.
  2. The interface is verified to be a physical interface (`wlp*` for Wi-Fi or `enp*` for Ethernet).
  3. All virtual bridge interfaces (`docker0`, `br-*`, `veth*`, `virbr*`, `tun*`) are explicitly filtered out.
  4. The server binds strictly to the discovered private IPv4 address (e.g. `192.168.1.7:8443`).
  5. If the network interface state changes (e.g. Wi-Fi disconnects or IP changes), the background watcher triggers immediate bridge teardown and invalidates active sessions.

### 3.2 Ephemeral Port Selection
- Default port: `8443`.
- If `8443` is already bound by another process, KIVO dynamically requests an ephemeral OS-assigned port (`port=0`), registers the actual bound port, and embeds it into the QR code.

---

## 4. Dynamic Quarantine Storage Architecture

### 4.1 The 1.6 GB tmpfs Constraint & Solution
As documented in `STAGE_13_3_RUNTIME_COMPATIBILITY.md`, `$XDG_RUNTIME_DIR` (`/run/user/1000`) is mounted on `tmpfs` with only 1.6 GB total space. Writing a 2 GiB upload to `/run/user/1000` will crash the desktop session.

### 4.2 Two-Tier Quarantine Model
`QuarantineManager` implements dynamic two-tier storage:

```
                         Incoming Transfer Size Check
                                      │
               ┌──────────────────────┴──────────────────────┐
               ▼                                             ▼
        Size <= 50 MiB                               Size > 50 MiB (up to 2 GiB)
               │                                             │
      Tier 1 (RAM tmpfs)                            Tier 2 (Disk Cache)
$XDG_RUNTIME_DIR/kivo/bridge/quarantine/     $XDG_CACHE_HOME/kivo/bridge/quarantine/
               │                                             │
      • Fast in-memory stream                       • Located on physical ext4 disk
      • Zero flash write wear                       • 18 GB available storage
      • Auto-cleared on reboot                      • Protected directory mode 0700
```

### 4.3 Quarantine File Security Rules
1. Files are assigned a random internal name: `quarantine_<uuid>.part`. Incoming client filenames are **never used as filesystem paths**.
2. File permissions: mode `0600` (readable/writable only by UID 1000).
3. Incremental disk space checks: Before accepting an upload, `os.statvfs` verifies available space exceeds `file_size + 1 GiB` safety margin.
4. Automatic cleanup:
   - Incomplete transfers that stall for >30 seconds are unlinked immediately.
   - On service startup and shutdown, both quarantine directories are purged.
5. Atomic Commitment to Downloads:
   - When the user clicks "Accept" on desktop, the file is atomically renamed (`os.replace` or cross-device atomic copy/unlink) into `~/Downloads/`.
   - Collision resolution: If `report.pdf` exists, saves as `report (1).pdf`; never overwrites existing files.

---

## 5. File Download Protocol (PC -> Phone)

1. **Explicit Selection**: The user selects a specific file from the File Shelf or an approved workspace via KIVO Dashboard.
2. **Download Ticket Issuance**:
   - Desktop issues an ephemeral download ticket:
     ```python
     @dataclass(frozen=True)
     class DownloadTicket:
         ticket_id: str         # 32-byte cryptographically secure token
         canonical_path: str    # Revalidated regular file path
         device_id: str         # Bound to specific paired device ID
         file_size: int
         sha256_hash: str
         expires_at: float      # 5 minutes TTL
         is_used: bool          # Single-use flag
     ```
3. **Download Execution**:
   - Phone issues `GET /api/v1/download/{ticket_id}` over authenticated TLS.
   - Server revalidates ticket: single-use, unexpired, caller authenticated.
   - Server re-opens file with descriptor-relative `O_NOFOLLOW` check to ensure the file was not replaced.
   - File streams to phone in 64 KiB chunks with incremental SHA-256 verification.
   - Ticket is invalidated immediately upon completion.
4. **No Browsing**: The phone client has **zero ability to list or browse PC directories**.

---

## 6. Security Guarantees & Non-Goals

### Guarantees
- **Transport Confidentiality & Integrity**: TLS 1.3 with pinned SHA-256 certificate fingerprint guarantees no attacker on the local network (active or passive) can inspect or tamper with files.
- **Mutual Authentication**: Phone authenticates PC via QR cert fingerprint; PC authenticates phone via SAS confirmation and explicit user approval.
- **No Agent Surface**: Phone Bridge endpoints, transfers, and pairing states are strictly segregated from AI model prompt contexts and tool executors.
- **Zero Disk Exposure Without Approval**: Incoming files remain quarantined and cannot be accessed or executed until explicit user acceptance.

### Non-Goals
- **Wide Area / Internet Sharing**: The bridge does not support cellular remote transfer or internet relays.
- **Permanent Sync / Cloud Backup**: This is an on-demand, explicit transfer utility, not a background sync daemon like Syncthing.
- **Multi-Tenant Desktop Sharing**: KIVO assumes single-user desktop ownership.
