# KIVO — Stage 13.3.2.3: Approval Response Integrity & Credential Delivery Audit

**Date:** 2026-11-10  
**Status:** COMPLETE  
**Verdict:** **PASS WITH LIMITATIONS** (response-authentication contract complete; limitations are validation/integration items)  
**Reference Baseline:** `docs/reverse-engineering/STAGE_13_3_2_2_POLL_AUTH_HARDENING_REPORT.md`

---

## 1. Executive Summary

Stage 13.3.2.2 introduced fresh per-request polling authentication, but the server's approval response was still signed over only two things: the canonical poll request and the literal status string `"approved"`.

```text
response_sig = HMAC-SHA256(K_poll,
    "KIVO_BRIDGE_POLL_RESPONSE_V1\x00" || canonical_poll_request || pack("approved"))
```

Everything the Android client actually consumed — the returned bearer token, expiry, device ID, HTTP status, and credential-delivery state — was **outside** the MAC. The legacy static `poll_auth_b64` path also remained live, allowing reusable credential retrieval.

Stage 13.3.2.3 replaces that contract with a full-payload response transcript:

```text
response_sig = HMAC-SHA256(K_poll,
    "KIVO_BRIDGE_POLL_RESPONSE_V2\x00" || CanonicalPollResponse)
```

`CanonicalPollResponse` is an unambiguous, length-prefixed binary encoding of protocol version, response context, session ID, HTTP status, request sequence, request nonce, approval state, expiry, device ID, credential-delivery state, and SHA-256 of the returned credential. The legacy static polling credential has been removed entirely.

The same V2 contract signs both poll responses and the blocking handshake response (handshake uses `response_context = "handshake"`, `sequence = 0`, and the verified client nonce so it cannot be confused with a poll response).

All verification gates pass. The two recorded limitations are: no physical-device/emulator hardware-backed Keystore validation, and the Android foreground pairing flow does not yet automatically fall back to signed polling if the handshake response is lost in transit (the signed polling transport and verifier exist, but UI orchestration is not wired). Neither limitation invalidates the response-authentication contract.

---

## 2. Original Contract and Identified Gaps

### 2.1 Original Response-Signing Contract (Stage 13.3.2.2)

- Poll response body:
  ```json
  {
    "ok": true,
    "status": "approved",
    "session_id": "sess_...",
    "session_token": "sess_tok_...",
    "expires_in": 7200,
    "device_id": "client_...",
    "response_sig": "<32-byte hex>"
  }
  ```
- Signature input: canonical poll request + `"approved"`.
- The client verified the MAC and then trusted the token, expiry, and device ID from the same body.

### 2.2 Identified Gaps

| # | Gap | Consequence |
| :--- | :--- | :--- |
| 1 | Token not covered by the MAC | The token field could be substituted in the response body without invalidating the MAC, so a client that trusted the body could persist an attacker-chosen credential. |
| 2 | Expiry and device ID not covered | Client could accept a forged lifetime or a response intended for another device. |
| 3 | HTTP status / credential-delivery state not covered | A pending response and a credential-bearing response were not cryptographically distinguished by state. |
| 4 | `response_context` absent | Handshake and poll responses used different transports but shared no cryptographic domain separation. |
| 5 | Legacy static `poll_auth_b64` path still active | A static, reusable pairing-derived credential could repeatedly retrieve the bearer token. It was not bound to a sequence, nonce, or timestamp. |
| 6 | Handshake response unsigned | The blocking handshake returned `session_token`, `expires_in`, and `device_id` without any MAC. |
| 7 | Invalid-MAC requests could mutate replay state | Sequence/nonce state was updated before signature verification, allowing an unauthenticated attacker to poison the replay window. |

---

## 3. Updated Canonical Response Format (V2)

### 3.1 Transcript Fields and Encoding

`CanonicalPollResponse` is built identically in Python and Kotlin:

| Field | Encoding | Notes |
| :--- | :--- | :--- |
| `version` | length-prefixed UTF-8 (`>I` length) | Protocol version, currently `"1"` |
| `response_context` | length-prefixed UTF-8 | `"poll"` or `"handshake"` |
| `session_id` | length-prefixed UTF-8 | Pairing session identifier |
| `http_status` | `>H` (2 bytes, big-endian) | Signed HTTP status (200 on success) |
| `sequence` | `>Q` (8 bytes, big-endian) | Poll: monotonic request sequence (`>= 1`); handshake: `0` |
| `request_nonce` | length-prefixed bytes | Poll: unique 16-byte request nonce; handshake: verified 32-byte client nonce |
| `status` | length-prefixed UTF-8 | `"pending_approval"` or `"approved"` |
| `expires_in` | `>Q` (8 bytes, big-endian) | Remaining session lifetime in seconds |
| `device_id` | length-prefixed UTF-8 | Client ID bound to the session |
| `credential_state` | length-prefixed UTF-8 | `"none"` or `"delivered"` |
| `credential_sha256` | length-prefixed bytes | Empty when `"none"`; 32-byte SHA-256 of the UTF-8 token when `"delivered"` |

Length-prefixing uses `pack(">I", len(field)) || field`, eliminating delimiter/concatenation ambiguity.

### 3.2 Domain Separation and MAC

```text
response_mac = HMAC-SHA256(
    K_poll,
    "KIVO_BRIDGE_POLL_RESPONSE_V2\x00" || CanonicalPollResponse
)
```

- The V2 label is distinct from the request domain (`KIVO_BRIDGE_POLL_REQUEST_V1`) and the removed V1 response domain.
- Comparison uses constant-time equality (`hmac.compare_digest` in Python, `MessageDigest.isEqual` in Kotlin).
- The raw bearer token never appears in the canonical transcript; only its SHA-256 digest does. The digest binds the exact token string returned in `session_token`.

### 3.3 Response Body

The server returns all signed fields so the client can rebuild the transcript:

```json
{
  "ok": true,
  "response_context": "poll",
  "http_status": 200,
  "session_id": "sess_...",
  "sequence": 3,
  "status": "approved",
  "expires_in": 7198,
  "device_id": "client_...",
  "credential_state": "delivered",
  "credential_sha256": "8c2ddfd1...",
  "session_token": "sess_tok_...",
  "response_sig": "66a0a2a9..."
}
```

- `session_token` is present **only** when `credential_state == "delivered"`.
- Pending responses omit the token, use `expires_in = 0`, and carry `credential_state = "none"` plus an empty credential hash.

### 3.4 Cross-Protocol Confusion Prevention

| Property | Poll response | Handshake response |
| :--- | :--- | :--- |
| `response_context` | `"poll"` | `"handshake"` |
| `sequence` | `>= 1` | `0` |
| `request_nonce` | Fresh 16-byte request nonce | Verified 32-byte client nonce |
| Trigger | Fresh signed poll | Successful blocking handshake after approval |

A captured handshake response cannot be replayed as a poll response (and vice versa) because both the context label and the sequence/nonce binding differ.

---

## 4. Credential Delivery and Recovery Semantics

### 4.1 State-by-State Behavior

| State / Event | Response | Credential disclosure | Key handling |
| :--- | :--- | :--- | :--- |
| Pending approval | `200`, `status = pending_approval`, `credential_state = none`, `expires_in = 0` | None | `K_poll` retained for the approval decision |
| Approved (handshake completes) | `200`, signed handshake V2 response, `status = approved`, `credential_state = delivered` | Token + expiry + device ID, all MAC-bound | `K_poll` retained for the 30s recovery window |
| Fresh authenticated poll while approved | `200`, signed poll V2 response, `status = approved`, `credential_state = delivered` | Token returned again (idempotent re-fetch) | Same key, same window |
| Response lost during network interruption | Client retries with a fresh nonce and the next sequence | Credential re-delivered on the next successful poll | Unchanged |
| Recovery window expired (`> 30s` after approval) | `410 Gone`, `polling_window_closed` | None | `K_poll`, sequence, nonce, cached keys, and approval time purged on the failed poll |
| Rejected pairing (waiting handshake) | `403`, `rejected_by_desktop` | None | Polling state purged; offer permanently consumed |
| Poll after rejection | `401` (`unknown_or_expired_polling`) | None | No polling key remains |
| Revoked session | `401` (`unknown_or_expired_polling`) | None | Polling state purged immediately |
| Replayed old poll request | `409` (`replay_sequence_invalid` / `replay_nonce_detected`) | None | Replay state preserved; no credential |
| Invalid MAC | `401` (`invalid_signature`) | None | **No state mutation**, so an attacker cannot poison replay state |
| Public session ID only | `401` (`unknown_or_expired_polling`) | None | Session ID alone is never authorization |
| Expired/missing polling key | `401` (`unknown_or_expired_polling`) | None | No key means no credential |
| Legacy static `poll_auth_b64` request | `422` validation error | None | Static credential path removed |

### 4.2 Why Re-Fetch Instead of One-Time Acknowledgement

Approved credentials are returned on **every fresh authenticated poll** during the 30-second recovery window, rather than through a one-time delivery/acknowledgement handshake.

Justification:

1. **Lost-response safety.** TLS can fail after the server has committed approval but before the client receives the response. A one-time delivery would permanently strand a legitimate client whose QR offer is already consumed.
2. **No reduction in authorization strength.** Every re-fetch requires a fresh HMAC over a new sequence number and unique nonce, within a ±60s timestamp window, using `K_poll` — a key only the legitimate client can derive.
3. **Replay resistance is independent of delivery count.** Strictly monotonic sequences and nonce deduplication reject captured requests; the 30s cap bounds the exposure window even if `K_poll` were later compromised.
4. **Idempotence matches the credential model.** The bearer token is a per-session secret; re-delivering the same token to the same authenticated key holder does not create multiple credentials or extend the session lifetime.

This is therefore **not** an unrecoverable one-time delivery design.

### 4.3 Sequence/Nonce Ordering Fix

Request verification order is now:

1. Key presence.
2. Timestamp freshness.
3. **MAC verification.**
4. Monotonic sequence check.
5. Nonce uniqueness check.
6. State mutation and response construction.

Because MAC verification precedes any mutation, a forged request cannot consume a sequence number or nonce and deny service to the legitimate client.

---

## 5. Python/Kotlin Compatibility Results

### 5.1 Shared Fixtures

`tests/fixtures/pairing_test_vectors.json` now contains V2 response vectors generated by Python:

| Vector | Value |
| :--- | :--- |
| `k_poll_hex` | `a91e7c826c62d2c9a5b42dbefef6a2de0bf8b8fb83965160703d0fe2f79a9e7b` |
| `poll_resp_credential_sha256_hex` | `8c2ddfd103a6e98ee17fb3bcbc0819fc9c54874db08bffc764cdae2c143f0556` |
| `canonical_poll_resp_hex` | `000000013100000004706f6c6c...8c2ddfd1...` |
| `poll_resp_sig_v2_hex` | `66a0a2a92f2cf68328d2e959f4c77e7543a9d9ee426dbf720aa9124351f5e1fe` |
| `canonical_pending_resp_hex` | `000000013100000004706f6c6c...000000046e6f6e6500000000` |
| `pending_resp_sig_v2_hex` | `ec6c82f56c4a342703c2459c4281692af52084aaff098d68e4a99ee7c8c4f9ed` |
| `canonical_handshake_resp_hex` | `00000001310000000968616e647368616b65...` |
| `handshake_resp_sig_v2_hex` | `db51bf809fc05df6bd7d953e6049b0fbf51a9bc9eae76fdbba19bcbb1ad7fc89` |

Obsolete V1 fixture keys (`poll_auth_hex`, `poll_auth_b64`, `poll_resp_sig_hex`) were removed so the fixture no longer advertises a retired contract.

### 5.2 Byte-for-Byte Agreement

| Check | Python | Kotlin |
| :--- | :--- | :--- |
| Canonical response transcript | `build_canonical_poll_response` | `CanonicalPollResponse.build` |
| Credential hash | `compute_credential_sha256` | `PollSigner.computeCredentialSha256` |
| Response MAC | `compute_poll_response_signature` | `PollSigner.computeResponseSignatureV2` |
| Verification | `verify_poll_response_signature` | `PollSigner.verifyResponseSignatureV2` |
| Response validation | `assert_signed_response` in integration tests | `PairingResponseVerifier.verify` |
| Request binding | Canonical poll request + sequence/nonce/timestamp | `CanonicalPollRequest.build` + `PollSigner.computeSignature` |

Kotlin tests assert the exact approved-poll, pending-poll, and handshake transcript hex and MAC hex produced by Python. Credential hash encoding is asserted independently.

Rejection is represented at the HTTP layer: the server returns `403 rejected_by_desktop` with no credential body and purges polling state. The V2 verifier accepts only `approved` / `pending_approval`; a payload with `status = "rejected"` is refused with `invalid_response_status`. Python integration tests assert the 403 rejection flow and the replay rejection that follows.

### 5.3 Android Trust Path

`PairingResponseVerifier` now validates, before any persistence:

1. `ok == true`.
2. `status` is `approved` or `pending_approval`.
3. `response_context` matches the expected transport (`poll` / `handshake`).
4. `http_status == 200`.
5. `session_id` matches the offer.
6. Response `sequence` matches the authenticated request sequence.
7. `expires_in` is within `0..86400`.
8. `device_id` is non-empty and bounded.
9. `credential_state` is consistent with `status` (`none` only while pending, `delivered` only when approved).
10. Pending responses carry no token; delivered responses carry a token and a 32-byte hash that matches the locally computed SHA-256 of that token.
11. The 32-byte response MAC verifies over the reconstructed V2 transcript.

`PairingRepository.executePairing` additionally requires:

- the signed `device_id` equals the locally generated `clientId`;
- the signed `session_token` equals the locally derived bearer token;
- only the verified token is persisted to the Keystore envelope.

The handshake response now goes through the same verifier with `response_context = "handshake"`, `sequence = 0`, and the client nonce.

---

## 6. Keystore Verification Limitations

The existing three-tier distinction is preserved:

| Tier | Status | What it proves |
| :--- | :--- | :--- |
| JVM/JCA unit tests (`KeystoreStorageTest`) | **9/9 PASS** | AES-256-GCM envelope serialization, AAD binding, IV/ciphertext tamper rejection, key rotation, expiry purge, file erasure using the JVM `SunJCE` provider. |
| Android emulator instrumentation tests | **Not run** | No emulator is attached; no SDK components were installed automatically. |
| Physical-device hardware-backed Keystore | **Outstanding** | TEE/StrongBox key generation, `setUserAuthenticationRequired`, persistence across reboot, and hardware key attestation remain unverified. |

**JVM encryption tests are not evidence of hardware-backed Keystore security.** The JVM provider is software-only. Real-device validation remains a Stage 13.3.4 hardware integration item.

`adb devices` returned an empty device list during this stage; no physical phone pairing was performed.

---

## 7. Adversarial Test Coverage

### 7.1 Python

| Attack / Scenario | Test | Result |
| :--- | :--- | :--- |
| Mutate any response transcript field | `test_response_v2_every_field_is_authenticated` (11-field mutation matrix: version, context, session, HTTP status, sequence, nonce, status, expiry, device, credential state, credential hash) | MAC rejected for every mutation |
| Forged request MAC | `test_invalid_signature_does_not_consume_sequence_or_nonce` | Rejected without consuming sequence/nonce |
| Replay sequence | `test_fresh_authenticated_poll_status_flow` | HTTP 409 |
| Replay nonce with a new sequence | `test_fresh_authenticated_poll_status_flow` | HTTP 409 |
| Legacy static credential | `test_poll_status_endpoint` | HTTP 422 |
| Out-of-range sequence / timestamp | `test_poll_status_endpoint` | HTTP 422 (no canonical-encoding 500) |
| Pending response leaks token | `test_pending_poll_response_is_signed_without_credential` | No token; state `none` |
| Approval response omits token binding | `test_approved_poll_response_signs_credential_hash_and_metadata` | MAC binds hash, expiry, device |
| Recovery window expiry | `test_recovery_window_expiry_purges_polling_material` | `polling_window_closed`; polling state purged |
| Revocation | `test_revoked_session_cannot_retrieve_credentials` | No credential after revoke |
| Rejection | `test_rejected_pairing_purges_polling_state` | Polling state purged |
| Reset | `test_reset_clears_all_polling_material` | All polling maps empty |
| Tampered expiry in body | `test_fresh_authenticated_poll_status_flow` | Re-verification assertion fails |

### 7.2 Kotlin

`PairingResponseVerifierTest` (16 tests) covers:

- Valid approved response.
- Token alteration (`credential_hash_mismatch`).
- Expiry alteration (`invalid_response_signature`).
- Device ID alteration (`invalid_response_signature`).
- Status alteration (`invalid_response_signature`).
- Request nonce alteration (`invalid_response_signature`).
- Sequence MAC alteration (`invalid_response_signature`).
- Response JSON sequence alteration (`invalid_response_sequence`).
- HTTP status alteration (`invalid_response_http_status`).
- Response context alteration (`invalid_response_context`).
- Session ID alteration (`invalid_response_session`).
- Rejection status is not accepted as a credential response (`invalid_response_status`).
- Pending response carrying a token (`invalid_credential_state`).
- Approved response without a credential (`invalid_credential_state`).
- Out-of-range expiry (`invalid_response_expiry`).
- Malformed MAC (`invalid_response_signature`).

`CryptoInteroperabilityTest` (11 tests) covers context, proof, SAS, session keys, poll key, canonical poll request/signature, credential hash, approved/pending/handshake V2 transcripts, and an 11-field Kotlin tamper matrix.

---

## 8. Verification Gates

| Gate | Check | Command | Result |
| :---: | :--- | :--- | :--- |
| **1** | Focused bridge suite | `.venv/bin/python -m pytest tests/test_bridge_*.py -q` | **52/52 PASSED** (6.39s) |
| **2** | Core response/state tests | `.venv/bin/python -m pytest tests/test_bridge_crypto.py tests/test_bridge_pairing.py tests/test_bridge_integration.py -q` | **41/41 PASSED** (3.07s) |
| **3** | Android unit tests | `cd android-companion && ./gradlew testDebugUnitTest` | **54/54 PASSED** (0 failures) |
| **4** | Android lint + debug APK | `cd android-companion && ./gradlew lintDebug assembleDebug` | **BUILD SUCCESSFUL**; lint **0 errors, 10 warnings**; APK produced |
| **5** | Full backend suite | `.venv/bin/python -m pytest tests/ -q` | **906 PASSED**, 176 subtests, 8 warnings (71.23s) |
| **6** | Shell test suite | `.venv/bin/python -m pytest shell/tests/ -q` | **16/16 PASSED** (15.81s) |
| **7** | Qt6 QML lint | `/usr/lib/qt6/bin/qmllint $(find shell/ -name "*.qml")` | **Exit 0** (zero errors; pre-existing warnings) |
| **8** | Desktop health | `./scripts/desktop-health.sh` | **9/9 PASS** |
| **9** | Whitespace / syntax | `git diff --check` | **Exit 0** (zero issues) |

Android test breakdown: `PairingResponseVerifierTest` 16, `CryptoInteroperabilityTest` 11, `KeystoreStorageTest` 9, `QrParserTest` 9, `TlsPinnerTest` 9.

Lint warnings are pre-existing and unrelated to this stage (target SDK/dependency update notices, custom TrustManager advisory, backup attribute). No new errors were introduced.

---

## 9. Remaining Risks and Limitations

1. **Android foreground recovery orchestration is not wired.** The server supports 30s credential re-fetch and `BridgeHttpClient.pollPairingStatus` implements signed polling + V2 verification, but `PairingRepository` still uses a handshake-first flow. If the handshake HTTP response is lost after the server commits approval, the current UI does not automatically switch to polling and may surface a failure even though the protocol allows recovery. Recommended follow-up in the Stage 13.3.3 integration work.
2. **Hardware-backed Keystore remains unvalidated.** JVM/JCA tests do not exercise TEE/StrongBox; no emulator or physical device was attached, and no SDK components were installed automatically.
3. **Python memory erasure is best-effort.** `bytes` objects are immutable and garbage-collected; unreferencing narrows retention but cannot guarantee C-level zeroization.
4. **The 30s recovery window is an availability/security trade-off.** Within it, any holder of `K_poll` who can produce a fresh signed request can re-fetch the token by design. After it, all polling material is purged on the next access and no credential is returned.
5. **No real LAN listener or physical phone pairing was performed.** Verification used loopback TLS integration tests and JVM cryptographic vectors only.
6. **Server authenticity depends on pinned TLS.** The V2 transcript binds the session and credential but does not include the server certificate fingerprint; transport identity relies on the existing TLS pinner.

---

## 10. Boundaries Observed

- Pairing protocol only; no file transfer or quarantine implementation.
- No QML changes.
- No agent tools added.
- No real LAN listener enabled for production use; integration tests bind loopback only.
- No physical phone pairing.
- No new dependencies.
- No commits or pushes; the working tree remains uncommitted.

---

## 11. Final Verdict

# **PASS WITH LIMITATIONS**

The response-authentication contract is complete: every field the client consumes is covered by a domain-separated HMAC over an unambiguous V2 transcript, the static reusable polling credential is gone, replay/expiry/revocation/rejection paths never disclose credentials, and Python/Kotlin fixtures agree byte-for-byte. The remaining limitations are the unvalidated hardware-backed Keystore and the not-yet-wired Android automatic polling fallback; neither leaves an unauthenticated credential path open.

**Stage 13.3.3 may proceed** because the approval-response integrity contract is complete.
