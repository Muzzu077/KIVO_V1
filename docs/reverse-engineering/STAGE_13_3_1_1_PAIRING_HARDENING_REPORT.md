# KIVO — Stage 13.3.1.1: Pairing Token Consumption & Protocol Hardening Report

```
Document:   Stage 13.3.1.1 Pairing Hardening Report
Status:     Complete
Date:       2026-11-09
Baseline:   HEAD 1062c0d; Stages 12.2 and 13.1–13.3.1 completed
Scope:      Atomic token consumption, replay rejection, SAS assurance analysis,
            session credential contract, TLS fingerprint format specification
```

---

## 1. Initial Defect & Root Cause

### 1.1 The Reported Vulnerability
In the initial Stage 13.3.1 specification, the one-time pairing token was described as being destroyed/burned on the *first handshake attempt*, regardless of whether the cryptographic client proof was valid or whether the request was malformed.

### 1.2 Root Cause Analysis & Security Implications
1. **Denial-of-Service Exposure**: If any unauthenticated client on the LAN sends an invalid request, an unsupported protocol version, or random garbage to `/api/v1/pair/handshake`, burning the pairing secret prematurely would immediately destroy the active desktop pairing offer. The legitimate user whose phone was about to scan the QR code would be denied service and forced to regenerate the QR code repeatedly.
2. **Online Brute-Force vs. Premature Burning Trade-off**:
   - While a single-use token must prevent replay attacks once authenticated, destroying it on a single malformed packet or transient Wi-Fi packet truncation makes the protocol fragile.
   - Conversely, allowing infinite failed attempts would permit an online brute-force search.
3. **The Solution**:
   - **Atomic token consumption on valid proof only**: The pairing token is consumed atomically when a valid client proof over the canonical transcript is verified.
   - **Offer lockout on repeated failures**: A bounded counter (`MAX_FAILED_PROOFS_PER_OFFER = 5`) locks out the offer permanently if repeated invalid proofs are submitted.
   - **Transient rejections do not consume token**: Malformed JSON, unsupported protocol versions, mismatched session IDs, and rate-limited requests fail safely without destroying the offer.

---

## 2. Hardened Token-Consumption State Machine

```
                        ┌──────────────────┐
                        │       IDLE       │
                        └────────┬─────────┘
                                 │ create_offer()
                        ┌────────▼─────────┐
       ┌────────────────┤     OFFERING     ├─────────────────┐
       │ (valid proof)  └────────┬─────────┘  (5 bad proofs) │
       │                         │ (expires 5m)              │
       ▼                         ▼                           ▼
┌──────────────┐        ┌──────────────────┐        ┌──────────────────┐
│  CONFIRMING  │        │     EXPIRED      │        │     EXPIRED      │
│ (Consumed!)  │        │ (Token Invalid)  │        │  (Locked Out!)   │
└──────┬───────┘        └──────────────────┘        └──────────────────┘
       │
       ├─────────────────────────────────┐
       │ approve_pairing()               │ reject_pairing()
┌──────▼───────┐                  ┌──────▼───────┐
│    PAIRED    │                  │   REJECTED   │
│(Active Sess) │                  │(Token Burned)│
└──────┬───────┘                  └──────────────┘
       │ revoke_device()
┌──────▼───────┐
│   REVOKED    │
│(Token Burned)│
└──────────────┘
```

### 2.1 State Transition Matrix

| Current State | Event | Token Consumed? | New State | Response / Result |
|---|---|---|---|---|
| `OFFERING` | Malformed base64 / bad length | **NO** | `OFFERING` | `400 Bad Request` |
| `OFFERING` | Unsupported protocol version | **NO** | `OFFERING` | `400 Bad Request` |
| `OFFERING` | Invalid session ID | **NO** | `OFFERING` | `400 Bad Request` |
| `OFFERING` | Invalid client proof (<5 fails) | **NO** | `OFFERING` (counter++) | `401 Unauthorized` |
| `OFFERING` | Invalid client proof ($\ge$5 fails) | **YES (Locked)** | `EXPIRED` | `429 Too Many Requests` |
| `OFFERING` | Offer TTL expired (5 min) | **YES** | `EXPIRED` | `410 Gone` |
| `OFFERING` | **Valid client proof** | **YES (Atomically)** | `CONFIRMING` | Awaits Desktop Approval |
| `CONFIRMING` | Replayed handshake with same token | **YES (Replay)** | `CONFIRMING` | `409 Conflict` (`token_already_consumed`) |
| `CONFIRMING` | Desktop clicks "Approve" | **YES (Permanent)** | `PAIRED` | `200 OK` (Issues session credentials) |
| `CONFIRMING` | Desktop clicks "Reject" | **YES (Permanent)** | `REJECTED` | `403 Forbidden` |
| `CONFIRMING` | Approval TTL expired (60s) | **YES (Permanent)** | `EXPIRED` | `408 Request Timeout` |
| `PAIRED` | User clicks "Revoke" | **YES (Permanent)** | `REVOKED` | `200 OK` (Session purged) |

---

## 3. Concurrency & Replay Rejection Guarantees

1. **Atomic Transition Under Mutex**: `handle_client_handshake()` executes inside `threading.Lock()`.
2. **Replay Rejection**: `self._consumed_session_ids` tracks consumed tokens. Once a proof is accepted, `session_id` is recorded immediately in `_consumed_session_ids`. Any concurrent or subsequent request with the same token is rejected with `token_already_consumed` (`409 Conflict`).
3. **Single Pending Candidate**: Exactly one candidate can transition to `CONFIRMING` at any time.

---

## 4. Rate-Limiting & Memory Bound Strategy

- **Per-IP Sliding Window**: Max 5 requests per 60 seconds per source IP.
- **Per-Offer Proof Ceiling**: Max 5 failed proof attempts per offer before the offer is terminated.
- **Bounded In-Memory Records**:
  - `_failed_attempts` is capped at `MAX_TRACKED_RATE_LIMIT_IPS = 100`.
  - When reaching capacity, timestamps older than 60 seconds are purged. If still at capacity, the oldest IP entry is evicted (FIFO).
  - Memory consumption is strictly bounded below 50 KiB.

---

## 5. SAS Assurance & Protocol Limitations

### 5.1 What SAS Proves
- **Agreement on Transcript**: Both desktop and phone derive the identical 6-digit Short Authentication String if and only if both endpoints possess the exact same one-time pairing secret $T_{\text{pair}}$ AND agree on the canonical transcript (including protocol version, session ID, server cert fingerprint, server nonce, and client nonce).
- **Protection Against In-Flight MITM Manipulation**: If an attacker attempts to alter the nonces or inject their own session parameters, the SAS calculation diverges, alerting the user immediately.

### 5.2 What SAS Does NOT Prove
- **SAS is NOT an independent identity proof**: The SAS is derived from $T_{\text{pair}}$, not from a pre-existing cryptographic identity of the phone.
- **Actual Security Anchors**:
  1. The **TLS certificate fingerprint** pinned from the QR code provides authenticated server identity and transport encryption.
  2. The **one-time secret $T_{\text{pair}}$** delivered optically via the camera provides proof of physical proximity.
  3. The **desktop user approval gate** provides explicit human authorization.
  4. The **SAS code** is a human visual cross-check ensuring no transcript desynchronization occurred.

---

## 6. Session Credential Contract & Android Storage

### 6.1 Directional Session Keys
Upon approval, `derive_session_keys()` uses HKDF-Expand with distinct domain separation:
- **Upstream Key ($K_{\text{up}}$, 32 bytes)**: Used by Phone $\rightarrow$ PC requests to compute HMAC-SHA256 request signatures over canonical request lines.
- **Downstream Key ($K_{\text{down}}$, 32 bytes)**: Used by PC $\rightarrow$ Phone responses to authenticate server responses.
- **Bearer Token ($\text{Token}_{\text{session}}$, string)**: Passed in `Authorization: Bearer sess_tok_...` for session lookup.

### 6.2 Authenticated Request Fields for Future Transfers (Stage 13.3.2)
Every future transfer request will authenticate:
```
Authorization: Bearer sess_tok_<token>
X-Kivo-Sequence: <monotonic_integer>
X-Kivo-Timestamp: <epoch_float>
X-Kivo-Signature: <hex_hmac_sha256>
```
Where $\text{Signature} = \text{HMAC-SHA256}(K_{\text{up}}, \text{Method} \parallel \text{Path} \parallel \text{Sequence} \parallel \text{Timestamp} \parallel \text{SHA256}(\text{Body}))$.

### 6.3 Android Keystore Storage Specification
On Android, arbitrary raw keys cannot be directly imported as non-exportable Keystore entries on older API levels without hardware HSM support. Therefore, the Android companion will use **EncryptedSharedPreferences** backed by an Android Keystore master key:
- The Android Keystore generates an AES-256-GCM master key in hardware (`AndroidKeyStore`).
- The session credentials ($K_{\text{up}}, K_{\text{down}}, \text{Token}_{\text{session}}$) are encrypted with the master key and stored in app-private storage.
- When pairing is revoked or expires, the shared preferences file is securely wiped.

---

## 7. TLS Fingerprint Format Specification

To ensure 100% interoperability with the Stage 13.3.2 / 13.3.4 Android companion app:
- **Hash Algorithm**: `SHA-256`.
- **Input Bytes**: Complete DER-encoded X.509 leaf certificate (`cert.public_bytes(serialization.Encoding.DER)`).
- **Format**: Exactly 64 uppercase hexadecimal characters (`[0-9A-F]{64}`).
- **Android Validation in Kotlin**:
  ```kotlin
  val serverCert = chain[0] as X509Certificate
  val derBytes = serverCert.encoded
  val digest = MessageDigest.getInstance("SHA-256").digest(derBytes)
  val fpHex = digest.joinToString("") { "%02X".format(it) }
  if (fpHex != expectedFingerprint) {
      throw CertificateException("Pinned certificate fingerprint mismatch")
  }
  ```
- **SAN Validation**: Certificate SAN strictly includes `IPAddress(192.168.1.7)` and `DNSName("kivo-bridge.local")`.
- **TLS Version**: Enforces `TLS 1.3` with `TLS_AES_256_GCM_SHA384` and `TLS_CHACHA20_POLY1305_SHA256`.

---

## 8. Adversarial Test Results (All 14 Required Tests Passed)

| # | Test Case | Target Behavior | Result |
|---|---|---|---|
| 1 | `test_invalid_proof_does_not_consume_token` | Invalid proof rejected; subsequent valid proof succeeds on same token | **PASS** |
| 2 | `test_unsupported_version_does_not_consume_token` | Version 999 rejected; token remains available | **PASS** |
| 3 | `test_invalid_session_id_does_not_consume_token` | Wrong session ID rejected; active offer remains valid | **PASS** |
| 4 | `test_expired_token_cannot_be_used` | Expired offer rejected with `offer_expired` | **PASS** |
| 5 | `test_valid_proof_consumes_token_exactly_once` | Valid proof consumes token; second attempt rejected | **PASS** |
| 6 | `test_replayed_valid_proof_rejected` | Replay of valid handshake rejected with `token_already_consumed` | **PASS** |
| 7 | `test_concurrent_handshakes_produce_exactly_one_candidate` | Two concurrent valid attempts under threads: exactly 1 succeeds | **PASS** |
| 8 | `test_handshake_rate_limiting` | 5 failed attempts allowed; 6th rejected with `rate_limited` | **PASS** |
| 9 | `test_rate_limit_state_remains_bounded` | 150 IP floods capped at max 100 entries in memory | **PASS** |
| 10 | `test_desktop_rejection_permanently_consumes_token` | Rejection permanently burns token; cannot be reused | **PASS** |
| 11 | `test_pairing_reset_invalidates_token` | Stopping bridge clears offer and burns token | **PASS** |
| 12 | `test_sas_derivation_deterministic` | Client and server derive identical SAS for identical context | **PASS** |
| 13 | `test_different_contexts_produce_different_sas` | Altering context bytes produces distinct SAS | **PASS** |
| 14 | `test_fingerprint_deterministic_and_uppercase_hex` | SHA-256 fingerprint is exactly 64 uppercase hex chars | **PASS** |

---

## 9. Verification Gate Results

| Gate | Command | Result |
|---|---|---|
| Focused Bridge Tests | `pytest tests/test_bridge_*.py -v` | **35 passed** in 3.74s |
| Security Regression Tests | `pytest tests/test_stage12_2_regression.py -v` | **10 passed** in 0.67s |
| Combined Stage 12 + 13 Focused Suite | `pytest tests/test_telemetry_*.py tests/test_microphone_*.py tests/test_clipboard_*.py tests/test_shelf_*.py tests/test_bridge_*.py tests/test_stage12_2_regression.py -q` | **257 passed** in 30.7s |
| Full Backend Test Suite | `pytest tests/ -q` | **878 passed, 176 subtests passed** in 68.0s (0 failures) |
| Shell Test Suite | `pytest shell/tests/ -q` | **16 passed** in 15.3s |
| Qt6 QML Lint | `qmllint shell/**/*.qml` | **Exit 0** (zero errors) |
| Desktop Health Check | `./scripts/desktop-health.sh` | **9/9 PASS** |
| Git Whitespace Check | `git diff --check` | **Exit 0** |
| Python Compilation | `py_compile` all modified files | **ALL COMPILED OK** |
| Live Environment State | Host checks | **Untouched** (no open bridge sockets, mic unmuted) |

---

## 10. Strict Boundaries Maintained

- ✅ **Desktop backend hardening only**: Mobile Android companion and actual file transfer implementation remain scheduled for Stage 13.3.2 / 13.3.4.
- ✅ **No QML UI changes**.
- ✅ **No Agent Tools registered**.
- ✅ **No physical network socket opened during testing**.
- ✅ **Zero new dependencies**.
- ✅ **No git commit or push**.

---

**Verdict: PASS**
