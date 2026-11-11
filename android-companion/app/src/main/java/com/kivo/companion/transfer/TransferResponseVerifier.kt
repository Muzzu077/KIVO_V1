package com.kivo.companion.transfer

import org.json.JSONObject
import org.json.JSONTokener
import java.security.MessageDigest

data class VerifiedTransferResponse(
    val httpStatus: Int,
    val context: String,
    val state: String,
    val transferId: String,
    val payload: JSONObject
) {
    val ok: Boolean get() = httpStatus in 200..299
    val errorCode: String? get() = payload.optString("error_code").ifEmpty { null }
    val errorMessage: String? get() = payload.optString("error_message").ifEmpty { null }
}

/**
 * Verifies the complete signed desktop response envelope before any field is
 * trusted. The MAC binds the protocol version, response context, session id,
 * HTTP status, request sequence, request nonce, transfer state, transfer id,
 * payload length, and the SHA-256 of the raw payload bytes.
 *
 * An unsigned or malformed response never updates progress.
 */
object TransferResponseVerifier {
    const val MAX_PAYLOAD_BYTES = 64 * 1024
    private val HEX_LOWER = Regex("^[0-9a-f]{64}$")
    private val BASE64_STANDARD = Regex("^[A-Za-z0-9+/]+={0,2}$")

    fun verify(
        envelope: JSONObject,
        downstreamKey: ByteArray,
        expectedContext: String,
        sessionId: String,
        sequence: Long,
        requestNonce: ByteArray,
        actualHttpStatus: Int,
        maxPayloadBytes: Int = MAX_PAYLOAD_BYTES
    ): VerifiedTransferResponse {
        require(maxPayloadBytes > 0) { "maxPayloadBytes must be positive" }
        if (!envelope.optBoolean("ok", false)) {
            // `ok` is itself authenticated by the MAC below; this early check
            // only prevents unsigned error objects from being treated as success.
            if (!envelope.has("sig")) {
                throw TransferProtocolException(
                    "unauthenticated_error",
                    "Desktop returned an unsigned error response"
                )
            }
        }

        if (envelope.optString("v", "") != TransferCrypto.PROTOCOL_VERSION) {
            throw TransferProtocolException("invalid_response_version", "Response protocol version mismatch")
        }
        val context = envelope.optString("ctx", "")
        if (context != expectedContext) {
            throw TransferProtocolException("invalid_response_context", "Response context mismatch")
        }
        if (envelope.optString("sid", "") != sessionId) {
            throw TransferProtocolException("invalid_response_session", "Response session mismatch")
        }
        if (envelope.optLong("seq", -1L) != sequence) {
            throw TransferProtocolException("invalid_response_sequence", "Response sequence mismatch")
        }

        val nonceB64 = envelope.optString("nonce_b64", "")
        val responseNonce = try {
            TransferCrypto.decodeNonceB64(nonceB64)
        } catch (e: IllegalArgumentException) {
            throw TransferProtocolException("invalid_response_nonce", "Response nonce is malformed")
        }
        if (!MessageDigest.isEqual(responseNonce, requestNonce)) {
            throw TransferProtocolException("invalid_response_nonce", "Response nonce does not match the request")
        }

        val httpStatus = envelope.optInt("http_status", -1)
        if (httpStatus != actualHttpStatus || httpStatus !in 100..599) {
            throw TransferProtocolException(
                "invalid_response_http_status",
                "Signed HTTP status does not match the transport response"
            )
        }
        if (envelope.optBoolean("ok", false) != (httpStatus in 200..299)) {
            throw TransferProtocolException("invalid_response_ok", "Response ok flag contradicts HTTP status")
        }

        val state = envelope.optString("state", "")
        if (state.isEmpty() || state.length > 64) {
            throw TransferProtocolException("invalid_response_state", "Response state is invalid")
        }
        val transferId = envelope.optString("tid", "")
        // Upload transfers use `tr_` + 24 hex, while a download response names
        // its opaque `tkt_` + 64 hex capability. Both are bounded here; the
        // upload/download engines still validate the exact expected identity.
        if (transferId.length > 68) {
            throw TransferProtocolException("invalid_response_transfer_id", "Response transfer id is invalid")
        }

        val payloadB64 = envelope.optString("payload_b64", "")
        if (payloadB64.isEmpty() || payloadB64.length > maxPayloadBytes * 2) {
            throw TransferProtocolException("invalid_response_payload", "Response payload is missing or too large")
        }
        if (!BASE64_STANDARD.matches(payloadB64)) {
            throw TransferProtocolException("invalid_response_payload", "Response payload is not standard base64")
        }
        val payloadBytes = try {
            java.util.Base64.getDecoder().decode(payloadB64)
        } catch (e: IllegalArgumentException) {
            throw TransferProtocolException("invalid_response_payload", "Response payload could not be decoded")
        }
        if (payloadBytes.size > maxPayloadBytes) {
            throw TransferProtocolException("invalid_response_payload", "Response payload is too large")
        }

        val payloadHashHex = envelope.optString("payload_sha256", "")
        if (!HEX_LOWER.matches(payloadHashHex)) {
            throw TransferProtocolException("invalid_response_payload_hash", "Payload hash is malformed")
        }
        val payloadHash = TransferCrypto.hexToBytes(payloadHashHex, 32)
            ?: throw TransferProtocolException("invalid_response_payload_hash", "Payload hash is malformed")
        val actualHash = TransferCrypto.sha256(payloadBytes)
        if (!MessageDigest.isEqual(actualHash, payloadHash)) {
            throw TransferProtocolException("invalid_response_payload_hash", "Payload hash mismatch")
        }

        val signatureHex = envelope.optString("sig", "")
        if (!HEX_LOWER.matches(signatureHex)) {
            throw TransferProtocolException("invalid_response_signature", "Response signature is malformed")
        }
        val signature = TransferCrypto.hexToBytes(signatureHex, 32)
            ?: throw TransferProtocolException("invalid_response_signature", "Response signature is malformed")
        if (!TransferCrypto.RESPONSE_CONTEXTS.contains(context)) {
            throw TransferProtocolException("invalid_response_context", "Unknown response context")
        }

        val canonical = TransferCrypto.buildCanonicalResponse(
            version = TransferCrypto.PROTOCOL_VERSION,
            responseContext = context,
            sessionId = sessionId,
            httpStatus = httpStatus,
            sequence = sequence,
            nonce = responseNonce,
            state = state,
            transferId = transferId,
            payloadLength = payloadBytes.size.toLong(),
            payloadSha256 = payloadHash
        )
        if (!TransferCrypto.verifyResponseSignature(downstreamKey, canonical, signature)) {
            throw TransferProtocolException("invalid_response_signature", "Response MAC verification failed")
        }

        val payload = parseStrictObject(payloadBytes)

        val payloadState = payload.optString("state", "")
        if (state != "error") {
            if (payloadState.isNotEmpty() && payloadState != state) {
                throw TransferProtocolException(
                    "invalid_response_payload_state",
                    "Payload state does not match the signed envelope state"
                )
            }
        }
        val payloadTransferId = payload.optString("transfer_id", "")
        if (payloadTransferId.isNotEmpty() && transferId.isNotEmpty() && payloadTransferId != transferId) {
            throw TransferProtocolException(
                "invalid_response_payload_transfer",
                "Payload transfer id does not match the signed envelope transfer id"
            )
        }

        return VerifiedTransferResponse(
            httpStatus = httpStatus,
            context = context,
            state = state,
            transferId = transferId,
            payload = payload
        )
    }

    private fun parseStrictObject(bytes: ByteArray): JSONObject {
        val text = bytes.toString(Charsets.UTF_8)
        if (text.isBlank()) {
            throw TransferProtocolException("invalid_response_payload", "Response payload is empty")
        }
        val tokener = JSONTokener(text)
        val value = try {
            tokener.nextValue()
        } catch (e: Exception) {
            throw TransferProtocolException("invalid_response_payload", "Response payload is not valid JSON")
        }
        if (value !is JSONObject) {
            throw TransferProtocolException("invalid_response_payload", "Response payload is not a JSON object")
        }
        val trailing = tokener.nextClean()
        if (trailing != 0.toChar()) {
            throw TransferProtocolException("invalid_response_payload", "Response payload has trailing data")
        }
        return value
    }
}
