package com.kivo.companion.pairing

import com.kivo.companion.storage.KeystoreSecretStorage
import org.json.JSONObject
import java.security.MessageDigest
import java.util.Base64

/**
 * Minimum durable state needed to finish a pairing whose blocking handshake
 * response was lost.
 *
 * This record intentionally contains no `T_pair`, no server nonce, and no
 * bearer token. It stores the already-derived directional keys and `K_poll`
 * because the original pairing secret is discarded immediately after the
 * single handshake attempt; without them, a process restart could not verify
 * the recovery response. Credentials are not written to the session store until
 * a V2 response has been authenticated.
 */
data class PendingPairingRecovery(
    val sessionId: String,
    val hostIp: String,
    val port: Int,
    val certFingerprint: String,
    val kPoll: ByteArray,
    val upstreamKey: ByteArray,
    val downstreamKey: ByteArray,
    val contextBytes: ByteArray,
    val sasCode: String,
    val deviceId: String,
    val bearerTokenSha256: ByteArray,
    val nextPollSequence: Long,
    val deadlineEpochSeconds: Long,
    val createdAtEpochSeconds: Long
) {
    fun isExpired(nowSeconds: Long = System.currentTimeMillis() / 1000): Boolean =
        nowSeconds >= deadlineEpochSeconds

    fun withNextPollSequence(sequence: Long): PendingPairingRecovery =
        copy(nextPollSequence = sequence)

    fun toJson(): JSONObject = JSONObject().apply {
        put("record_type", "pairing_recovery")
        put("record_version", 1)
        put("sessionId", sessionId)
        put("hostIp", hostIp)
        put("port", port)
        put("certFingerprint", certFingerprint)
        put("kPollB64", b64(kPoll))
        put("upstreamKeyB64", b64(upstreamKey))
        put("downstreamKeyB64", b64(downstreamKey))
        put("contextB64", b64(contextBytes))
        put("sasCode", sasCode)
        put("deviceId", deviceId)
        put("credentialHashB64", b64(bearerTokenSha256))
        put("nextPollSequence", nextPollSequence)
        put("deadlineEpochSeconds", deadlineEpochSeconds)
        put("createdAtEpochSeconds", createdAtEpochSeconds)
    }

    companion object {
        private val SESSION_PATTERN = Regex("^sess_[A-Za-z0-9_.-]{1,63}$")
        private val HEX64 = Regex("^[0-9a-fA-F]{64}$")
        private val SAS_PATTERN = Regex("^\\d{3} \\d{3}$")

        fun fromJson(json: JSONObject): PendingPairingRecovery? {
            return try {
                if (json.optString("record_type") != "pairing_recovery") return null
                if (json.optInt("record_version", 0) != 1) return null
                val sessionId = json.optString("sessionId", "")
                val hostIp = json.optString("hostIp", "")
                val port = json.optInt("port", 0)
                val certFingerprint = json.optString("certFingerprint", "")
                val kPoll = unb64(json.optString("kPollB64", ""))
                val kUp = unb64(json.optString("upstreamKeyB64", ""))
                val kDown = unb64(json.optString("downstreamKeyB64", ""))
                val context = unb64(json.optString("contextB64", ""))
                val sasCode = json.optString("sasCode", "")
                val deviceId = json.optString("deviceId", "")
                val tokenHash = unb64(json.optString("credentialHashB64", ""))
                val nextSequence = json.optLong("nextPollSequence", 0L)
                val deadline = json.optLong("deadlineEpochSeconds", 0L)
                val createdAt = json.optLong("createdAtEpochSeconds", 0L)

                if (!SESSION_PATTERN.matches(sessionId)) return null
                if (hostIp.isEmpty() || hostIp.length > 255 || port !in 1..65535) return null
                if (!HEX64.matches(certFingerprint)) return null
                if (kPoll?.size != 32 || kUp?.size != 32 || kDown?.size != 32) return null
                if (context == null || context.isEmpty() || context.size > 4096) return null
                if (!SAS_PATTERN.matches(sasCode)) return null
                if (deviceId.isEmpty() || deviceId.length > 64) return null
                if (tokenHash?.size != 32) return null
                if (nextSequence < 1L || deadline <= 0L || createdAt <= 0L) return null

                PendingPairingRecovery(
                    sessionId = sessionId,
                    hostIp = hostIp,
                    port = port,
                    certFingerprint = certFingerprint,
                    kPoll = kPoll,
                    upstreamKey = kUp,
                    downstreamKey = kDown,
                    contextBytes = context,
                    sasCode = sasCode,
                    deviceId = deviceId,
                    bearerTokenSha256 = tokenHash,
                    nextPollSequence = nextSequence,
                    deadlineEpochSeconds = deadline,
                    createdAtEpochSeconds = createdAt
                )
            } catch (e: Exception) {
                null
            }
        }

        fun bearerTokenHash(token: String): ByteArray =
            MessageDigest.getInstance("SHA-256").digest(token.toByteArray(Charsets.UTF_8))

        private fun b64(value: ByteArray): String = Base64.getEncoder().encodeToString(value)

        private fun unb64(value: String): ByteArray? = try {
            if (value.isEmpty() || value.length > 16384) null
            else Base64.getDecoder().decode(value)
        } catch (e: IllegalArgumentException) {
            null
        }
    }
}

class PairingRecoveryStore(
    private val storage: KeystoreSecretStorage
) {
    @Synchronized
    fun save(record: PendingPairingRecovery): Boolean = storage.savePairingRecovery(record.toJson())

    @Synchronized
    fun load(): PendingPairingRecovery? {
        val json = storage.loadPairingRecovery() ?: return null
        val record = PendingPairingRecovery.fromJson(json)
        if (record == null) storage.clearPairingRecovery()
        return record
    }

    @Synchronized
    fun clear() {
        storage.clearPairingRecovery()
    }
}
