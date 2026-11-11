# KIVO — Stage 13.3.0 QR Pairing & Cryptographic Protocol Specification

```
Document:   Stage 13.3.0 QR Pairing & Cryptographic Protocol Specification
Status:     Approved Protocol Specification
Date:       2026-11-09
Baseline:   HEAD 1062c0d; Stage 12.2 and Stages 13.1–13.2 completed
Security:   TLS 1.3 Pinning + Ephemeral Secret + HKDF-SHA256 SAS + Desktop Approval Gate
```

---

## 1. Protocol Overview

The KIVO Phone Bridge pairing protocol provides mutual cryptographic authentication and transport encryption between a smartphone and KIVO desktop over local Wi-Fi, completely offline, without reliance on external Certificate Authorities or public DNS.

### Cryptographic Primitives
- **Transport Security**: TLS 1.3 with cipher suites `TLS_AES_256_GCM_SHA384` and `TLS_CHACHA20_POLY1305_SHA256`.
- **Server Identity Proof**: SHA-256 fingerprint of the desktop's ephemeral X.509 certificate pinned via QR code.
- **Key Derivation Function**: HKDF-SHA256 (RFC 5869).
- **Session Authentication**: HMAC-SHA256 (RFC 2104).
- **Mutual Verification**: 6-digit Short Authentication String (SAS) derived from handshake transcripts.

---

## 2. Ephemeral TLS Certificate Generation

Upon `bridge_start`, `PairingManager` dynamically generates an in-memory X.509 certificate and private key using Python's `cryptography` library:

1. **Private Key**: 256-bit ECDSA key on the `SECP256R1` curve (or Ed25519).
2. **Subject & Issuer**: `CN=kivo-bridge.local, O=KIVO Desktop, OU=Bridge`.
3. **Subject Alternative Names (SAN)**:
   - `IPAddress: 192.168.1.7` (the verified LAN IP).
   - `DNSName: kivo-bridge.local`.
4. **Validity Window**: Ephemeral; strictly bounded to 24 hours (`not_valid_before = now - 5min`, `not_valid_after = now + 24hr`).
5. **Fingerprint Calculation**:
   $$\text{Cert\_FP} = \text{SHA-256}(\text{DER\_encoded\_certificate})$$
   Formatted as 64 hex characters or colon-delimited hex (`E4:2A:B1:...`).

---

## 3. QR Code Payload Schema

The QR code encodes a standard URI scheme recognized by the KIVO Native Companion app:

```
kivo-pair://v1?addr=192.168.1.7:8443&fp=a1b2c3d4...&t=xyz789...&id=sess_uuid&exp=1700003600
```

### JSON-Equivalent Payload Fields

| Parameter | Type | Description | Security Requirement |
|---|---|---|---|
| `v` | integer | Protocol version (`1`). | Strict version match. |
| `addr` | string | IPv4 and port of desktop (`192.168.1.7:8443`). | Verified LAN IP only; never loopback. |
| `fp` | string | SHA-256 hex fingerprint of ephemeral TLS certificate. | Pinned by mobile TLS client; eliminates MITM. |
| `t` | string | One-time pairing secret `T_pair` (32 random bytes, base64url). | High-entropy (256 bits). Single-use. |
| `id` | string | Ephemeral session pairing UUID (`sess_<uuid>`). | Identifies pairing request in memory. |
| `exp` | integer | Expiration timestamp (epoch seconds, 5-minute TTL). | Expired QR codes are rejected by server. |
| `name` | string | Friendly desktop hostname (e.g. `KIVO Desktop (niri)`). | Display only (sanitized, max 32 chars). |

---

## 4. End-to-End Pairing Handshake Sequence

```
Phone (Native Companion)                             KIVO Desktop (BridgeServer)
  │                                                                 │
  │  1. Scans QR Code                                               │
  │     Extracts: addr, fp, t, id                                   │
  │                                                                 │
  │  2. TLS 1.3 Handshake ClientHello ────────────────────────────> │
  │  <─────────────────────────── ServerHello + Ephemeral Cert ──── │
  │     [Client validates SHA-256(Cert) == fp]                      │
  │     (TLS Channel is now secure against MITM)                    │
  │                                                                 │
  │  3. POST /api/v1/pair/handshake ──────────────────────────────> │
  │     Payload: { session_id: id, client_nonce: N_c,               │
  │                device_name: "Pixel 8", client_id: "dev_uuid" }  │
  │                                                                 │
  │  4. Both derive Key Material via HKDF-SHA256:                   │
  │     PRK = HKDF-Extract(salt=N_c, IKM=T_pair)                    │
  │     SAS_bytes = HKDF-Expand(PRK, info="kivo-sas-v1", L=4)       │
  │     SAS_code = (SAS_bytes as uint32) mod 1,000,000              │
  │     (e.g., "482 910")                                           │
  │                                                                 │
  │  5. Displays SAS: "482 910"           Displays SAS: "482 910"   │
  │                                       Pops Desktop Modal:       │
  │                                       "Pair Pixel 8 (482 910)?" │
  │                                                                 │
  │                                       6. User clicks "Approve"  │
  │                                          on KIVO Desktop!       │
  │                                                                 │
  │  <─────────────────────────── 200 OK + SessionToken ─────────── │
  │     Payload: { token: "sess_token_...", expires_in: 7200 }      │
  │                                                                 │
  │  7. Established Session: Phone stores token in KeyStore         │
  │     T_pair is invalidated; QR code destroyed                    │
```

### 4.1 Step-by-Step Security Invariants

1. **Step 2 (TLS Pinning)**: The mobile app’s custom `X509TrustManager` validates that the SHA-256 hash of the received peer certificate matches `fp` exactly. If an attacker on the Wi-Fi performs an ARP spoof or DNS hijack, the handshake fails immediately before any application data is sent.
2. **Step 3 (Proof of Token)**: The client proves possession of the QR code secret `T_pair` by including `HMAC-SHA256(T_pair, N_c + session_id)` in the handshake header.
3. **Step 4 (SAS Derivation)**: The Short Authentication String (SAS) is a 6-digit decimal number derived deterministically on both sides using HKDF. Because it binds the client nonce, session ID, and secret, an attacker cannot forge matching codes.
4. **Step 6 (Physical Consent Gate)**: Pairing **cannot complete without manual desktop approval**. Even if an attacker photographs the QR code, the desktop pops up an explicit dialog showing the phone's IP address and device name. If the user clicks "Reject" or ignores it, the session is purged.

---

## 5. Session State & Token Management

```python
@dataclass
class PairedSession:
    session_id: str              # Unique device session UUID
    device_name: str             # "Pixel 8" (sanitized)
    client_ip: str               # "192.168.1.45"
    session_token: str           # Cryptographically secure 32-byte bearer token
    created_at: float            # Monotonic / epoch timestamp
    last_active: float           # Heartbeat timestamp
    expires_at: float            # TTL (2 hours)
    is_revoked: bool = False
```

### Storage and Revocation Policy
- **In-Memory Only**: Active session tokens reside strictly in memory inside `PairingManager`. They are never written to unencrypted disk files.
- **Heartbeat & Inactivity**:
  - The phone client sends `POST /api/v1/ping` every 30 seconds.
  - If no heartbeat is received for 10 minutes, the session enters `dormant` state.
- **Instant Revocation**:
  - When the desktop user clicks "Revoke" on KIVO Dashboard, `session.is_revoked = True` and the token is expunged from memory.
  - Any subsequent request using that token immediately receives `401 Unauthorized`.
- **Teardown on Bridge Stop**:
  - When the desktop user toggles off the Phone Bridge, all session tokens are purged, the listening socket is closed, and the server thread terminates.

---

## 6. Brute-Force & Flood Protections

1. **Strict Rate Limiting**:
   - `/api/v1/pair/handshake`: Max 5 requests per minute per IP.
   - Three consecutive failed handshake attempts trigger a 5-minute IP lockout.
2. **One-Time Token Burning**:
   - `T_pair` is valid for exactly one pairing attempt. Whether the pairing is approved or rejected, `T_pair` is destroyed immediately.
3. **Single Active Pairing Window**:
   - Only one pending pairing request can be in-flight at any time. A second pairing request while a prompt is displayed on desktop is rejected with `409 Conflict`.
4. **Ephemeral Port Randomization**:
   - In addition to standard port `8443`, the bridge supports dynamic ephemeral port allocation (`port=0`) to prevent port squatting.
