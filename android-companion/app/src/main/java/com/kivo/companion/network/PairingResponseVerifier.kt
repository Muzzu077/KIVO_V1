package com.kivo.companion.network

import com.kivo.companion.crypto.CanonicalPollResponse
import com.kivo.companion.crypto.PROTOCOL_VERSION
import com.kivo.companion.crypto.PollSigner
import org.json.JSONObject
import java.security.MessageDigest

/**
 * Result of a cryptographically verified pairing status response.
 */
data class VerifiedPairingResponse(
    val status: String,
    val sessionToken: String?,
    val expiresIn: Long,
    val deviceId: String
)

/**
 * Verifies the Stage 13.3.2.3 V2 response transcript before any credential is
 * trusted or persisted. The MAC binds every field the client consumes:
 * status, expiry, device id, credential-delivery state, and the SHA-256 of the
 * returned bearer token.
 */
object PairingResponseVerifier {
    private const val MAX_EXPIRES_IN_SECONDS = 86_400L

    fun verify(
        responseJson: JSONObject,
        kPoll: ByteArray,
        expectedContext: String,
        sessionId: String,
        sequence: Long,
        requestNonce: ByteArray
    ): VerifiedPairingResponse {
        if (!responseJson.optBoolean("ok", false)) {
            throw PairingNetworkException(
                "invalid_response",
                "Pairing response did not report ok=true"
            )
        }

        val status = responseJson.optString("status", "")
        if (status != "approved" && status != "pending_approval") {
            throw PairingNetworkException(
                "invalid_response_status",
                "Unexpected pairing response status"
            )
        }

        if (responseJson.optString("response_context", "") != expectedContext) {
            throw PairingNetworkException(
                "invalid_response_context",
                "Pairing response context mismatch"
            )
        }

        val httpStatus = responseJson.optInt("http_status", -1)
        if (httpStatus != 200) {
            throw PairingNetworkException(
                "invalid_response_http_status",
                "Pairing response HTTP status mismatch"
            )
        }

        if (responseJson.optString("session_id", "") != sessionId) {
            throw PairingNetworkException(
                "invalid_response_session",
                "Pairing response session id mismatch"
            )
        }

        if (responseJson.optLong("sequence", -1L) != sequence) {
            throw PairingNetworkException(
                "invalid_response_sequence",
                "Pairing response sequence does not match the authenticated request"
            )
        }

        val expiresIn = responseJson.optLong("expires_in", -1L)
        if (expiresIn < 0L || expiresIn > MAX_EXPIRES_IN_SECONDS) {
            throw PairingNetworkException(
                "invalid_response_expiry",
                "Pairing response expiry out of bounds"
            )
        }

        val deviceId = responseJson.optString("device_id", "")
        if (deviceId.isEmpty() || deviceId.length > 64) {
            throw PairingNetworkException(
                "invalid_response_device",
                "Pairing response device id invalid"
            )
        }

        val credentialState = responseJson.optString("credential_state", "")
        val credentialHashHex = responseJson.optString("credential_sha256", "")
        val token = responseJson.optString("session_token", "")

        val credentialHash: ByteArray
        when (credentialState) {
            "none" -> {
                if (status != "pending_approval") {
                    throw PairingNetworkException(
                        "invalid_credential_state",
                        "Credential state none is only valid while pending"
                    )
                }
                if (token.isNotEmpty() || credentialHashHex.isNotEmpty()) {
                    throw PairingNetworkException(
                        "invalid_credential_state",
                        "Pending response unexpectedly carried credentials"
                    )
                }
                credentialHash = ByteArray(0)
            }

            "delivered" -> {
                if (status != "approved") {
                    throw PairingNetworkException(
                        "invalid_credential_state",
                        "Credential delivery requires approved status"
                    )
                }
                if (token.isEmpty()) {
                    throw PairingNetworkException(
                        "missing_credential",
                        "Approved response did not contain a session token"
                    )
                }
                credentialHash = hexToBytes(credentialHashHex)
                if (credentialHash.size != 32) {
                    throw PairingNetworkException(
                        "invalid_credential_hash",
                        "Credential hash must be exactly 32 bytes"
                    )
                }
                val computed = PollSigner.computeCredentialSha256(token)
                if (!MessageDigest.isEqual(computed, credentialHash)) {
                    throw PairingNetworkException(
                        "credential_hash_mismatch",
                        "Session token does not match the signed credential hash"
                    )
                }
            }

            else -> throw PairingNetworkException(
                "invalid_credential_state",
                "Unknown credential-delivery state"
            )
        }

        val signature = hexToBytes(responseJson.optString("response_sig", ""))
        if (signature.size != 32) {
            throw PairingNetworkException(
                "invalid_response_signature",
                "Response signature must be exactly 32 bytes"
            )
        }

        val canonical = CanonicalPollResponse.build(
            version = PROTOCOL_VERSION,
            responseContext = expectedContext,
            sessionId = sessionId,
            httpStatus = httpStatus,
            sequence = sequence,
            requestNonce = requestNonce,
            status = status,
            expiresIn = expiresIn,
            deviceId = deviceId,
            credentialState = credentialState,
            credentialSha256 = credentialHash
        )

        if (!PollSigner.verifyResponseSignatureV2(kPoll, canonical, signature)) {
            throw PairingNetworkException(
                "invalid_response_signature",
                "Pairing response MAC verification failed"
            )
        }

        return VerifiedPairingResponse(
            status = status,
            sessionToken = token.ifEmpty { null },
            expiresIn = expiresIn,
            deviceId = deviceId
        )
    }

    private fun hexToBytes(hex: String): ByteArray {
        if (hex.length % 2 != 0) return ByteArray(0)
        if (hex.isEmpty()) return ByteArray(0)
        return try {
            ByteArray(hex.length / 2) { index ->
                hex.substring(index * 2, index * 2 + 2).toInt(16).toByte()
            }
        } catch (e: NumberFormatException) {
            ByteArray(0)
        }
    }
}
