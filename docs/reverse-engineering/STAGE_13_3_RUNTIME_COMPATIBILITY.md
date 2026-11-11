# KIVO — Stage 13.3.0 Runtime Compatibility & Browser Security Analysis

```
Document:   Stage 13.3.0 Runtime Compatibility & Browser Security Analysis
Status:     Approved Research Baseline
Date:       2026-11-09
Target:     Phone-to-PC Bridge Security Architecture Redesign
Host:       Arch Linux (kernel 7.2.14-arch1-1), Wayland, niri 26.04
```

---

## 1. Executive Summary: The WebCrypto Insecure-Context Failure

The Stage 13.0 preliminary proposal envisioned a zero-install browser PWA delivered over plain local HTTP (`http://192.168.1.7:8443`), relying on the W3C Web Cryptography API (`window.crypto.subtle`) for client-side AES-256-GCM encryption with a shared key passed in the URL hash fragment (`#k=...`).

**Empirical & Standards Investigation Result:**  
This architecture is **fundamentally broken and impossible to execute securely in modern mobile browsers**.

1. **W3C Secure Context Specification Enforced**:
   - According to the W3C Secure Contexts specification (RFC 7617 / W3C Recommendation), the `crypto.subtle` API is **strictly restricted to Secure Contexts** (`isSecureContext === true`).
   - In both Google Chrome / Chromium (Android) and Apple Safari / WebKit (iOS), `window.crypto.subtle` is **`undefined`** when loaded over plain HTTP on private IP addresses (`192.168.x.x`, `10.x.x.x`). The only exceptions browsers make for HTTP are `http://localhost` and `http://127.0.0.1`.
   - A smartphone opening `http://192.168.1.7:8443` will immediately throw `TypeError: Cannot read properties of undefined (reading 'subtle')` when attempting any encryption, key derivation, or decryption.
2. **Untrusted Code Delivery (The In-Flight Tampering Fatal Flaw)**:
   - Serving an HTML/JavaScript application over unauthenticated HTTP means that any active attacker on the local network (e.g. ARP cache poisoning, DNS spoofing, rogue access point, or a compromised IoT device on the same Wi-Fi) can **modify the served JavaScript in flight**.
   - Because JavaScript running in the browser has direct programmatic access to `window.location.hash`, a modified script can effortlessly read `#k=<secret_key>` and exfiltrate the key and any uploaded files directly to the attacker.
   - **Crucial Security Axiom**: *Encryption cannot substitute for authenticated code delivery.* If the application binary (the JavaScript) is delivered over an insecure channel, the application's runtime cannot be trusted to protect secrets.
3. **Private Network Access (PNA) and Mixed-Content Blocks**:
   - Modern Chromium implements Private Network Access checks, blocking public-to-private network transitions and restricting LAN resources.
   - iOS Safari blocks local network access unless explicitly granted and imposes strict restrictions on non-standard local web sockets.
4. **Self-Signed HTTPS Browser Warnings**:
   - If the server attempts to provide a Secure Context by using HTTPS with a self-signed certificate on `192.168.1.7`, mobile browsers display alarming interstitial security warnings (`NET::ERR_CERT_AUTHORITY_INVALID` on Android Chrome; "This Connection Is Not Private" on iOS Safari).
   - In modern iOS Safari (iOS 15+), bypassing self-signed certificates on bare IP addresses is frequently blocked entirely without installing a root configuration profile.

---

## 2. Host Runtime Inventory & Hardware Limits

### 2.1 Storage & tmpfs Boundary Constraints

A critical empirical discovery made during Phase 1 investigation:

```
Filesystem      Size  Used Avail Use% Mounted on
tmpfs           1.6G   49M  1.5G   4% /run/user/1000
/dev/nvme0n1p8  147G  121G   18G  88% /
```

- **Runtime Directory (`$XDG_RUNTIME_DIR`) is a `tmpfs`**:
  - `/run/user/1000` has only **1.6 GB total capacity** and **1.5 GB available**.
  - Storing a proposed 2 GiB file in `$XDG_RUNTIME_DIR/kivo/bridge/quarantine/` will **completely fill the RAM-backed tmpfs**, crashing the user session (systemd-user units fail, Unix socket creation fails, Quickshell layer-shell crashes).
- **Physical Disk Storage (`/dev/nvme0n1p8`)**:
  - Root filesystem has **18 GB available**.
  - Large file transfers (> 50 MB) must quarantine on disk (`$XDG_CACHE_HOME/kivo/bridge/quarantine/`), with strict directory permissions (`0700`) and file permissions (`0600`), and automatic cleanup on exit.

### 2.2 Network Interfaces on Host

| Interface | Type | IP Address / Subnet | State | Bridge Eligibility |
|---|---|---|---|---|
| `lo` | Loopback | `127.0.0.1/8`, `::1/128` | UP | Ineligible (local IPC only) |
| `wlp0s20f3` | Wi-Fi (Intel AX211) | `192.168.1.7/24` | UP | **Eligible Physical LAN Interface** |
| `enp109s0` | Ethernet | None | DOWN | Ineligible (no link) |
| `docker0` | Docker Bridge | `172.17.0.1/16` | DOWN | **Strictly Excluded** |
| `br-3154...` | Docker Bridge | `172.19.0.1/16` | UP | **Strictly Excluded** |
| `br-954a...` | Docker Bridge | `172.20.0.1/16` | UP | **Strictly Excluded** |
| `br-0c56...` | Docker Bridge | `172.21.0.1/16` | UP | **Strictly Excluded** |

**Mandatory Socket Binding Rule**:  
The Bridge daemon must discover the active default route interface (`wlp0s20f3`) and bind strictly to `192.168.1.7`, **never to `0.0.0.0`**, to prevent exposing endpoints on container interfaces or external tunnels.

### 2.3 Verified Cryptographic Dependencies in `.venv`

The KIVO Python 3.14 virtual environment already contains full cryptographic and server facilities:

| Library | Version | Tested Capabilities |
|---|---|---|
| `cryptography` | `46.0.5` | `x509` certificate generation, `hazmat.primitives.asymmetric` (X25519, Ed25519, RSA), `ciphers.aead` (AES-GCM, ChaCha20-Poly1305), `kdf.hkdf` |
| `fastapi` | `0.135.1` | Asynchronous REST and WebSocket routing, dependency injection, stream upload handling |
| `uvicorn` | `0.44.0` | ASGI server with SSL support, graceful cancellation |
| `qrcode` | `8.2` | QR code generation with error correction (ECC Level M / Q) |
| `PIL` (Pillow) | `12.1.1` | In-memory image encoding for QML display |
| `websockets` | `16.0` | Low-latency bi-directional messaging |

---

## 3. Detailed Browser Security Evaluation Matrix

| Vector | Plain HTTP + URL Fragment Key | Self-Signed HTTPS in Browser | Native Companion App (Pinned Cert) |
|---|---|---|---|
| **W3C Secure Context** | **FAIL** (`crypto.subtle` is `undefined`) | **PASS** | **N/A** (Native Android/iOS Crypto API) |
| **In-Flight Script Tampering** | **FAIL** (Active LAN attacker can rewrite JS) | **PASS** (Protected by TLS) | **PASS** (Installed code signature verified) |
| **URL Fragment Key Leakage** | **FAIL** (Compromised JS reads `location.hash`) | **WARN** (Browser history/extensions may see fragment) | **PASS** (Secret read from QR camera buffer directly) |
| **Browser Security Interstitials** | **NONE** (Plain HTTP) | **FATAL** (Alarms users, blocked on iOS) | **NONE** (Custom TrustManager pins cert fingerprint) |
| **Offline / Zero-Internet** | **PASS** | **PASS** | **PASS** (100% LAN peer-to-peer) |
| **Background Transfer Support** | **POOR** (Tab sleep cancels transfer) | **POOR** (Tab sleep cancels transfer) | **EXCELLENT** (Foreground service / WorkManager) |
| **Zero-Install User Flow** | **PASS** (Camera scan opens browser) | **FAIL** (Cert warnings abort user flow) | **REQUIRES INSTALL** (One-time APK / app install) |

---

## 4. Key Takeaways for Stage 13.3 Redesign

1. **The zero-install plain HTTP browser model is discarded on grounds of technical impossibility and critical vulnerability.** Modern browsers deliberately disable cryptographic capabilities on plain HTTP LAN IPs to prevent developers from making this exact security mistake.
2. **Self-signed HTTPS in a mobile browser is discarded** because it trains users to bypass certificate warnings, fails outright on modern iOS versions, and breaks the user experience.
3. **The primary secure architecture must be a Native Companion (Option B)** that pairs via QR code and directly pins the desktop's ephemeral TLS certificate fingerprint.
4. **Quarantine storage must be dynamically tiered**: Small transfers (< 50 MB) can use `$XDG_RUNTIME_DIR/kivo/bridge/quarantine/`, while large transfers (up to 2 GiB) must use disk storage under `$XDG_CACHE_HOME/kivo/bridge/quarantine/` to prevent tmpfs exhaustion.
