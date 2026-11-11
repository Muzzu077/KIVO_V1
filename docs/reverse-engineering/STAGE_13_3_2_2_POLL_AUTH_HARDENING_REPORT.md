# KIVO — Stage 13.3.2.2: Pairing Status Authentication & Credential Lifecycle Hardening Report

**Date:** 2026-11-09  
**Status:** COMPLETE  
**Verdict:** **PASS** (Ready for Stage 13.3.3 File Transfer & Storage Architecture)  
**Reference Document:** `docs/reverse-engineering/STAGE_13_3_2_1_ANDROID_TRUST_VALIDATION_REPORT.md`

---

## 1. Executive Summary

Stage 13.3.2.2 executed a focused adversarial security audit and cryptographic hardening of the Phone-to-PC Bridge pairing and approval-polling protocol. 

In Stage 13.3.2.1, status polling (`POST /api/v1/pair/status`) relied on a static domain-separated token (`poll_auth_b64 = HMAC-SHA256(T_pair, ...)`). While cryptographically unforgeable without the pairing secret $T_{\text{pair}}$, this static token was reusable throughout the session, lacked per-request replay protection, retained $T_{\text{pair}}$ in desktop memory throughout the pending user-confirmation period, and repeatedly returned the session bearer token upon desktop approval without response authentication.

In Stage 13.3.2.2, the pairing and polling protocol has been hardened with:
1. **Dedicated Ephemeral Polling Key ($K_{\text{poll}}$):** Derived via HKDF-SHA256 immediately upon handshake proof verification, enabling the immediate destruction of the root pairing secret $T_{\text{pair}}$ from memory.
2. **Fresh Per-Request Authentication:** Every poll request requires a strictly monotonic sequence number ($S_i > S_{i-1}$), a unique 16-byte random request nonce ($N_i$), a fresh UTC timestamp ($|t - t_{\text{now}}| \le 60\text{s}$), and an HMAC-SHA256 signature over an unambiguous canonical binary payload (`CanonicalPollRequest`).
3. **Mutual Response Authentication:** Desktop approval responses are signed with $K_{\text{poll}}$ bound to the request's canonical transcript (`response_sig`). The Android client cryptographically verifies this signature before storing session credentials.
4. **Bounded Credential Recovery Window:** Following desktop approval, status polling is restricted to a 30-second recovery window (`APPROVAL_RECOVERY_WINDOW = 30.0s`). Once the window closes or the session is revoked, $K_{\text{poll}}$ is purged permanently, returning HTTP 410 Gone.
5. **Availability Hardening (DoS Mitigation):** Brute-force protection was restructured to distinguish per-IP lockouts (`MAX_FAILED_PROOFS_PER_IP = 5`, 5-minute lockout) from global offer invalidation (`MAX_TOTAL_FAILED_PROOFS_PER_OFFER = 25`). Malicious LAN clients can no longer invalidate legitimate pairing offers by injecting 5 bad proofs.
6. **Cross-Platform Interoperability:** Shared protocol vectors updated in `tests/fixtures/pairing_test_vectors.json` and verified byte-for-byte across Python (`tests/test_bridge_crypto.py`) and Kotlin (`CryptoInteroperabilityTest.kt`).

All 5 verification gates (full Python backend suite, shell tests, QML lint, desktop health, and Android Gradle test/lint/build) have passed with zero regressions.

---

## 2. Phase 1 & 2: Audit of Original Poll Authentication

### 2.1 Original Protocol Contract (Stage 13.3.2.1)
- Endpoint: `POST /api/v1/pair/status`
- Request:
  ```json
  {
    "session_id": "sess_0123456789abcdef",
    "poll_auth_b64": "llnJiqC3nXeUBYSlmFpKQm3d59juCTuSJ9mehMjKReQ"
  }
  ```
- Authentication:
  $$\text{poll\_auth} = \text{HMAC-SHA256}(T_{\text{pair}}, \text{"KIVO\_BRIDGE\_POLL\_AUTH\_V1\x00"} \mathbin{\Vert} \text{canonical\_context})$$

### 2.2 Findings & Adversarial Deficiencies
1. **Replay Vulnerability (Static Polling Credential):** `poll_auth` was completely invariant across all poll requests in a pairing session. Any entity that observed or retrieved `poll_auth` could submit arbitrary poll requests.
2. **Repeated Credential Disclosure:** Following desktop approval, calling `/api/v1/pair/status` with `poll_auth` repeatedly returned the plaintext session bearer token without limit.
3. **Prolonged $T_{\text{pair}}$ Retention:** In `kivo/bridge/pairing.py`, $T_{\text{pair}}$ remained stored in `self._current_offer` until `approve_pairing` was called on the desktop (up to 60 seconds later), because `derive_session_keys` was called inside `approve_pairing`.
4. **Unauthenticated Server Responses:** The server's approval response had no cryptographic signature. A malicious MITM or local proxy could spoof approval payloads.
5. **Single-Source Denial of Service:** Submitting 5 invalid proofs from any LAN IP globally invalidated `self._current_offer`. An unauthenticated attacker observing QR code sessions on the LAN could immediately DoS the pairing process with 5 rapid bad requests.

---

## 3. Phase 3 & 4: Fresh Poll Authentication & Response Signing

### 3.1 Polling Key Derivation ($K_{\text{poll}}$)
Upon receiving a valid handshake proof from the client, the server derives $K_{\text{poll}}$ via HKDF-SHA256:
$$PRK = \text{HKDF-Extract}(\text{salt}=\text{client\_nonce}, \text{IKM}=T_{\text{pair}})$$
$$K_{\text{poll}} = \text{HKDF-Expand}(PRK, \text{info}=\text{"kivo-pair-poll-v1\x00"} \mathbin{\Vert} \text{canonical\_context}, \text{length}=32)$$

Both Python and Android companion derive identical 32-byte keys $K_{\text{poll}}$.

### 3.2 Canonical Poll Request Framing
To prevent delimiter collision and ambiguity, requests are encoded in canonical length-prefixed binary:
$$\text{CanonicalPollRequest} = \text{\_pack}(\text{version}) \mathbin{\Vert} \text{\_pack}(\text{session\_id}) \mathbin{\Vert} \text{\_pack}(\text{"POST"}) \mathbin{\Vert} \text{\_pack}(\text{"/api/v1/pair/status"}) \mathbin{\Vert} \text{pack}(>Q, \text{sequence}) \mathbin{\Vert} \text{\_pack}(\text{request\_nonce}) \mathbin{\Vert} \text{pack}(>d, \text{timestamp})$$
where $\text{\_pack}(b) = \text{pack}(>I, \text{len}(b)) \mathbin{\Vert} b$.

### 3.3 Request Authentication & Replay Prevention
Client computes:
$$\text{Signature} = \text{HMAC-SHA256}(K_{\text{poll}}, \text{"KIVO\_BRIDGE\_POLL\_REQUEST\_V1\x00"} \mathbin{\Vert} \text{CanonicalPollRequest})$$

Server verifies:
1. **Freshness Window:** $|t_{\text{server}} - \text{timestamp}| \le 60.0\text{s}$ (otherwise HTTP 401 `timestamp_out_of_bounds`).
2. **Monotonic Sequence:** $\text{sequence} > \text{last\_seen\_sequence}$ (otherwise HTTP 409 `replay_sequence_invalid`).
3. **Nonce Deduplication:** $\text{request\_nonce} \notin \text{seen\_nonces}$ (otherwise HTTP 409 `replay_nonce_detected`).
4. **Constant-Time Verification:** `hmac.compare_digest(expected, signature)` (otherwise HTTP 401 `invalid_signature`).

### 3.4 Approval Response Authentication
When the desktop user approves the pairing, the server generates a cryptographic proof of approval bound to the client's request:
$$\text{response\_sig} = \text{HMAC-SHA256}(K_{\text{poll}}, \text{"KIVO\_BRIDGE\_POLL\_RESPONSE\_V1\x00"} \mathbin{\Vert} \text{CanonicalPollRequest} \mathbin{\Vert} \text{\_pack}(\text{"approved"}))$$

The Android client verifies `response_sig` against its local $K_{\text{poll}}$ before persisting credentials into encrypted storage.

---

## 4. Phase 5: Credential Lifecycle State Transitions

```
[Offer Created]
       │  (T_pair in memory, TTL 300s)
       ▼
[Client Handshake Proof Verified]
       ├─► Derive K_poll, K_up, K_down, Bearer Token
       ├─► Store keys in _poll_keys & _cached_session_keys
       ├─► ERASE T_pair from server state (_current_offer = None)
       └─► Transition state -> CONFIRMING
       │
       ▼
[Status Polling Window]
       ├─► Fresh per-request HMAC with K_poll
       ├─► Strict sequence S_i > S_{i-1} & Nonce deduplication
       │
  ┌────┴───────────────────────────┐
  │ User Rejects                   │ User Approves
  ▼                                ▼
[REJECTED State]                 [PAIRED State]
  ├─► Erase K_poll                 ├─► Activate session
  ├─► Erase session keys           ├─► Start 30s recovery window
  └─► HTTP 403 Forbidden           ├─► Return signed approval (response_sig)
                                   └─► Purge K_poll on window close (HTTP 410)
```

### Destruction & Retention Rules:
- $T_{\text{pair}}$ is destroyed immediately upon successful handshake verification (step 10 of `handle_client_handshake`).
- $K_{\text{poll}}$ is retained during `CONFIRMING` state and for at most `APPROVAL_RECOVERY_WINDOW` (30 seconds) in `PAIRED` state.
- Upon revocation or rejection, all temporary keys ($K_{\text{poll}}$, cached session keys) are popped from dictionaries immediately.
- *Memory Limitation Note:* Python's garbage collector frees unreferenced byte objects, but immutable Python byte strings cannot be zeroized with guaranteed C-level memory wiping. Narrowing the window of retention is the strongest mechanism available.

---

## 5. Phase 6: Rate Limiting & Denial of Service Analysis

### 5.1 Vulnerability in Previous 5-Proof Global Threshold
In Stage 13.3.2.1, 5 bad handshake proofs globally expired the pairing offer. An attacker on the local Wi-Fi subnet could easily sniff or guess session IDs and submit 5 invalid proofs, locking out legitimate users.

### 5.2 Hardened Multi-Tier Policy
| Limit Mechanism | Scope | Threshold | Window / Action | Objective |
| :--- | :--- | :--- | :--- | :--- |
| **IP Lockout** | Per client IP | 5 bad proofs | 300s (5 min) lockout (`ip_locked_out`) | Isolates attacker IP; preserves offer for legitimate phone |
| **Global Offer Ceiling** | Across all IPs | 25 bad proofs | Invalidate offer (`too_many_failed_attempts`) | Protects against distributed brute-force attacks |
| **Request Rate Limit** | Per client IP | 5 attempts | 60s sliding window (`rate_limited`) | Blocks high-frequency request flooding |
| **Memory Bounds** | Internal maps | Capped sizes | Capped at 100 IPs / 50 nonces / 100 consumed IDs | Prevents state-exhaustion memory exhaustion DoS |

*Availability Guarantee:* A single attacker IP cannot deny service to a legitimate pairing phone. Distributed DoS (e.g. 25 distinct IP addresses or physical network flooding) remains an unavoidable LAN threat model constraint.

---

## 6. Phase 7: Android Keystore Evidence Audit

A rigorous inspection of `android-companion/` test suites was conducted to verify what is and is not exercised:

### 6.1 Audit Findings
- **Environment:** JVM unit test suite (`com.kivo.companion.KeystoreStorageTest`).
- **Cryptographic Provider:** SunJCE / Java Cryptography Architecture (JCA) AES-GCM engine in JVM test scope.
- **Hardware Backing:** **NOT EXERCISED.** There is no physical device or Android emulator attached (`adb devices` is empty). The Android StrongBox / TEE hardware security module is **not** exercised during JVM unit tests.

### 6.2 Validated Capabilities in JVM Tests
1. AES-256-GCM envelope serialization and deserialization (Format: Version byte `0x01` + Salt `32B` + IV `12B` + Ciphertext + Tag `16B`).
2. Associated Authenticated Data (AAD) tamper rejection: Changing `session_id` in AAD causes GCM tag verification failure.
3. IV bit flip rejection: Tampering with IV bytes fails AEAD decryption.
4. Ciphertext corruption rejection: Bit flips in encrypted bearer token fail AEAD authentication.
5. Key rotation / Wrong key handling: Decrypting with an alternate key fails safely without leaking plaintext.
6. Safe file erasure on `clear()`.
7. Automatic purging of expired sessions on load.

### 6.3 Outstanding Real-Device Validation Requirement
Stage 13.3.4 (Hardware Integration) must run on physical Android hardware or an Android Emulator with API 34+ to validate:
- Android KeyStore KeyGenParameterSpec TEE key generation.
- User authentication binding (`setUserAuthenticationRequired`).
- Hardware-backed key persistence across process restart and OS reboot.

---

## 7. Phase 8: Cross-Platform Protocol Fixtures

Deterministic test vectors in `tests/fixtures/pairing_test_vectors.json` were expanded and verified across Python and Kotlin:

```json
{
  "pairing_secret_hex": "0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20",
  "client_nonce_hex": "c0c1c2c3c4c5c6c7c8c9cacbcccdcecfd0d1d2d3d4d5d6d7d8d9dadbdcdddedf",
  "session_id": "sess_0123456789abcdef",
  "k_poll_hex": "a91e7c826c62d2c9a5b42dbefef6a2de0bf8b8fb83965160703d0fe2f79a9e7b",
  "poll_req_sequence": 1,
  "poll_req_nonce_hex": "101112131415161718191a1b1c1d1e1f",
  "poll_req_timestamp": 1700000000.0,
  "canonical_poll_req_hex": "000000013100000015736573735f3031323334353637383961626364656600000004504f5354000000132f6170692f76312f706169722f737461747573000000000000000100000010101112131415161718191a1b1c1d1e1f41d954fc40000000",
  "poll_req_sig_hex": "4640d07c4596f328b626bd655547ca39918ddcaca237a107248ccd74c800f9bd",
  "poll_resp_sig_hex": "0d14f794caee271a5fa845fa80c5207ed90f739b61b5462f0f4c7992a4817064"
}
```

### Verification Results:
- `tests/test_bridge_crypto.py`: `test_poll_key_and_signature_interoperability` **PASSED**.
- `android-companion/CryptoInteroperabilityTest.kt`: `testPollKeyDerivationMatchesPythonByteForByte` and `testCanonicalPollRequestAndSignaturesMatchPythonByteForByte` **PASSED**.

---

## 8. Phase 9: Adversarial Test Coverage

Integration and unit tests verify adversarial defenses under simulated local attack scenarios:

| Adversarial Attack / Scenario | Test Method | Outcome |
| :--- | :--- | :--- |
| **Replay of Poll Sequence** | `test_fresh_authenticated_poll_status_flow` (Integration) | Rejected with HTTP 409 `replay_sequence_invalid` |
| **Replay of Request Nonce** | `test_fresh_authenticated_poll_status_flow` (Integration) | Rejected with HTTP 409 `replay_nonce_detected` |
| **Out-of-Window Timestamp** | `check_authenticated_poll_status` (Unit) | Rejected with `timestamp_out_of_bounds` |
| **Tampered Request Signature** | `test_poll_key_and_signature_interoperability` (Crypto) | Rejected with `invalid_signature` |
| **Tampered Approval Status** | `test_poll_key_and_signature_interoperability` (Crypto) | Rejected by client `verify_approval_response_signature` |
| **Single-IP Proof Brute Force** | `test_failed_proofs_per_ip_lockout_preserves_offer_for_legitimate_client` (Pairing) | Offending IP locked out (5 min); legitimate IP completes pairing successfully |
| **Distributed Proof Brute Force** | `test_total_failed_proofs_across_ips_locks_offer` (Pairing) | 25 distributed attempts expires offer (`too_many_failed_attempts`) |
| **Polling Window Expiry** | `check_authenticated_poll_status` (Pairing) | 30s after approval, $K_{\text{poll}}$ purged; returns `polling_window_closed` |
| **Desktop Rejection** | `test_end_to_end_handshake_rejection_flow` (Integration) | State transitions to `REJECTED`; returns HTTP 403 `rejected_by_desktop` |

---

## 9. Phase 10: Verification Gates Summary

| Gate | Check | Command | Result |
| :---: | :--- | :--- | :---: |
| **1** | Bridge Test Suite | `.venv/bin/python -m pytest tests/test_bridge_*.py -v` | **41/41 PASSED** (6.27s) |
| **2** | Android Kotlin Unit Tests | `cd android-companion && ./gradlew test` | **34/34 PASSED** (607ms) |
| **3** | Android Lint & Debug APK | `cd android-companion && ./gradlew lintDebug assembleDebug` | **BUILD SUCCESSFUL** (5s) |
| **4** | Full Python Backend Suite | `.venv/bin/python -m pytest tests/ -q` | **895 PASSED**, 176 subtests (69.60s) |
| **5** | Shell Test Suite | `.venv/bin/python -m pytest shell/tests/ -q` | **16/16 PASSED** (15.18s) |
| **6** | Qt6 QML Lint | `/usr/lib/qt6/bin/qmllint $(find shell/ -name "*.qml")` | **Exit 0** (zero errors) |
| **7** | Desktop Health Diagnostic | `./scripts/desktop-health.sh` | **9/9 PASS** |
| **8** | Whitespace & Syntax | `git diff --check` | **PASS** (zero issues) |

---

## 10. Remaining Limitations & Boundaries

1. **Python Memory Erasure:** Python `bytes` objects are immutable and handled by runtime garbage collection; memory cannot be zeroed with cryptographic certainty. Unreferencing immediately after derivation is best-effort.
2. **Android Keystore Hardware Testing:** Hardware-backed Keystore / StrongBox tests require real device hardware and will be formally verified in Stage 13.3.4.
3. **No Out-of-Scope Implementations:** In strict accordance with instructions, zero file transfers, quarantine mechanisms, QML UI changes, agent tools, physical LAN listeners, or git commits/pushes were performed.

---

## 11. Final Verdict

# **PASS**

The pairing status authentication and credential delivery protocol has been hardened against replay attacks, single-source LAN DoS, and premature credential leakage. The repository is fully verified and prepared for **Stage 13.3.3: Encrypted Chunked File Transfer & Staging Architecture**.
