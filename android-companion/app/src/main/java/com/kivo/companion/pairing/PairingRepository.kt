package com.kivo.companion.pairing

import android.os.Build
import com.kivo.companion.crypto.ALGORITHM_ID
import com.kivo.companion.crypto.CanonicalContext
import com.kivo.companion.crypto.CanonicalPollRequest
import com.kivo.companion.crypto.PairingProof
import com.kivo.companion.crypto.PROTOCOL_VERSION
import com.kivo.companion.crypto.PollSigner
import com.kivo.companion.crypto.SasDerivation
import com.kivo.companion.crypto.SessionKeyDerivation
import com.kivo.companion.model.PairingOffer
import com.kivo.companion.model.SessionCredentials
import com.kivo.companion.network.BridgeHttpClient
import com.kivo.companion.network.PairingNetworkException
import com.kivo.companion.network.PairingResponseVerifier
import com.kivo.companion.storage.KeystoreSecretStorage
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.io.IOException
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import java.util.UUID

class PairingRepository(
    private val storage: KeystoreSecretStorage,
    private val secureRandom: SecureRandom = SecureRandom(),
    private val transportFactory: PairingTransportFactory = OkHttpPairingTransportFactory(),
    private val nowSeconds: () -> Long = { System.currentTimeMillis() / 1000L },
    private val wallClockMillis: () -> Long = { System.currentTimeMillis() },
    private val pollDelayMs: Long = 2_000L,
    private val delayFn: suspend (Long) -> Unit = { delay(it) }
) {
    companion object {
        const val PAIRING_APPROVAL_TTL_SECONDS = 60L
        const val APPROVAL_RECOVERY_WINDOW_SECONDS = 30L
        const val MAX_RECOVERY_SECONDS = 120L
        const val MAX_SESSION_EXPIRY_SECONDS = 86_400L
    }

    private val recoveryStore = PairingRecoveryStore(storage)
    private val pairingMutex = Mutex()

    fun loadPersistedSession(): SessionCredentials? {
        val credentials = storage.loadCredentials()
        if (credentials != null) {
            // A completed pairing supersedes any stale recovery record.
            recoveryStore.clear()
        }
        return credentials
    }

    fun loadPendingRecovery(): PendingPairingRecovery? {
        val record = recoveryStore.load() ?: return null
        if (record.isExpired(nowSeconds())) {
            recoveryStore.clear()
            return null
        }
        return record
    }

    /**
     * Executes exactly one handshake attempt for this pairing offer.
     *
     * Durable recovery state is written before the handshake is transmitted.
     * If the response is lost, the client never replays the consumed handshake
     * material; it polls with fresh sequence/nonce/timestamp/signature values.
     */
    suspend fun executePairing(
        offer: PairingOffer,
        onSasDerived: (String) -> Unit,
        onRecovering: (Int) -> Unit = {}
    ): SessionCredentials = pairingMutex.withLock {
        withContext(Dispatchers.IO) {
            if (offer.isExpired(nowSeconds())) {
                throw PairingNetworkException("offer_expired", "The pairing QR code has expired")
            }

            // If a durable attempt already exists for this session, resume it
            // instead of sending the handshake again.
            val existing = recoveryStore.load()
            if (existing != null) {
                if (existing.sessionId == offer.sessionId) {
                    onSasDerived(existing.sasCode)
                    return@withContext recoverUntilApproved(existing, onRecovering)
                }
                recoveryStore.clear()
            }

            val clientNonce = ByteArray(32).also(secureRandom::nextBytes)
            val clientId = "client_" + UUID.randomUUID().toString().replace("-", "").take(16)
            val deviceName = "${Build.MANUFACTURER} ${Build.MODEL}".trim()
                .ifEmpty { "Android device" }

            val contextBytes = CanonicalContext.build(
                version = PROTOCOL_VERSION,
                sessionId = offer.sessionId,
                certFingerprint = offer.certFingerprint,
                serverNonce = offer.serverNonce,
                clientNonce = clientNonce,
                clientId = clientId,
                algorithmId = ALGORITHM_ID
            )
            val proofBytes = PairingProof.compute(offer.pairingSecret, contextBytes)
            val clientProofB64 = Base64.getUrlEncoder().withoutPadding().encodeToString(proofBytes)
            val clientNonceB64 = Base64.getUrlEncoder().withoutPadding().encodeToString(clientNonce)
            val (_, sasCode) = SasDerivation.derive(
                offer.pairingSecret,
                offer.serverNonce,
                contextBytes
            )

            val derived = SessionKeyDerivation.derive(
                offer.pairingSecret,
                clientNonce,
                contextBytes
            )
            val kPoll = SessionKeyDerivation.derivePollKey(
                offer.pairingSecret,
                clientNonce,
                contextBytes
            )

            val now = nowSeconds()
            val deadline = minOf(
                offer.expiresAt,
                now + MAX_RECOVERY_SECONDS
            )
            val recovery = PendingPairingRecovery(
                sessionId = offer.sessionId,
                hostIp = offer.hostIp,
                port = offer.port,
                certFingerprint = offer.certFingerprint,
                kPoll = kPoll,
                upstreamKey = derived.upstreamKey,
                downstreamKey = derived.downstreamKey,
                contextBytes = contextBytes,
                sasCode = sasCode,
                deviceId = clientId,
                bearerTokenSha256 = PendingPairingRecovery.bearerTokenHash(derived.bearerToken),
                nextPollSequence = 1L,
                deadlineEpochSeconds = deadline,
                createdAtEpochSeconds = now
            )

            // Failure here must abort before the handshake: without durable
            // recovery state a lost response would strand the pairing.
            if (!recoveryStore.save(recovery)) {
                throw PairingNetworkException(
                    "pairing_recovery_unavailable",
                    "Could not persist pairing recovery state"
                )
            }
            onSasDerived(sasCode)

            val transport = transportFactory.create(offer.hostIp, offer.port, offer.certFingerprint)
            try {
                val rawResponse = try {
                    transport.handshake(
                        PairingHandshakeRequest(
                            hostIp = offer.hostIp,
                            port = offer.port,
                            certFingerprint = offer.certFingerprint,
                            version = PROTOCOL_VERSION,
                            sessionId = offer.sessionId,
                            clientId = clientId,
                            deviceName = deviceName,
                            clientNonceB64 = clientNonceB64,
                            clientProofB64 = clientProofB64
                        )
                    )
                } catch (e: Exception) {
                    if (e is CancellationException) throw e
                    currentCoroutineContext().ensureActive()
                    if (!isRecoverableHandshakeFailure(e)) {
                        recoveryStore.clear()
                        throw asPairingException(e)
                    }
                    return@withContext recoverUntilApproved(recovery, onRecovering)
                }

                currentCoroutineContext().ensureActive()

                val verified = try {
                    PairingResponseVerifier.verify(
                        responseJson = rawResponse,
                        kPoll = kPoll,
                        expectedContext = "handshake",
                        sessionId = offer.sessionId,
                        sequence = 0L,
                        requestNonce = clientNonce
                    )
                } catch (e: Exception) {
                    recoveryStore.clear()
                    throw asPairingException(e)
                }

                if (verified.status != "approved") {
                    recoveryStore.clear()
                    throw PairingNetworkException(
                        "unexpected_handshake_status",
                        "Handshake completed without an approved response"
                    )
                }
                if (verified.deviceId != clientId) {
                    recoveryStore.clear()
                    throw PairingNetworkException(
                        "device_id_mismatch",
                        "Signed handshake response named a different device"
                    )
                }
                val verifiedToken = verified.sessionToken
                    ?: throw PairingNetworkException(
                        "missing_credential",
                        "Signed handshake response did not include a session token"
                    )
                if (verifiedToken != derived.bearerToken) {
                    recoveryStore.clear()
                    throw PairingNetworkException(
                        "credential_mismatch",
                        "Signed session token does not match the locally derived bearer token"
                    )
                }
                return@withContext persistVerifiedCredentials(
                    recovery = recovery,
                    token = verifiedToken,
                    expiresIn = verified.expiresIn
                )
            } finally {
                transport.close()
            }
        }
    }

    /**
     * Resumes a pending pairing after activity recreation or process death.
     * Returns null when there is no live recovery record.
     */
    suspend fun resumePendingPairing(
        onSasDerived: (String) -> Unit,
        onRecovering: (Int) -> Unit = {}
    ): SessionCredentials? = pairingMutex.withLock {
        withContext(Dispatchers.IO) {
            val record = recoveryStore.load() ?: return@withContext null
            if (record.isExpired(nowSeconds())) {
                recoveryStore.clear()
                return@withContext null
            }
            onSasDerived(record.sasCode)
            recoverUntilApproved(record, onRecovering)
        }
    }

    fun cancelPendingPairing() {
        recoveryStore.clear()
    }

    suspend fun disconnect(hostIp: String, port: Int, certFp: String, bearerToken: String) =
        withContext(Dispatchers.IO) {
            try {
                if (hostIp.isNotBlank() && port in 1..65535 && certFp.length == 64) {
                    BridgeHttpClient(hostIp, port, certFp).disconnectSession(bearerToken)
                }
            } catch (e: Exception) {
                // Best effort remote notification; local material is cleared.
            } finally {
                storage.clear()
            }
        }

    private suspend fun recoverUntilApproved(
        initial: PendingPairingRecovery,
        onRecovering: (Int) -> Unit
    ): SessionCredentials {
        var record = initial
        var attempt = 0
        if (record.isExpired(nowSeconds())) {
            recoveryStore.clear()
            throw PairingNetworkException("pairing_expired", "Pairing recovery window expired")
        }

        while (true) {
            currentCoroutineContext().ensureActive()
            if (nowSeconds() >= record.deadlineEpochSeconds) {
                recoveryStore.clear()
                throw PairingNetworkException("pairing_expired", "Pairing recovery window expired")
            }

            val sequence = record.nextPollSequence
            val advanced = record.withNextPollSequence(sequence + 1L)
            if (!recoveryStore.save(advanced)) {
                throw PairingNetworkException(
                    "pairing_recovery_unavailable",
                    "Could not persist the next recovery sequence"
                )
            }
            record = advanced

            val nonce = ByteArray(16).also(secureRandom::nextBytes)
            val timestamp = wallClockMillis().toDouble() / 1000.0
            val canonical = CanonicalPollRequest.build(
                version = PROTOCOL_VERSION,
                sessionId = record.sessionId,
                sequence = sequence,
                requestNonce = nonce,
                timestamp = timestamp
            )
            val signature = PollSigner.computeSignature(record.kPoll, canonical)

            val transport = transportFactory.create(
                record.hostIp,
                record.port,
                record.certFingerprint
            )
            try {
                attempt += 1
                onRecovering(attempt)
                val raw = transport.poll(
                    PairingPollRequest(
                        hostIp = record.hostIp,
                        port = record.port,
                        certFingerprint = record.certFingerprint,
                        sessionId = record.sessionId,
                        kPoll = record.kPoll,
                        sequence = sequence,
                        requestNonce = nonce,
                        timestamp = timestamp
                    )
                )
                currentCoroutineContext().ensureActive()

                val verified = try {
                    PairingResponseVerifier.verify(
                        responseJson = raw,
                        kPoll = record.kPoll,
                        expectedContext = "poll",
                        sessionId = record.sessionId,
                        sequence = sequence,
                        requestNonce = nonce
                    )
                } catch (e: PairingNetworkException) {
                    recoveryStore.clear()
                    throw PairingNetworkException(
                        "pairing_response_invalid",
                        e.message ?: "Pairing recovery response failed verification"
                    )
                }

                when (verified.status) {
                    "pending_approval" -> {
                        // Keep waiting for the desktop decision.
                    }
                    "approved" -> {
                        val token = verified.sessionToken
                            ?: throw PairingNetworkException(
                                "missing_credential",
                                "Approved recovery response did not include a session token"
                            )
                        return persistVerifiedCredentials(
                            recovery = record,
                            token = token,
                            expiresIn = verified.expiresIn,
                            expectedDeviceId = verified.deviceId
                        )
                    }
                    else -> {
                        recoveryStore.clear()
                        throw PairingNetworkException(
                            "unexpected_recovery_status",
                            "Pairing recovery returned an unexpected status"
                        )
                    }
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: PairingNetworkException) {
                if (isTerminalPollFailure(e.code)) {
                    recoveryStore.clear()
                    throw when (e.code) {
                        "rejected_by_desktop" -> PairingNetworkException(
                            "pairing_rejected",
                            "Pairing was rejected on the desktop"
                        )
                        "pairing_response_invalid" -> e
                        "poll_replay_rejected", "rate_limited" -> e
                        else -> PairingNetworkException(
                            "pairing_expired",
                            "Pairing recovery is no longer available"
                        )
                    }
                }
                if (!isRetryablePollFailure(e.code)) {
                    recoveryStore.clear()
                    throw PairingNetworkException(
                        "pairing_recovery_failed",
                        e.message ?: "Pairing recovery failed"
                    )
                }
            } catch (e: IOException) {
                // Transient loss of Wi-Fi/TLS; the next attempt uses a fresh
                // sequence and nonce. The original handshake is never replayed.
            } finally {
                transport.close()
            }

            if (nowSeconds() >= record.deadlineEpochSeconds) {
                recoveryStore.clear()
                throw PairingNetworkException("pairing_expired", "Pairing recovery window expired")
            }
            delayFn(pollDelayMs)
        }
    }

    private fun persistVerifiedCredentials(
        recovery: PendingPairingRecovery,
        token: String,
        expiresIn: Long,
        expectedDeviceId: String = recovery.deviceId
    ): SessionCredentials {
        if (expectedDeviceId != recovery.deviceId) {
            recoveryStore.clear()
            throw PairingNetworkException(
                "device_id_mismatch",
                "Signed recovery response named a different device"
            )
        }
        val tokenHash = PendingPairingRecovery.bearerTokenHash(token)
        if (!MessageDigest.isEqual(tokenHash, recovery.bearerTokenSha256)) {
            recoveryStore.clear()
            throw PairingNetworkException(
                "credential_mismatch",
                "Recovered session token does not match the paired credential hash"
            )
        }
        if (expiresIn !in 0L..MAX_SESSION_EXPIRY_SECONDS) {
            recoveryStore.clear()
            throw PairingNetworkException(
                "invalid_response_expiry",
                "Recovery response expiry is out of bounds"
            )
        }

        val credentials = SessionCredentials(
            sessionId = recovery.sessionId,
            deviceId = recovery.deviceId,
            bearerToken = token,
            upstreamKey = recovery.upstreamKey,
            downstreamKey = recovery.downstreamKey,
            expiresAt = nowSeconds() + expiresIn,
            nextSequence = 1L,
            hostIp = recovery.hostIp,
            port = recovery.port,
            certFingerprint = recovery.certFingerprint
        )
        if (!storage.saveCredentials(credentials)) {
            // Keep recovery state so a later poll can deliver the credential
            // again while the desktop recovery window is still open.
            throw PairingNetworkException(
                "credential_persist_failed",
                "Verified credentials could not be stored"
            )
        }
        recoveryStore.clear()
        return credentials
    }

    private fun isRecoverableHandshakeFailure(error: Throwable): Boolean {
        if (error is PairingNetworkException) {
            return when {
                error.code == "approval_timed_out" -> true
                error.code == "token_already_consumed" -> true
                error.code.startsWith("http_error_5") -> true
                error.code.startsWith("http_error_") -> false
                else -> false
            }
        }
        // Socket resets, TLS EOF, timeouts after the request may have reached
        // the desktop are all ambiguous and must be recovered by polling.
        return error is IOException
    }

    private fun isTerminalPollFailure(code: String): Boolean =
        code in setOf(
            "invalid_poll_auth",
            "rejected_by_desktop",
            "polling_window_closed",
            "pairing_response_invalid"
        )

    private fun isRetryablePollFailure(code: String): Boolean =
        code == "poll_replay_rejected" ||
            code == "rate_limited" ||
            code.startsWith("http_error_5")

    private fun asPairingException(error: Throwable): PairingNetworkException {
        if (error is PairingNetworkException) return error
        if (error is CancellationException) throw error
        return PairingNetworkException(
            "pairing_failed",
            error.message ?: "Pairing failed"
        )
    }
}
