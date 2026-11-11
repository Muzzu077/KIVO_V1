package com.kivo.companion.pairing

import com.kivo.companion.network.BridgeHttpClient
import org.json.JSONObject

data class PairingHandshakeRequest(
    val hostIp: String,
    val port: Int,
    val certFingerprint: String,
    val version: String,
    val sessionId: String,
    val clientId: String,
    val deviceName: String,
    val clientNonceB64: String,
    val clientProofB64: String
)

data class PairingPollRequest(
    val hostIp: String,
    val port: Int,
    val certFingerprint: String,
    val sessionId: String,
    val kPoll: ByteArray,
    val sequence: Long,
    val requestNonce: ByteArray,
    val timestamp: Double
)

/**
 * Narrow transport boundary so pairing-recovery state transitions can be
 * exercised deterministically without a network or a desktop process.
 */
interface PairingTransport {
    fun handshake(request: PairingHandshakeRequest): JSONObject
    fun poll(request: PairingPollRequest): JSONObject
    fun close()
}

interface PairingTransportFactory {
    fun create(hostIp: String, port: Int, certFingerprint: String): PairingTransport
}

class OkHttpPairingTransportFactory : PairingTransportFactory {
    override fun create(hostIp: String, port: Int, certFingerprint: String): PairingTransport =
        OkHttpPairingTransport(BridgeHttpClient(hostIp, port, certFingerprint))
}

class OkHttpPairingTransport(
    private val client: BridgeHttpClient
) : PairingTransport {
    override fun handshake(request: PairingHandshakeRequest): JSONObject =
        client.submitHandshake(
            version = request.version,
            sessionId = request.sessionId,
            clientId = request.clientId,
            deviceName = request.deviceName,
            clientNonceB64 = request.clientNonceB64,
            clientProofB64 = request.clientProofB64
        )

    override fun poll(request: PairingPollRequest): JSONObject =
        client.pollPairingStatusRaw(
            sessionId = request.sessionId,
            kPoll = request.kPoll,
            sequence = request.sequence,
            requestNonce = request.requestNonce,
            timestamp = request.timestamp
        )

    override fun close() {
        // OkHttp does not expose a lifecycle close; the client is per attempt
        // and its connection pool is released by normal GC/eviction.
    }
}
