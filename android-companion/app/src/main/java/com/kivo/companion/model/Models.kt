package com.kivo.companion.model

/**
 * Validated pairing offer received from desktop QR code.
 */
data class PairingOffer(
    val version: String,
    val hostIp: String,
    val port: Int,
    val certFingerprint: String,
    val pairingSecret: ByteArray,
    val sessionId: String,
    val serverNonce: ByteArray,
    val expiresAt: Long
) {
    fun isExpired(nowSeconds: Long = System.currentTimeMillis() / 1000): Boolean {
        return nowSeconds >= expiresAt
    }

    override fun equals(other: Any?): Boolean {
        if (this === other) return true
        if (javaClass != other?.javaClass) return false
        other as PairingOffer
        return sessionId == other.sessionId && certFingerprint == other.certFingerprint
    }

    override fun hashCode(): Int {
        return sessionId.hashCode()
    }
}

/**
 * Established session credentials stored securely on the mobile device.
 *
 * Endpoint identity and the next transfer sequence are persisted alongside the
 * keys so uploads and signed recovery can continue after process death. The
 * sequence is advanced and persisted before any authenticated body is sent.
 */
data class SessionCredentials(
    val sessionId: String,
    val deviceId: String,
    val bearerToken: String,
    val upstreamKey: ByteArray,
    val downstreamKey: ByteArray,
    val expiresAt: Long,
    val nextSequence: Long = 1L,
    val hostIp: String = "",
    val port: Int = 0,
    val certFingerprint: String = ""
) {
    fun isExpired(nowSeconds: Long = System.currentTimeMillis() / 1000): Boolean {
        return nowSeconds >= expiresAt
    }

    fun hasEndpoint(): Boolean =
        hostIp.isNotBlank() && port in 1..65535 && certFingerprint.length == 64

    override fun equals(other: Any?): Boolean {
        if (this === other) return true
        if (javaClass != other?.javaClass) return false
        other as SessionCredentials
        return sessionId == other.sessionId && bearerToken == other.bearerToken
    }

    override fun hashCode(): Int {
        return sessionId.hashCode()
    }
}

/**
 * Sealed class modeling the end-to-end pairing state machine.
 */
sealed class PairingState {
    object Idle : PairingState()
    object Scanning : PairingState()
    data class Connecting(val endpoint: String) : PairingState()
    data class VerifyingSas(val sasCode: String, val desktopName: String) : PairingState()
    data class AwaitingApproval(val sasCode: String) : PairingState()
    data class Recovering(val sasCode: String) : PairingState()
    data class Paired(
        val sessionId: String,
        val hostAddress: String,
        val certFingerprintSuffix: String,
        val expiresAt: Long
    ) : PairingState()
    data class Error(val code: String, val message: String) : PairingState()
}
