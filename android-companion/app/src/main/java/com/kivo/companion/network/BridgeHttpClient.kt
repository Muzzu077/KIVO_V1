package com.kivo.companion.network

import com.kivo.companion.crypto.CanonicalPollRequest
import com.kivo.companion.crypto.PROTOCOL_VERSION
import com.kivo.companion.crypto.PollSigner
import com.kivo.companion.crypto.TlsPinnerFactory
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.Base64
import java.util.concurrent.TimeUnit

class PairingNetworkException(val code: String, message: String) : IOException(message)

/**
 * Strict TLS-pinned HTTP client communicating with KIVO desktop bridge server.
 */
class BridgeHttpClient(
    private val hostIp: String,
    private val port: Int,
    private val certFingerprintHex: String
) {
    private val baseUrl = "https://$hostIp:$port"
    private val jsonMediaType = "application/json; charset=utf-8".toMediaType()

    private val client: OkHttpClient = run {
        val (socketFactory, trustManager, hostnameVerifier) =
            TlsPinnerFactory.createTlsComponents(certFingerprintHex, hostIp)

        OkHttpClient.Builder()
            .sslSocketFactory(socketFactory, trustManager)
            .hostnameVerifier(hostnameVerifier)
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(70, TimeUnit.SECONDS) // Accommodates 60s desktop approval wait
            .writeTimeout(10, TimeUnit.SECONDS)
            .followRedirects(false)
            .followSslRedirects(false)
            .retryOnConnectionFailure(false)
            .build()
    }

    /**
     * Submits client handshake and awaits desktop manual approval.
     */
    fun submitHandshake(
        version: String,
        sessionId: String,
        clientId: String,
        deviceName: String,
        clientNonceB64: String,
        clientProofB64: String
    ): JSONObject {
        val payload = JSONObject().apply {
            put("version", version)
            put("session_id", sessionId)
            put("client_id", clientId)
            put("device_name", deviceName)
            put("client_nonce_b64", clientNonceB64)
            put("client_proof_b64", clientProofB64)
        }

        val request = Request.Builder()
            .url("$baseUrl/api/v1/pair/handshake")
            .post(payload.toString().toRequestBody(jsonMediaType))
            .build()

        client.newCall(request).execute().use { response ->
            val bodyString = response.body?.string().orEmpty()

            when (response.code) {
                200 -> {
                    return JSONObject(bodyString)
                }
                401 -> throw PairingNetworkException("invalid_proof", "Cryptographic proof rejected by desktop")
                403 -> throw PairingNetworkException("rejected_by_desktop", "Pairing rejected by user on desktop")
                408 -> throw PairingNetworkException("approval_timed_out", "Pairing approval timed out on desktop")
                409 -> throw PairingNetworkException("token_already_consumed", "Pairing token was already consumed")
                429 -> throw PairingNetworkException("rate_limited", "Rate limit reached. Please wait before retrying")
                else -> throw PairingNetworkException(
                    "http_error_${response.code}",
                    "Desktop returned HTTP ${response.code}: $bodyString"
                )
            }
        }
    }

    /**
     * Performs a fresh, signed pairing-status poll and returns the raw JSON.
     * Callers must verify the V2 transcript before trusting any field.
     */
    fun pollPairingStatusRaw(
        sessionId: String,
        kPoll: ByteArray,
        sequence: Long,
        requestNonce: ByteArray,
        timestamp: Double
    ): JSONObject {
        val canonicalRequest = CanonicalPollRequest.build(
            version = PROTOCOL_VERSION,
            sessionId = sessionId,
            sequence = sequence,
            requestNonce = requestNonce,
            timestamp = timestamp
        )
        val signature = PollSigner.computeSignature(kPoll, canonicalRequest)

        val payload = JSONObject().apply {
            put("session_id", sessionId)
            put("sequence", sequence)
            put(
                "request_nonce_b64",
                Base64.getUrlEncoder().withoutPadding().encodeToString(requestNonce)
            )
            put("timestamp", timestamp)
            put("signature_hex", signature.toHex())
        }

        val request = Request.Builder()
            .url("$baseUrl/api/v1/pair/status")
            .post(payload.toString().toRequestBody(jsonMediaType))
            .build()

        client.newCall(request).execute().use { response ->
            val bodyString = response.body?.string().orEmpty()
            when (response.code) {
                200 -> return JSONObject(bodyString)
                401 -> throw PairingNetworkException(
                    "invalid_poll_auth",
                    "Fresh poll authentication was rejected"
                )
                403 -> throw PairingNetworkException(
                    "rejected_by_desktop",
                    "Pairing rejected by user on desktop"
                )
                409 -> throw PairingNetworkException(
                    "poll_replay_rejected",
                    "Poll request was rejected as a replay"
                )
                410 -> throw PairingNetworkException(
                    "polling_window_closed",
                    "Pairing approval recovery window has closed"
                )
                429 -> throw PairingNetworkException(
                    "rate_limited",
                    "Polling rate limit reached"
                )
                else -> throw PairingNetworkException(
                    "http_error_${response.code}",
                    "Server returned HTTP ${response.code}"
                )
            }
        }
    }

    /**
     * Performs a fresh, signed pairing-status poll and verifies the V2 response
     * transcript before returning any credential.
     */
    fun pollPairingStatus(
        sessionId: String,
        kPoll: ByteArray,
        sequence: Long,
        requestNonce: ByteArray,
        timestamp: Double
    ): VerifiedPairingResponse {
        val body = pollPairingStatusRaw(sessionId, kPoll, sequence, requestNonce, timestamp)
        return PairingResponseVerifier.verify(
            responseJson = body,
            kPoll = kPoll,
            expectedContext = "poll",
            sessionId = sessionId,
            sequence = sequence,
            requestNonce = requestNonce
        )
    }

    private fun ByteArray.toHex(): String {
        val out = StringBuilder(size * 2)
        for (byte in this) {
            out.append(String.format("%02x", byte.toInt() and 0xFF))
        }
        return out.toString()
    }

    /**
     * Heartbeat probe.
     */
    fun ping(bearerToken: String): Boolean {
        val request = Request.Builder()
            .url("$baseUrl/api/v1/ping")
            .header("Authorization", "Bearer $bearerToken")
            .get()
            .build()

        return try {
            client.newCall(request).execute().use { response ->
                response.isSuccessful
            }
        } catch (e: Exception) {
            false
        }
    }

    /**
     * Disconnects / unpairs session.
     */
    fun disconnectSession(bearerToken: String): Boolean {
        val request = Request.Builder()
            .url("$baseUrl/api/v1/session")
            .header("Authorization", "Bearer $bearerToken")
            .delete()
            .build()

        return try {
            client.newCall(request).execute().use { response ->
                response.isSuccessful
            }
        } catch (e: Exception) {
            false
        }
    }
}
