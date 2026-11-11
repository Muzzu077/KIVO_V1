package com.kivo.companion.transfer

import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.util.Base64
import java.util.Locale
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * Canonical Stage 13.3.3.1 transfer transcripts.
 *
 * These encodings are byte-for-byte identical to `kivo/bridge/transfer_crypto.py`.
 * `K_up` authenticates Android-to-desktop requests; `K_down` authenticates
 * desktop-to-Android responses. The bearer token identifies a session but is
 * never used as a MAC key.
 */
object TransferCrypto {
    const val PROTOCOL_VERSION = "1"
    const val NONCE_BYTES = 16

    private val REQUEST_LABEL = "KIVO_BRIDGE_TRANSFER_REQUEST_V1\u0000".toByteArray(Charsets.UTF_8)
    private val RESPONSE_LABEL = "KIVO_BRIDGE_TRANSFER_RESPONSE_V1\u0000".toByteArray(Charsets.UTF_8)
    private const val HMAC_ALGORITHM = "HmacSHA256"
    private const val MAX_TIMESTAMP = 4_102_444_800.0

    val RESPONSE_CONTEXTS = setOf(
        "upload_init",
        "upload_status",
        "upload_chunk",
        "upload_cancel",
        "upload_decision",
        "download_init",
        "download_status",
        "download_chunk",
        "download_complete",
        "download_cancel"
    )

    private val BASE64URL_CHARS =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_".toSet()
    private val HEX_CHARS = "0123456789abcdefABCDEF".toSet()

    fun packField(data: ByteArray): ByteArray {
        val length = ByteBuffer.allocate(4).order(ByteOrder.BIG_ENDIAN).putInt(data.size).array()
        return length + data
    }

    fun packField(data: String): ByteArray = packField(data.toByteArray(Charsets.UTF_8))

    fun buildCanonicalRequest(
        version: String,
        sessionId: String,
        method: String,
        endpoint: String,
        sequence: Long,
        nonce: ByteArray,
        timestamp: Double,
        contentLength: Long,
        contentSha256: ByteArray
    ): ByteArray {
        require(version.isNotEmpty()) { "version must be non-empty" }
        require(sessionId.isNotEmpty()) { "sessionId must be non-empty" }
        require(method.isNotEmpty() && method == method.uppercase(Locale.ROOT)) {
            "method must be uppercase"
        }
        require(endpoint.startsWith("/")) { "endpoint must be an absolute path" }
        require(sequence in 1L..Long.MAX_VALUE) { "sequence out of range" }
        require(nonce.size == NONCE_BYTES) { "nonce must be exactly 16 bytes" }
        require(timestamp.isFinite() && timestamp in 0.0..MAX_TIMESTAMP) {
            "timestamp out of range"
        }
        require(contentLength >= 0L) { "contentLength out of range" }
        require(contentSha256.size == 32) { "contentSha256 must be exactly 32 bytes" }

        val out = ByteArrayOutputStream()
        out.write(packField(version))
        out.write(packField(sessionId))
        out.write(packField(method))
        out.write(packField(endpoint))
        out.write(ByteBuffer.allocate(8).order(ByteOrder.BIG_ENDIAN).putLong(sequence).array())
        out.write(packField(nonce))
        out.write(ByteBuffer.allocate(8).order(ByteOrder.BIG_ENDIAN).putDouble(timestamp).array())
        out.write(ByteBuffer.allocate(8).order(ByteOrder.BIG_ENDIAN).putLong(contentLength).array())
        out.write(packField(contentSha256))
        return out.toByteArray()
    }

    fun buildCanonicalResponse(
        version: String,
        responseContext: String,
        sessionId: String,
        httpStatus: Int,
        sequence: Long,
        nonce: ByteArray,
        state: String,
        transferId: String,
        payloadLength: Long,
        payloadSha256: ByteArray
    ): ByteArray {
        require(responseContext in RESPONSE_CONTEXTS) { "unknown response context" }
        require(sessionId.isNotEmpty()) { "sessionId must be non-empty" }
        require(httpStatus in 100..599) { "httpStatus out of range" }
        require(sequence in 1L..Long.MAX_VALUE) { "sequence out of range" }
        require(nonce.size == NONCE_BYTES) { "nonce must be exactly 16 bytes" }
        require(state.isNotEmpty()) { "state must be non-empty" }
        require(payloadLength >= 0L) { "payloadLength out of range" }
        require(payloadSha256.size == 32) { "payloadSha256 must be exactly 32 bytes" }

        val out = ByteArrayOutputStream()
        out.write(packField(version))
        out.write(packField(responseContext))
        out.write(packField(sessionId))
        out.write(ByteBuffer.allocate(2).order(ByteOrder.BIG_ENDIAN).putShort(httpStatus.toShort()).array())
        out.write(ByteBuffer.allocate(8).order(ByteOrder.BIG_ENDIAN).putLong(sequence).array())
        out.write(packField(nonce))
        out.write(packField(state))
        out.write(packField(transferId))
        out.write(ByteBuffer.allocate(8).order(ByteOrder.BIG_ENDIAN).putLong(payloadLength).array())
        out.write(packField(payloadSha256))
        return out.toByteArray()
    }

    fun computeRequestSignature(upstreamKey: ByteArray, canonicalRequest: ByteArray): ByteArray =
        hmac(upstreamKey, REQUEST_LABEL, canonicalRequest)

    fun computeResponseSignature(downstreamKey: ByteArray, canonicalResponse: ByteArray): ByteArray =
        hmac(downstreamKey, RESPONSE_LABEL, canonicalResponse)

    fun verifyRequestSignature(
        upstreamKey: ByteArray,
        canonicalRequest: ByteArray,
        receivedSignature: ByteArray
    ): Boolean = MessageDigest.isEqual(
        computeRequestSignature(upstreamKey, canonicalRequest),
        receivedSignature
    )

    fun verifyResponseSignature(
        downstreamKey: ByteArray,
        canonicalResponse: ByteArray,
        receivedSignature: ByteArray
    ): Boolean = MessageDigest.isEqual(
        computeResponseSignature(downstreamKey, canonicalResponse),
        receivedSignature
    )

    fun sha256(data: ByteArray): ByteArray =
        MessageDigest.getInstance("SHA-256").digest(data)

    fun sha256Hex(data: ByteArray): String = toHex(sha256(data))

    fun encodeNonceB64(nonce: ByteArray): String {
        require(nonce.size == NONCE_BYTES) { "nonce must be exactly 16 bytes" }
        return Base64.getUrlEncoder().withoutPadding().encodeToString(nonce)
    }

    fun decodeNonceB64(value: String): ByteArray {
        require(value.isNotEmpty() && value.length <= 64) { "nonce encoding out of range" }
        require(value.all { it in BASE64URL_CHARS }) { "nonce is not unpadded base64url" }
        val raw = try {
            Base64.getUrlDecoder().decode(value)
        } catch (e: IllegalArgumentException) {
            throw IllegalArgumentException("nonce is not valid base64url", e)
        }
        require(raw.size == NONCE_BYTES) { "nonce must be exactly 16 bytes" }
        return raw
    }

    fun toHex(bytes: ByteArray): String {
        val out = StringBuilder(bytes.size * 2)
        for (byte in bytes) {
            val value = byte.toInt() and 0xFF
            out.append(HEX_DIGITS[value ushr 4]).append(HEX_DIGITS[value and 0x0F])
        }
        return out.toString()
    }

    fun hexToBytes(value: String, expectedLength: Int? = null): ByteArray? {
        if (value.length % 2 != 0 || value.any { it !in HEX_CHARS }) return null
        if (expectedLength != null && value.length != expectedLength * 2) return null
        return try {
            ByteArray(value.length / 2) { index ->
                value.substring(index * 2, index * 2 + 2).toInt(16).toByte()
            }
        } catch (e: NumberFormatException) {
            null
        }
    }

    /**
     * Deterministic compact JSON used for the upload-init control body.
     * Python sorts keys, emits UTF-8 without ASCII escaping, and uses the
     * shortest JSON integer form. The server authenticates the exact bytes, so
     * this serializer must remain deterministic.
     */
    fun buildInitBody(filename: String, mime: String?, size: Long, sha256Hex: String): ByteArray {
        require(size >= 0L) { "size must be non-negative" }
        require(sha256Hex.length == 64 && sha256Hex.all { it in HEX_CHARS }) {
            "sha256 must be 64 hexadecimal characters"
        }
        val out = StringBuilder(160)
        out.append('{')
        out.append("\"filename\":").append(jsonString(filename))
        if (!mime.isNullOrEmpty()) {
            out.append(",\"mime\":").append(jsonString(mime))
        }
        out.append(",\"sha256\":").append(jsonString(sha256Hex.lowercase(Locale.ROOT)))
        out.append(",\"size\":").append(size)
        out.append('}')
        return out.toString().toByteArray(Charsets.UTF_8)
    }

    /**
     * Deterministic compact JSON for the download-init control body. The
     * desktop authenticates these exact bytes, so key ordering and separators
     * must match `json.dumps(..., sort_keys=True, separators=(",", ":"))`.
     */
    fun buildDownloadInitBody(ticketId: String): ByteArray {
        require(TICKET_ID_PATTERN.matches(ticketId)) { "ticket id is malformed" }
        return "{\"ticket_id\":${jsonString(ticketId)}}".toByteArray(Charsets.UTF_8)
    }

    val TICKET_ID_PATTERN = Regex("^tkt_[0-9a-f]{64}$")

    fun jsonString(value: String): String {
        val out = StringBuilder(value.length + 2)
        out.append('"')
        for (char in value) {
            when (char) {
                '"' -> out.append("\\\"")
                '\\' -> out.append("\\\\")
                '\b' -> out.append("\\b")
                '\u000C' -> out.append("\\f")
                '\n' -> out.append("\\n")
                '\r' -> out.append("\\r")
                '\t' -> out.append("\\t")
                else -> {
                    if (char.code < 0x20) {
                        out.append("\\u")
                        out.append(String.format(Locale.ROOT, "%04x", char.code))
                    } else {
                        out.append(char)
                    }
                }
            }
        }
        out.append('"')
        return out.toString()
    }

    private fun hmac(key: ByteArray, label: ByteArray, payload: ByteArray): ByteArray {
        val mac = Mac.getInstance(HMAC_ALGORITHM)
        mac.init(SecretKeySpec(key, HMAC_ALGORITHM))
        mac.update(label)
        mac.update(payload)
        return mac.doFinal()
    }

    private const val HEX_DIGITS = "0123456789abcdef"
}
