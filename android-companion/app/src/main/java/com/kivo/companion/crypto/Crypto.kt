package com.kivo.companion.crypto

import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.util.Base64
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

const val PROTOCOL_VERSION = "1"
const val ALGORITHM_ID = "HMAC-SHA256-HKDF-SAS-V1"

private const val SAS_MODULO = 1_000_000
private const val SAS_UNBIASED_CEILING = 4_294_000_000L

/**
 * Builds an unambiguous length-prefixed binary pairing transcript.
 * Exactly matches Python build_canonical_context() byte-for-byte.
 */
object CanonicalContext {
    fun packField(data: ByteArray): ByteArray {
        val len = ByteBuffer.allocate(4).order(ByteOrder.BIG_ENDIAN).putInt(data.size).array()
        return len + data
    }

    fun packField(data: String): ByteArray {
        return packField(data.toByteArray(Charsets.UTF_8))
    }

    fun build(
        version: String,
        sessionId: String,
        certFingerprint: String,
        serverNonce: ByteArray,
        clientNonce: ByteArray,
        clientId: String,
        algorithmId: String = ALGORITHM_ID
    ): ByteArray {
        val out = ByteArrayOutputStream()
        out.write(packField(version))
        out.write(packField(sessionId))
        out.write(packField(certFingerprint.uppercase()))
        out.write(packField(serverNonce))
        out.write(packField(clientNonce))
        out.write(packField(clientId))
        out.write(packField(algorithmId))
        return out.toByteArray()
    }
}

/**
 * Standard RFC 5869 HKDF-SHA256 implementation.
 */
object Hkdf {
    private const val HMAC_ALGORITHM = "HmacSHA256"
    private const val HASH_LEN = 32

    fun extract(salt: ByteArray, ikm: ByteArray): ByteArray {
        val effectiveSalt = if (salt.isEmpty()) ByteArray(HASH_LEN) else salt
        val mac = Mac.getInstance(HMAC_ALGORITHM)
        mac.init(SecretKeySpec(effectiveSalt, HMAC_ALGORITHM))
        return mac.doFinal(ikm)
    }

    fun expand(prk: ByteArray, info: ByteArray, length: Int): ByteArray {
        val mac = Mac.getInstance(HMAC_ALGORITHM)
        mac.init(SecretKeySpec(prk, HMAC_ALGORITHM))

        val result = ByteArray(length)
        var t = ByteArray(0)
        var offset = 0
        var blockIndex = 1

        while (offset < length) {
            mac.reset()
            if (t.isNotEmpty()) {
                mac.update(t)
            }
            mac.update(info)
            mac.update(blockIndex.toByte())
            t = mac.doFinal()

            val toCopy = minOf(t.size, length - offset)
            System.arraycopy(t, 0, result, offset, toCopy)
            offset += toCopy
            blockIndex++
        }
        return result
    }
}

/**
 * Client proof calculation using HMAC-SHA256.
 */
object PairingProof {
    private const val PROOF_DOMAIN = "KIVO_BRIDGE_CLIENT_PROOF_V1\u0000"

    fun compute(pairingSecret: ByteArray, contextBytes: ByteArray): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(pairingSecret, "HmacSHA256"))
        mac.update(PROOF_DOMAIN.toByteArray(Charsets.UTF_8))
        mac.update(contextBytes)
        return mac.doFinal()
    }

    fun verify(pairingSecret: ByteArray, contextBytes: ByteArray, receivedProof: ByteArray): Boolean {
        val expected = compute(pairingSecret, contextBytes)
        return MessageDigest.isEqual(expected, receivedProof)
    }
}

/**
 * SAS Derivation matching Python derive_sas_code().
 */
object SasDerivation {
    private const val SAS_DOMAIN = "kivo-bridge-sas-v1\u0000"

    fun derive(
        pairingSecret: ByteArray,
        serverNonce: ByteArray,
        contextBytes: ByteArray
    ): Pair<Int, String> {
        var counter = 0
        while (true) {
            val counterBytes = ByteBuffer.allocate(4).order(ByteOrder.BIG_ENDIAN).putInt(counter).array()
            val info = SAS_DOMAIN.toByteArray(Charsets.UTF_8) + counterBytes + contextBytes

            val prk = Hkdf.extract(serverNonce, pairingSecret)
            val derived = Hkdf.expand(prk, info, 4)

            val uint32 = ByteBuffer.wrap(derived).order(ByteOrder.BIG_ENDIAN).int.toLong() and 0xFFFFFFFFL

            if (uint32 < SAS_UNBIASED_CEILING) {
                val sasInt = (uint32 % SAS_MODULO).toInt()
                val rawStr = "%06d".format(sasInt)
                val formatted = "${rawStr.substring(0, 3)} ${rawStr.substring(3)}"
                return Pair(sasInt, formatted)
            }
            counter++
        }
    }
}

data class DerivedSessionKeys(
    val upstreamKey: ByteArray,
    val downstreamKey: ByteArray,
    val bearerToken: String
)

/**
 * Session key derivation matching Python derive_session_keys().
 */
object SessionKeyDerivation {
    fun derive(
        pairingSecret: ByteArray,
        clientNonce: ByteArray,
        contextBytes: ByteArray
    ): DerivedSessionKeys {
        val prkInfo = "kivo-bridge-prk-v1".toByteArray(Charsets.UTF_8)
        val prkRaw = Hkdf.extract(clientNonce, pairingSecret)
        val prk = Hkdf.expand(prkRaw, prkInfo, 32)

        val upInfo = "kivo-session-upstream-v1\u0000".toByteArray(Charsets.UTF_8) + contextBytes
        val downInfo = "kivo-session-downstream-v1\u0000".toByteArray(Charsets.UTF_8) + contextBytes
        val bearerInfo = "kivo-session-bearer-v1\u0000".toByteArray(Charsets.UTF_8) + contextBytes

        val kUp = Hkdf.expand(prk, upInfo, 32)
        val kDown = Hkdf.expand(prk, downInfo, 32)
        val tokenBytes = Hkdf.expand(prk, bearerInfo, 32)

        val b64Token = Base64.getUrlEncoder().withoutPadding().encodeToString(tokenBytes)
        val bearerToken = "sess_tok_$b64Token"

        return DerivedSessionKeys(kUp, kDown, bearerToken)
    }

    fun derivePollKey(
        pairingSecret: ByteArray,
        clientNonce: ByteArray,
        contextBytes: ByteArray
    ): ByteArray {
        val prkInfo = "kivo-bridge-prk-v1".toByteArray(Charsets.UTF_8)
        val prkRaw = Hkdf.extract(clientNonce, pairingSecret)
        val prk = Hkdf.expand(prkRaw, prkInfo, 32)
        val pollInfo = "kivo-pair-poll-v1\u0000".toByteArray(Charsets.UTF_8) + contextBytes
        return Hkdf.expand(prk, pollInfo, 32)
    }
}

/**
 * Builds unambiguous length-prefixed binary encoding of a poll request.
 */
object CanonicalPollRequest {
    fun build(
        version: String,
        sessionId: String,
        sequence: Long,
        requestNonce: ByteArray,
        timestamp: Double,
        method: String = "POST",
        endpoint: String = "/api/v1/pair/status"
    ): ByteArray {
        val out = ByteArrayOutputStream()
        out.write(CanonicalContext.packField(version))
        out.write(CanonicalContext.packField(sessionId))
        out.write(CanonicalContext.packField(method))
        out.write(CanonicalContext.packField(endpoint))
        val seqBytes = ByteBuffer.allocate(8).order(ByteOrder.BIG_ENDIAN).putLong(sequence).array()
        out.write(seqBytes)
        out.write(CanonicalContext.packField(requestNonce))
        val timeBytes = ByteBuffer.allocate(8).order(ByteOrder.BIG_ENDIAN).putDouble(timestamp).array()
        out.write(timeBytes)
        return out.toByteArray()
    }
}

/**
 * Builds unambiguous length-prefixed binary encoding of a status/handshake response.
 * Exactly matches Python build_canonical_poll_response() byte-for-byte.
 */
object CanonicalPollResponse {
    fun build(
        version: String,
        responseContext: String,
        sessionId: String,
        httpStatus: Int,
        sequence: Long,
        requestNonce: ByteArray,
        status: String,
        expiresIn: Long,
        deviceId: String,
        credentialState: String,
        credentialSha256: ByteArray
    ): ByteArray {
        require(httpStatus in 0..0xFFFF) { "httpStatus out of range" }
        require(sequence >= 0) { "sequence must be non-negative" }
        require(expiresIn >= 0) { "expiresIn must be non-negative" }
        require(credentialSha256.isEmpty() || credentialSha256.size == 32) {
            "credentialSha256 must be empty or 32 bytes"
        }

        val out = ByteArrayOutputStream()
        out.write(CanonicalContext.packField(version))
        out.write(CanonicalContext.packField(responseContext))
        out.write(CanonicalContext.packField(sessionId))
        out.write(ByteBuffer.allocate(2).order(ByteOrder.BIG_ENDIAN).putShort(httpStatus.toShort()).array())
        out.write(ByteBuffer.allocate(8).order(ByteOrder.BIG_ENDIAN).putLong(sequence).array())
        out.write(CanonicalContext.packField(requestNonce))
        out.write(CanonicalContext.packField(status))
        out.write(ByteBuffer.allocate(8).order(ByteOrder.BIG_ENDIAN).putLong(expiresIn).array())
        out.write(CanonicalContext.packField(deviceId))
        out.write(CanonicalContext.packField(credentialState))
        out.write(CanonicalContext.packField(credentialSha256))
        return out.toByteArray()
    }
}

/**
 * Signs and verifies per-request polling HMACs.
 */
object PollSigner {
    fun computeSignature(kPoll: ByteArray, canonicalPollBytes: ByteArray): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(kPoll, "HmacSHA256"))
        mac.update("KIVO_BRIDGE_POLL_REQUEST_V1\u0000".toByteArray(Charsets.UTF_8))
        mac.update(canonicalPollBytes)
        return mac.doFinal()
    }

    fun computeCredentialSha256(credential: String): ByteArray {
        return MessageDigest.getInstance("SHA-256")
            .digest(credential.toByteArray(Charsets.UTF_8))
    }

    fun computeResponseSignatureV2(
        kPoll: ByteArray,
        canonicalResponseBytes: ByteArray
    ): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(kPoll, "HmacSHA256"))
        mac.update("KIVO_BRIDGE_POLL_RESPONSE_V2\u0000".toByteArray(Charsets.UTF_8))
        mac.update(canonicalResponseBytes)
        return mac.doFinal()
    }

    fun verifyResponseSignatureV2(
        kPoll: ByteArray,
        canonicalResponseBytes: ByteArray,
        receivedSignature: ByteArray
    ): Boolean {
        val expected = computeResponseSignatureV2(kPoll, canonicalResponseBytes)
        return MessageDigest.isEqual(expected, receivedSignature)
    }
}
