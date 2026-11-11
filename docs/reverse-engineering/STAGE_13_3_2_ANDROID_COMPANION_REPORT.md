# KIVO — Stage 13.3.2 Native Android Companion & Secure Pairing Client Report

```
Document:   Stage 13.3.2 Native Android Companion & Secure Pairing Client Report
Status:     Complete
Date:       2026-11-09
Baseline:   HEAD 1062c0d; Stages 12.2 and 13.1–13.3.1.1 completed
Artifact:   android-companion/app/build/outputs/apk/debug/app-debug.apk (25 MB)
Language:   Kotlin 2.0.0, Jetpack Compose, OpenJDK 21, Gradle 8.9
Min SDK:    API 29 (Android 10) | Target SDK: API 35 (Android 15)
```

---

## 1. Executive Summary

Stage 13.3.2 builds the standalone **native Android companion application** (`android-companion/`) for KIVO's Phone-to-PC Bridge.

The application implements client-side pairing with KIVO desktop:
- **Zero Browser WebCrypto Insecure Context**: Completely eliminates the flawed plain HTTP browser proposal.
- **Strict TLS 1.3 with Certificate Fingerprint Pinning**: Enforces `TLSv1.3` and validates that the desktop's leaf certificate SHA-256 digest matches the QR-delivered fingerprint byte-for-byte, eliminating MITM risks on local Wi-Fi.
- **Exact Byte-for-Byte Cross-Platform Compatibility**: Uses identical length-prefixed canonical binary context encoding, HMAC-SHA256 client proof, unbiased HKDF-SHA256 SAS derivation, and directional session keys verified against shared test vectors.
- **Native Android KeyStore AES-256-GCM Envelope Storage**: Replaces deprecated `EncryptedSharedPreferences` with native KeyStore master keys and Authenticated Additional Data (`cipher.updateAAD`).
- **Minimal Permissions**: Strictly requests only `CAMERA`, `INTERNET`, and `ACCESS_NETWORK_STATE`; cleartext HTTP traffic is globally disabled.

---

## 2. Project Structure & Dependency Architecture

```text
android-companion/
  ├── build.gradle.kts                # Root build file
  ├── settings.gradle.kts             # Module definitions
  ├── gradle.properties               # JVM memory and AndroidX flags
  ├── gradlew                         # Gradle 8.9 wrapper executable
  └── app/
      ├── build.gradle.kts            # Application configuration & dependencies
      ├── src/main/
      │   ├── AndroidManifest.xml     # Strict permissions & usesCleartextTraffic="false"
      │   ├── res/
      │   │   └── drawable/ic_launcher.xml
      │   └── java/com/kivo/companion/
      │       ├── MainActivity.kt     # Single Activity Compose entry point
      │       ├── model/
      │       │   └── Models.kt       # PairingOffer, SessionCredentials, PairingState
      │       ├── crypto/
      │       │   ├── Crypto.kt       # CanonicalContext, Hkdf, PairingProof, SasDerivation, SessionKeyDerivation
      │       │   └── TlsPinner.kt    # PinnedCertificateTrustManager, Tls13SocketFactory, PinnedHostnameVerifier
      │       ├── storage/
      │       │   └── KeystoreSecretStorage.kt # Native Android KeyStore AES-256-GCM envelope
      │       ├── network/
      │       │   └── BridgeHttpClient.kt      # Pinned OkHttpClient with long-polling approval
      │       ├── pairing/
      │       │   ├── QrParser.kt              # Strict kivo-pair:// parser and RFC 1918 validator
      │       │   ├── PairingRepository.kt     # End-to-end pairing journey coordinator
      │       │   └── PairingViewModel.kt      # StateFlow UI state machine
      │       └── ui/
      │           ├── theme/Theme.kt           # KIVO desktop palette (charcoal surfaces, emerald accents)
      │           └── screens/Screens.kt       # HomeScreen, QrScannerScreen, SasScreen, PairedScreen
      └── src/test/java/com/kivo/companion/
          ├── CryptoInteroperabilityTest.kt   # Byte-for-byte comparison with Python fixtures
          └── QrParserTest.kt                 # Adversarial URI validation & scheme rejection
```

### SDK and Dependency Decisions
- **`compileSdk = 35` / `targetSdk = 35`**: Targets Android 15.
- **`minSdk = 29` (Android 10)**: Guarantees native TLS 1.3 protocol support across all supported devices.
- **Libraries**:
  - `androidx.compose.material3`: Modern UI matching KIVO's dark design tokens.
  - `androidx.camera:camera-* (1.3.4)`: CameraX lifecycle-aware QR scanner.
  - `com.google.zxing:core (3.5.3)`: Offline barcode binarizer and QR decoder.
  - `com.squareup.okhttp3:okhttp (4.12.0)`: Network transport supporting custom `SSLSocketFactory` and `X509TrustManager`.
  - `org.jetbrains.kotlinx:kotlinx-coroutines-* (1.8.1)`: Asynchronous non-blocking network calls.

---

## 3. Protocol & Cryptographic Interoperability

### 3.1 Deterministic Cross-Platform Test Vectors
Kotlin and Python implementations were verified byte-for-byte using shared fixtures (`tests/fixtures/pairing_test_vectors.json`):

| Component | Target Wire Format | Verified Match |
|---|---|---|
| **Canonical Context** | 4-byte BE length-prefixed binary transcript (`0000000131...`) | **MATCH (Byte-for-byte)** |
| **HMAC Client Proof** | `HMAC-SHA256(T_pair, b"KIVO_BRIDGE_CLIENT_PROOF_V1\0" + context)` | **MATCH (Byte-for-byte)** |
| **SAS Calculation** | HKDF-SHA256 with rejection sampling (`num < 4,294,000,000`) | **MATCH (`779 684`)** |
| **Directional Upstream Key** | `HKDF-Expand(PRK, b"kivo-session-upstream-v1\0" + context, 32)` | **MATCH (`a5050108...`)** |
| **Directional Downstream Key** | `HKDF-Expand(PRK, b"kivo-session-downstream-v1\0" + context, 32)` | **MATCH (`4a34cf26...`)** |
| **Bearer Token** | `sess_tok_` + base64url(`HKDF-Expand(PRK, bearer_info, 32)`) | **MATCH (`sess_tok_buOWe...`)** |

---

## 4. Strict TLS 1.3 & Certificate Fingerprint Pinning

### 4.1 Leaf Certificate Fingerprint Pinning
The mobile app bypasses public CA validation in favor of direct fingerprint verification:
1. When TLS connects, `PinnedCertificateTrustManager` intercepts the peer certificate chain.
2. Computes `SHA-256` over `chain[0].encoded` (complete DER-encoded leaf certificate).
3. Verifies constant-time equality (`MessageDigest.isEqual`) against the 64-hex fingerprint from the QR code.
4. Checks certificate validity dates (`leaf.checkValidity()`).
5. Verifies Subject Alternative Name (SAN) includes an `iPAddress` (type 7) matching the advertised LAN IP.
6. Enforces `TLSv1.3` on `SSLSocket`.

---

## 5. Native Android KeyStore Session Storage

In accordance with Phase 7 instructions, deprecated `EncryptedSharedPreferences` was rejected. Storage uses direct Android KeyStore primitives:
1. **Master Key**: Generated inside `AndroidKeyStore` as an AES-256 key restricted to GCM mode (`AES/GCM/NoPadding`) with randomized encryption required.
2. **Authenticated Additional Data (AAD)**: Binds `kivo-envelope-v1:<sessionId>` to the GCM authentication tag.
3. **Envelope Format**: `[1-byte version][12-byte random IV][ciphertext + 16-byte GCM auth tag]`.
4. **App-Private Storage**: Written to `context.filesDir/kivo_session.enc` (mode `0600`), excluded from cloud backups (`android:allowBackup="false"`).

---

## 6. Pairing Flow Journey

```text
[HomeScreen] ──> [Scan QR Button] ──> [QrScannerScreen]
                                             │ (Scans kivo-pair://v1)
                                             ▼
                                     [Validates URI & LAN IP]
                                             │
                                             ▼
                                     [TLS 1.3 Pinned Connect]
                                             │
                                             ▼
                                     [Derives SAS: "779 684"]
                                             │
                                             ▼
                                     [SasScreen (Displays Code)]
                                     "Awaiting desktop approval…"
                                             │ (HTTP 200 OK received)
                                             ▼
                                     [Stores KeyStore Envelope]
                                             │
                                             ▼
                                     [PairedScreen (Connected)]
```

---

## 7. Build Verification & Quality Gates

### 7.1 Android Build & Test Results
- **Kotlin Unit Tests**: `13 passed in 1s` (`CryptoInteroperabilityTest` + `QrParserTest`).
- **Android Lint**: `BUILD SUCCESSFUL in 3s` (0 errors, 0 warnings).
- **Assemble Debug APK**: `BUILD SUCCESSFUL in 31s`.
  - Artifact: `android-companion/app/build/outputs/apk/debug/app-debug.apk` (25 MB).
- **AAPT Badging Verification**:
  ```
  package: name='com.kivo.companion' versionCode='1' versionName='0.1.0'
  sdkVersion:'29'
  targetSdkVersion:'35'
  uses-permission: name='android.permission.INTERNET'
  uses-permission: name='android.permission.ACCESS_NETWORK_STATE'
  uses-permission: name='android.permission.CAMERA'
  application-label:'KIVO Companion'
  ```

### 7.2 Desktop Verification Gates
- **Full Backend Test Suite**: `.venv/bin/python -m pytest tests/ -q` → **878 passed, 176 subtests** (68.0s).
- **Shell Test Suite**: `.venv/bin/python -m pytest shell/tests/ -q` → **16 passed** (15.3s).
- **Qt6 QML Lint**: `/usr/lib/qt6/bin/qmllint shell/**/*.qml` → **Exit 0** (0 errors).
- **Desktop Health Check**: `./scripts/desktop-health.sh` → **9/9 PASS**.
- **Git Whitespace**: `git diff --check` → **Exit 0**.

---

## 8. Build & Install Instructions

### To run unit tests:
```bash
cd android-companion
./gradlew test
```

### To build the debug APK:
```bash
cd android-companion
./gradlew assembleDebug
```
Output: `android-companion/app/build/outputs/apk/debug/app-debug.apk`.

### To install on an Android device:
```bash
adb install -r android-companion/app/build/outputs/apk/debug/app-debug.apk
```

---

## 9. Strict Boundaries & Limitations

- ✅ **Android pairing client only**: File upload, download, and background transfers remain scheduled for Stage 13.3.3.
- ✅ **No QML UI changes**.
- ✅ **No Agent tools registered**.
- ✅ **No real phone paired without explicit authorization**.
- ✅ **No git commit or push**.

---

**Verdict: PASS**
