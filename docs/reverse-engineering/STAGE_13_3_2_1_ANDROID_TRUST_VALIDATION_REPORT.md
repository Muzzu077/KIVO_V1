# KIVO — Stage 13.3.2.1: Android TLS, Keystore & Pairing Integration Validation Report

```
Document:   Stage 13.3.2.1 Android Trust & Integration Validation Report
Status:     Complete
Date:       2026-11-09
Baseline:   HEAD 1062c0d; Stage 12.2 and Stages 13.1–13.3.2 completed
Scope:      Adversarial TLS certificate pinning validation, Android KeyStore AES-GCM envelope
            storage validation, secure approval completion contract, loopback integration
```

---

## 1. Executive Summary

Stage 13.3.2.1 executes an adversarial security and integration validation of the **KIVO Android Companion** (`android-companion/`) and desktop pairing server (`kivo/bridge/`).

This validation proves:
1. **Adversarial TLS 1.3 Validation**: The client's `PinnedCertificateTrustManager` strictly verifies the SHA-256 leaf certificate fingerprint, enforces SAN matching of the LAN IP, enforces validity periods, verifies key usage, and rejects MITM/downgrade attacks.
2. **Native Android KeyStore AES-256-GCM Verification**: Proves that session envelopes with Authenticated Additional Data (`kivo-envelope-v1:<sessionId>`) detect and reject tampered ciphertexts, modified IVs, altered session IDs, truncated envelopes, and wrong keys.
3. **Secure Approval Completion Contract**: Resolves the long-polling interruption gap by introducing `POST /api/v1/pair/status` authenticated with domain-separated token `HMAC-SHA256(T_pair, b"KIVO_BRIDGE_POLL_AUTH_V1\0" + context)` without leaking credentials to a public session ID or reusing the consumed pairing secret.
4. **End-to-End Loopback Pairing Execution**: Validates complete handshake, SAS derivation (`482 910`), desktop approval, session token issuance, and authenticated `GET /api/v1/ping` over TLS on loopback.

---

## 2. Adversarial TLS Certificate Pinning Validation (Phase 1)

`PinnedCertificateTrustManager` and `PinnedHostnameVerifier` were tested across 9 adversarial scenarios in `android-companion/app/src/test/java/com/kivo/companion/TlsPinnerTest.kt`:

| Test Vector | Simulated Attack / Condition | Expected Behavior | Result |
|---|---|---|---|
| **Valid Leaf Cert** | P-256 ECDSA, SAN = `192.168.1.7`, valid fingerprint | Handshake proceeds cleanly | **PASS** |
| **Wrong Fingerprint** | Attacker presents certificate with differing SHA-256 hash | Rejects with `TlsPinningException` ("fingerprint mismatch") | **PASS** |
| **Expired Certificate** | Desktop certificate past `not_after` date | Rejects with `TlsPinningException` ("expired") | **PASS** |
| **Future Certificate** | Desktop certificate before `not_before` date | Rejects with `TlsPinningException` ("not yet valid") | **PASS** |
| **SAN IP Mismatch** | Certificate has `IPAddress: 192.168.1.99`, but client connects to `192.168.1.7` | Rejects with `TlsPinningException` ("SAN does not match IP") | **PASS** |
| **Missing SAN** | Certificate lacks `SubjectAlternativeName` extension | Rejects with `TlsPinningException` ("SAN does not match IP") | **PASS** |
| **Empty Chain** | Server sends 0 certificates in TLS handshake | Rejects with `TlsPinningException` ("Empty server certificate chain") | **PASS** |
| **Excessive Chain Length** | Attacker injects intermediate CA certificates ($> 2$) | Rejects with `TlsPinningException` ("Unexpected chain length") | **PASS** |
| **Hostname / IP Verifier** | Verifier receives mismatched IP or public hostname (`evil.com`) | Returns `false`; connection aborted | **PASS** |

### Cryptographic Boundary
- The fingerprint strictly pins the **complete DER-encoded leaf certificate** (`SHA-256(cert.encoded)`), formatted as 64 uppercase hex characters.
- TLS 1.3 is strictly enforced via `Tls13SocketFactory.patch()` (`socket.enabledProtocols = arrayOf("TLSv1.3")`).

---

## 3. Native Android KeyStore Validation (Phase 2)

`KeystoreSecretStorage` was validated across 9 adversarial cases in `android-companion/app/src/test/java/com/kivo/companion/KeystoreStorageTest.kt`:

| Test Vector | Condition / Manipulation | Security Behavior Verified | Result |
|---|---|---|---|
| **Round Trip** | Save valid credentials $\rightarrow$ Load credentials | Exact match of `sessionId`, `bearerToken`, `upstreamKey`, `downstreamKey`, `expiresAt` | **PASS** |
| **Wrong Key** | Attempting decryption with different AES-256 key | `Cipher.doFinal()` fails with `AEADBadTagException`; returns `null` | **PASS** |
| **Tampered Ciphertext** | 1 bit flipped in ciphertext / GCM auth tag | Tag mismatch fails decryption; returns `null` | **PASS** |
| **Tampered IV** | 1 bit flipped in 12-byte IV header | AEAD authentication fails; returns `null` | **PASS** |
| **Tampered Session ID in AAD** | Header `sessionId` altered (`sess_original` $\rightarrow$ `xess_original`) | AES-GCM tag verification fails because AAD does not match; returns `null` | **PASS** |
| **Truncated Envelope** | File truncated to fewer bytes than header + tag | Envelope length check fails safely; returns `null` | **PASS** |
| **Invalid Envelope Version** | Version byte set to `0x99` instead of `0x01` | Header validation fails; returns `null` | **PASS** |
| **Expired Session Clean** | Saved credentials have `expiresAt < now` | Detected as expired; automatically wipes file from disk; returns `null` | **PASS** |
| **Revocation Cleanup** | `storage.clear()` called | Storage file deleted immediately; subsequent reads return `null` | **PASS** |

---

## 4. Approval-Polling Contract & Completion Specification (Phase 3)

### 4.1 The Long-Polling Reconnection Challenge
In Stage 13.3.1, `POST /api/v1/pair/handshake` holds the HTTP connection open for up to 60 seconds awaiting desktop user approval. If the mobile Wi-Fi interface entered low-power sleep or transiently dropped at second 25, the long-poll HTTP connection terminated with an `IOException`. Because the one-time pairing secret $T_{\text{pair}}$ was consumed atomically, re-sending the handshake would fail with `409 Conflict` (`token_already_consumed`).

### 4.2 The Solution: Authenticated Status Poll
We implemented a dedicated endpoint: `POST /api/v1/pair/status` in `kivo/bridge/server.py`.

```text
POST /api/v1/pair/status
Headers:
  Host: 192.168.1.7:8443
  Content-Type: application/json
Body:
  {
    "session_id": "sess_0123456789abcdef",
    "poll_auth_b64": "<base64url_hmac>"
  }
```

#### Authentication & Domain Separation
$$\text{Auth}_{\text{poll}} = \text{HMAC-SHA256}(T_{\text{pair}}, \text{b"KIVO\_BRIDGE\_POLL\_AUTH\_V1\0"} \parallel \text{Context}_{\text{pairing}})$$

1. **Immune to Public Session ID Snooping**: An attacker on the LAN who discovers `session_id` cannot query this endpoint because they do not have $T_{\text{pair}}$ or the client nonce.
2. **No Reusable Credential Misuse**: It does not reuse $T_{\text{pair}}$ as a bearer token; it derives a domain-separated token.
3. **Response Schema**:
   - While waiting on desktop approval:
     `{"ok": true, "status": "pending_approval", "session_id": "...", "sas_code": "482 910"}`
   - Once approved:
     `{"ok": true, "status": "approved", "session_token": "sess_tok_...", "expires_in": 7200, "device_id": "..."}`
   - If rejected by desktop:
     `403 Forbidden` (`{"detail": "rejected_by_desktop"}`)
   - If approval timed out:
     `410 Gone` (`{"detail": "approval_timed_out"}`)
   - If invalid poll auth:
     `401 Unauthorized` (`{"detail": "invalid_poll_auth"}`)

---

## 5. End-to-End Loopback Integration Results (Phase 4)

Three comprehensive integration tests executed against live ephemeral Uvicorn servers on loopback in `tests/test_bridge_integration.py`:

1. **`test_end_to_end_handshake_and_approval_flow`**:
   - Pinned TLS 1.3 handshake established on `127.0.0.1:18443`.
   - Handshake submitted $\rightarrow$ desktop callback triggered with matching SAS (`779 684`).
   - Desktop calls `approve_pairing()`.
   - Client receives 200 OK with `session_token`.
   - Client issues `GET /api/v1/ping` with `Authorization: Bearer <token>` $\rightarrow$ returns 200 OK (`pong=true`).
2. **`test_end_to_end_handshake_rejection_flow`**:
   - Client submits handshake $\rightarrow$ desktop user clicks Reject.
   - Server returns 403 Forbidden.
   - Subsequent replay of identical handshake is rejected with 409 Conflict (`token_already_consumed`).
3. **`test_poll_status_endpoint`**:
   - Handshake initiated in background.
   - Client calls `POST /api/v1/pair/status` with `poll_auth` $\rightarrow$ receives `pending_approval`.
   - Unauthenticated status check with invalid poll auth returns 401 Unauthorized.
   - Desktop user approves $\rightarrow$ poll status returns `approved` with bearer token.

---

## 6. Verification Gate Results

| # | Verification Gate | Execution Command | Result |
|---|---|---|---|
| 1 | Android Unit Tests | `cd android-companion && ./gradlew test` | **32 passed in 1s** (0 failures) |
| 2 | Android Lint | `cd android-companion && ./gradlew lintDebug` | **BUILD SUCCESSFUL in 3s** (0 errors) |
| 3 | Assemble Debug APK | `cd android-companion && ./gradlew assembleDebug` | **BUILD SUCCESSFUL in 31s** (25 MB APK) |
| 4 | Focused Bridge Python Tests | `.venv/bin/python -m pytest tests/test_bridge_*.py -v` | **38 passed in 5.61s** |
| 5 | Security Regression Tests | `.venv/bin/python -m pytest tests/test_stage12_2_regression.py -v` | **10 passed in 0.67s** |
| 6 | Combined Stage 12 + 13 Focused Suite | `.venv/bin/python -m pytest tests/test_telemetry_*.py tests/test_microphone_*.py tests/test_clipboard_*.py tests/test_shelf_*.py tests/test_bridge_*.py tests/test_stage12_2_regression.py -q` | **271 passed in 32.5s** |
| 7 | Full Backend Test Suite | `.venv/bin/python -m pytest tests/ -q` | **892 passed, 176 subtests passed** in 69.7s (0 failures) |
| 8 | Shell Test Suite | `.venv/bin/python -m pytest shell/tests/ -q` | **16 passed in 15.1s** |
| 9 | Qt6 QML Lint | `/usr/lib/qt6/bin/qmllint shell/**/*.qml` | **Exit 0** (0 errors) |
| 10 | Desktop Health Check | `./scripts/desktop-health.sh` | **9/9 PASS** |
| 11 | Git Whitespace Check | `git diff --check` | **Exit 0** |

---

## 7. Strict Boundaries Maintained

- ✅ **No file upload or download implemented**: Streaming transfer protocols remain scheduled for Stage 13.3.3.
- ✅ **No QML UI modifications**.
- ✅ **No Agent tools registered**.
- ✅ **No real phone paired without explicit authorization**.
- ✅ **Zero new Python dependencies**.
- ✅ **No git commit or push**.

---

**Verdict: PASS**
