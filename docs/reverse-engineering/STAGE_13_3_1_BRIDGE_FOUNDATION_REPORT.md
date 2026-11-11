# KIVO — Stage 13.3.1 Desktop Bridge Foundation & Secure Pairing Backend Report

```
Document:   Stage 13.3.1 Desktop Bridge Foundation & Secure Pairing Backend Report
Status:     Complete
Date:       2026-11-09
Baseline:   HEAD 1062c0d; Stage 12.2 and Stages 13.1–13.2 completed
Scope:      Ephemeral TLS identity, physical LAN interface binding, QR pairing offer,
            one-time token proof, HKDF-SHA256 SAS derivation, desktop approval state machine,
            session key derivation, IPC operations & events
```

---

## 1. Executive Summary

Stage 13.3.1 implements the **desktop-side foundation and secure pairing backend** for KIVO's Phone-to-PC Bridge (`kivo/bridge/`).

This stage executes the security redesign established in Stage 13.3.0, replacing the flawed plain HTTP browser proposal with an offline-capable, cryptographically robust pairing and session foundation:
1. **Ephemeral TLS 1.3 Identity**: Generated in memory on demand (ECDSA P-256 / `SECP256R1`) with a 64-character SHA-256 certificate fingerprint and a 24-hour validity window.
2. **Strict Physical LAN Binding**: Dynamic interface discovery probes default routing and binds strictly to the active physical LAN interface (`192.168.1.7:8443`), explicitly filtering out Docker bridges (`docker0`, `br-*`, `veth*`).
3. **QR Pairing Offer Generation**: Generates ephemeral 256-bit random secrets and renders QR code PNG data URLs entirely in-memory using `qrcode` and `PIL.Image` (zero disk footprint).
4. **Unambiguous Pairing Context**: Canonical binary transcript encoding with 4-byte big-endian length prefixing for every field, eliminating string concatenation ambiguity.
5. **Constant-Time Proof Verification**: Client proof verified using `hmac.compare_digest` with single-use token burning and rate-limiting.
6. **Unbiased SAS Derivation**: 6-digit Short Authentication String derived via HKDF-SHA256 (RFC 5869) with rejection sampling to eliminate modulo bias.
7. **Physical Desktop Approval Gate**: Pairing handshake enters a `CONFIRMING` state requiring physical desktop user confirmation before session credentials can be derived.
8. **Directional Session Key Derivation**: Derives independent directional keys ($K_{\text{up}}$, $K_{\text{down}}$) and bearer token with 2-hour TTL.

---

## 2. Implementation Deliverables

| File | Status | Description |
|---|---|---|
| `kivo/bridge/__init__.py` | **New** | Package exports: states, models, service, pairing manager. |
| `kivo/bridge/models.py` | **New** | Immutable data models: `PairingOffer`, `PairingRequest`, `PairedSession`, `BridgeStatus`, state enums, TTL constants. |
| `kivo/bridge/crypto.py` | **New** | `generate_ephemeral_certificate` (P-256 ECDSA, SAN LAN IP, 24h), `build_canonical_context` (length-prefixed binary transcript), `compute_client_proof`, `verify_client_proof` (constant-time HMAC), `derive_sas_code` (unbiased HKDF-SHA256), `derive_session_keys` (directional keys + bearer token). |
| `kivo/bridge/net.py` | **New** | Physical LAN interface discovery (filters Docker/virtual interfaces), ephemeral port allocator, port availability check. |
| `kivo/bridge/qr.py` | **New** | QR URI construction (`kivo-pair://v1`) and in-memory PNG data URL rendering via `qrcode` + `PIL.Image`. |
| `kivo/bridge/pairing.py` | **New** | `PairingManager`: state machine (`IDLE` $\rightarrow$ `OFFERING` $\rightarrow$ `CONFIRMING` $\rightarrow$ `PAIRED` / `REJECTED` / `REVOKED`), one-time token burning, rate limiting, desktop approval event. |
| `kivo/bridge/server.py` | **New** | `BridgeServer`: ephemeral Uvicorn/FastAPI ASGI server with TLS 1.3, dedicated background thread (`kivo-bridge-server`), endpoints: `POST /api/v1/pair/handshake`, `GET /api/v1/ping`, `DELETE /api/v1/session`. |
| `kivo/bridge/service.py` | **New** | `PhoneBridgeService`: service coordinator, LAN binding, lifecycle management (`start_bridge`, `stop_bridge`, `approve_pairing`, `reject_pairing`, `revoke_device`, `close`). |
| `kivo/ipc/protocol.py` | **Modified** | Added 6 bridge operations and 2 events to allowlists; added strict parameter validation (`_validate_bridge_*`). |
| `kivo/ipc/handlers.py` | **Modified** | Injected `bridge_service`; implemented handlers for `bridge_start`, `bridge_stop`, `bridge_get_status`, `bridge_approve_pairing`, `bridge_reject_pairing`, `bridge_revoke_device`. |
| `kivo/app.py` | **Modified** | Initialized `PhoneBridgeService` in lifecycle; connected callbacks `on_status_changed` and `on_pairing_requested`; added orderly shutdown cleanup. |
| `tests/test_bridge_crypto.py` | **New** | 6 unit tests for ephemeral P-256 cert, canonical binary context, proof verification, SAS test vectors, and directional session keys. |
| `tests/test_bridge_pairing.py` | **New** | 7 unit & adversarial tests for offer generation, handshake validation, rate limiting, desktop approval, rejection, revocation, and token burning. |
| `tests/test_bridge_ipc.py` | **New** | 11 integration tests for protocol validation, parameter rejection, handler execution, and event delivery. |
| `tests/test_stage12_2_regression.py` | **Modified** | Added regression assertions verifying zero bridge agent tools and zero shell execution in `kivo/bridge/`. |

---

## 3. Cryptographic and Protocol Specifications

### 3.1 Pairing Transcript Canonical Binary Encoding
To eliminate field boundary collision and string parsing ambiguities, `build_canonical_context()` encodes every element using big-endian 4-byte length prefixing:
```
[4B len(version)] [version bytes]
[4B len(session_id)] [session_id bytes]
[4B len(cert_fp)] [cert_fp bytes]
[4B len(server_nonce)] [server_nonce bytes] (32 bytes)
[4B len(client_nonce)] [client_nonce bytes] (32 bytes)
[4B len(client_id)] [client_id bytes]
[4B len(algorithm_id)] [algorithm_id bytes] ("HMAC-SHA256-HKDF-SAS-V1")
```

### 3.2 Proof of Possession & Handshake Verification
- **Client Proof**:
  $$\text{Proof}_{\text{client}} = \text{HMAC-SHA256}(T_{\text{pair}}, \text{b"KIVO\_BRIDGE\_CLIENT\_PROOF\_V1\x00"} \parallel \text{Context}_{\text{pairing}})$$
- **Constant-Time Verification**: `hmac.compare_digest` prevents timing attacks.
- **Single-Use Invalidation**: $T_{\text{pair}}$ is burned immediately on the first handshake attempt, regardless of whether pairing is approved or rejected.

### 3.3 Unbiased SAS Derivation
- To avoid modulo bias where $2^{32} \not\equiv 0 \pmod{1{,}000{,}000}$, `derive_sas_code()` enforces rejection sampling:
  $$\text{Ceiling} = 2^{32} - (2^{32} \pmod{1{,}000{,}000}) = 4{,}294{,}000{,}000$$
  If the extracted 32-bit unsigned integer $\ge 4{,}294{,}000{,}000$, HKDF expands with an incremented counter until a value below the ceiling is obtained.
- Resulting integer is formatted as a 6-digit decimal string (`"482 910"`).

### 3.4 Directional Session Keys & Credential Storage
- Derived using HKDF-Expand with distinct domain separation strings:
  - Upstream Key: `b"kivo-session-upstream-v1\x00" + context`
  - Downstream Key: `b"kivo-session-downstream-v1\x00" + context`
  - Bearer Token: `b"kivo-session-bearer-v1\x00" + context`
- **Mobile Storage Policy**: The companion app encrypts $K_{\text{up}}$, $K_{\text{down}}$, and $\text{Token}_{\text{session}}$ using an Android Keystore-backed AES-256-GCM master key before persisting them into app-private storage (`EncryptedSharedPreferences`).

---

## 4. Verification Gate Results

All 10 verification gates passed cleanly:

| # | Gate | Command | Result |
|---|---|---|---|
| 1 | Focused Phone Bridge Tests | `.venv/bin/python -m pytest tests/test_bridge_*.py -v` | **24 passed** in 3.66s |
| 2 | Regression & Security Tests | `.venv/bin/python -m pytest tests/test_stage12_2_regression.py -v` | **10 passed** in 0.67s |
| 3 | Combined Stage 12 + 13 Focused Suite | `.venv/bin/python -m pytest tests/test_telemetry_*.py tests/test_microphone_*.py tests/test_clipboard_*.py tests/test_shelf_*.py tests/test_bridge_*.py tests/test_stage12_2_regression.py -q` | **257 passed** in 30.7s |
| 4 | Full Backend Test Suite | `.venv/bin/python -m pytest tests/ -q` | **878 passed, 176 subtests passed** in 68.0s (0 failures) |
| 5 | Shell Test Suite | `.venv/bin/python -m pytest shell/tests/ -q` | **16 passed** in 15.3s |
| 6 | Qt6 QML Lint | `/usr/lib/qt6/bin/qmllint shell/**/*.qml` | **Exit 0** (zero errors) |
| 7 | Desktop Health Check | `./scripts/desktop-health.sh` | **9/9 PASS** |
| 8 | Git Whitespace & Syntax | `git diff --check` | **Exit 0** |
| 9 | Python Compilation | `py_compile` all modified/added files | **ALL COMPILED OK** |
| 10 | Live Environment State | Verified host state | **Untouched** (no open bridge sockets, mic unmuted) |

---

## 5. Scope Confirmation

- ✅ **Desktop foundation and backend only**: Mobile Android client and actual chunked file transfers remain scheduled for subsequent stages.
- ✅ **No QML UI implementation**: QML UI components remain scheduled for Stage 13.4.
- ✅ **No Agent Tools**: Bridge operations are strictly for user desktop UI; never registered as LLM agent tools.
- ✅ **No 0.0.0.0 Binding**: Server binds strictly to the discovered physical LAN IP.
- ✅ **Zero New Dependencies**: Used existing libraries already declared in `.venv`.
- ✅ **No git commit or push**.

---

**Verdict: PASS**
