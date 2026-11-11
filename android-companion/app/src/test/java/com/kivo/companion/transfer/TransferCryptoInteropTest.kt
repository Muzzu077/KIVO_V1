package com.kivo.companion.transfer

import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.Base64

/**
 * Byte-for-byte interoperability tests against the shared Python fixture.
 * Both implementations read `tests/fixtures/transfer_test_vectors.json`.
 */
class TransferCryptoInteropTest {

    private val fixture: JSONObject = loadFixture()

    private val sessionId = fixture.getString("session_id")
    private val kUp = hex(fixture.getString("upstream_key_hex"))
    private val kDown = hex(fixture.getString("downstream_key_hex"))

    @Test
    fun `exact upload init body matches Python bytes`() {
        val exact = fixture.getJSONObject("exact_upload_init")
        val body = TransferCrypto.buildInitBody(
            filename = "report.pdf",
            mime = "application/pdf",
            size = 2_097_152L,
            sha256Hex = "a591a6d40bf420404a011733cfb7b190d62c65bf0bcda32b57b277d9ad9f146e"
        )

        assertEquals(exact.getString("body_utf8"), body.toString(Charsets.UTF_8))
        assertEquals(exact.getInt("content_length"), body.size)
        assertEquals(exact.getString("content_sha256_hex"), TransferCrypto.sha256Hex(body))
    }

    @Test
    fun `exact upload init canonical request and MAC match Python`() {
        val exact = fixture.getJSONObject("exact_upload_init")
        val body = exact.getString("body_utf8").toByteArray(Charsets.UTF_8)
        val nonce = hex(exact.getString("nonce_hex"))

        val canonical = TransferCrypto.buildCanonicalRequest(
            version = "1",
            sessionId = sessionId,
            method = exact.getString("method"),
            endpoint = exact.getString("endpoint"),
            sequence = exact.getLong("sequence"),
            nonce = nonce,
            timestamp = exact.getDouble("timestamp"),
            contentLength = body.size.toLong(),
            contentSha256 = TransferCrypto.sha256(body)
        )

        assertEquals(exact.getString("canonical_request_hex"), TransferCrypto.toHex(canonical))
        assertEquals(
            exact.getString("signature_hex"),
            TransferCrypto.toHex(TransferCrypto.computeRequestSignature(kUp, canonical))
        )
    }

    @Test
    fun `exact short chunk canonical request and MAC match Python`() {
        val exact = fixture.getJSONObject("exact_upload_chunk")
        val body = Base64.getDecoder().decode(exact.getString("body_b64"))
        val nonce = hex(exact.getString("nonce_hex"))

        assertEquals(exact.getInt("content_length"), body.size)
        assertEquals(exact.getString("content_sha256_hex"), TransferCrypto.sha256Hex(body))

        val canonical = TransferCrypto.buildCanonicalRequest(
            version = "1",
            sessionId = sessionId,
            method = exact.getString("method"),
            endpoint = exact.getString("endpoint"),
            sequence = exact.getLong("sequence"),
            nonce = nonce,
            timestamp = exact.getDouble("timestamp"),
            contentLength = body.size.toLong(),
            contentSha256 = TransferCrypto.sha256(body)
        )

        assertEquals(exact.getString("canonical_request_hex"), TransferCrypto.toHex(canonical))
        assertEquals(
            exact.getString("signature_hex"),
            TransferCrypto.toHex(TransferCrypto.computeRequestSignature(kUp, canonical))
        )
    }

    @Test
    fun `legacy protocol vectors remain byte identical`() {
        for (name in listOf("upload_init", "upload_chunk", "upload_status", "upload_cancel")) {
            val vector = fixture.getJSONObject(name)
            val canonical = TransferCrypto.buildCanonicalRequest(
                version = "1",
                sessionId = sessionId,
                method = vector.getString("method"),
                endpoint = vector.getString("endpoint"),
                sequence = vector.getLong("sequence"),
                nonce = hex(vector.getString("nonce_hex")),
                timestamp = vector.getDouble("timestamp"),
                contentLength = vector.getLong("content_length"),
                contentSha256 = hex(vector.getString("content_sha256_hex"))
            )
            assertEquals(name, vector.getString("canonical_request_hex"), TransferCrypto.toHex(canonical))
            assertEquals(
                name,
                vector.getString("signature_hex"),
                TransferCrypto.toHex(TransferCrypto.computeRequestSignature(kUp, canonical))
            )
        }
    }

    @Test
    fun `exact response canonical and MAC match Python`() {
        val exact = fixture.getJSONObject("exact_response")
        val payload = Base64.getDecoder().decode(exact.getString("payload_b64"))
        val nonce = hex(exact.getString("nonce_hex"))

        assertEquals(exact.getInt("payload_length"), payload.size)
        assertEquals(exact.getString("payload_sha256_hex"), TransferCrypto.sha256Hex(payload))

        val canonical = TransferCrypto.buildCanonicalResponse(
            version = "1",
            responseContext = exact.getString("response_context"),
            sessionId = sessionId,
            httpStatus = exact.getInt("http_status"),
            sequence = exact.getLong("sequence"),
            nonce = nonce,
            state = exact.getString("state"),
            transferId = exact.getString("transfer_id"),
            payloadLength = payload.size.toLong(),
            payloadSha256 = TransferCrypto.sha256(payload)
        )

        assertEquals(exact.getString("canonical_response_hex"), TransferCrypto.toHex(canonical))
        assertEquals(
            exact.getString("signature_hex"),
            TransferCrypto.toHex(TransferCrypto.computeResponseSignature(kDown, canonical))
        )
    }

    @Test
    fun `nonce encoding matches Python unpadded base64url`() {
        val vector = fixture.getJSONObject("upload_init")
        val nonce = hex(vector.getString("nonce_hex"))
        assertEquals(vector.getString("nonce_b64"), TransferCrypto.encodeNonceB64(nonce))
        assertArrayEquals(nonce, TransferCrypto.decodeNonceB64(vector.getString("nonce_b64")))
    }

    @Test
    fun `modified request bytes never verify`() {
        val exact = fixture.getJSONObject("exact_upload_init")
        val canonical = hex(exact.getString("canonical_request_hex"))
        val signature = hex(exact.getString("signature_hex"))
        assertTrue(TransferCrypto.verifyRequestSignature(kUp, canonical, signature))
        assertFalse(TransferCrypto.verifyRequestSignature(kUp, canonical, flip(signature)))
        assertFalse(TransferCrypto.verifyRequestSignature(kUp, flip(canonical), signature))

        val otherKey = ByteArray(32) { 0x5A }
        assertFalse(TransferCrypto.verifyRequestSignature(otherKey, canonical, signature))
    }

    @Test
    fun `nonce decoder rejects mutation and non-canonical encodings`() {
        val valid = fixture.getJSONObject("upload_init").getString("nonce_b64")
        assertNotNull(TransferCrypto.decodeNonceB64(valid))
        for (bad in listOf(valid + "=", valid + "AA", "AAAA", "!!!!", "")) {
            try {
                TransferCrypto.decodeNonceB64(bad)
                throw AssertionError("accepted malformed nonce: $bad")
            } catch (expected: IllegalArgumentException) {
                // expected
            }
        }
    }

    @Test
    fun `deterministic init serializer preserves non-ascii and escapes controls`() {
        val body = TransferCrypto.buildInitBody(
            filename = "résumé.docx",
            mime = null,
            size = 7L,
            sha256Hex = "A".repeat(64)
        )
        assertEquals(
            "{\"filename\":\"résumé.docx\",\"sha256\":\"${"a".repeat(64)}\",\"size\":7}",
            body.toString(Charsets.UTF_8)
        )
        assertTrue(TransferCrypto.jsonString("a\nb\u0001").startsWith("\"a\\nb"))
    }

    private fun flip(bytes: ByteArray): ByteArray = bytes.copyOf().also {
        it[it.size / 2] = (it[it.size / 2].toInt() xor 0x40).toByte()
    }

    private fun hex(value: String): ByteArray {
        return TransferCrypto.hexToBytes(value) ?: error("bad hex in fixture")
    }

    private fun loadFixture(): JSONObject {
        val stream = javaClass.classLoader!!.getResourceAsStream("transfer_test_vectors.json")
            ?: error("transfer_test_vectors.json missing from test resources")
        return stream.use { JSONObject(it.readBytes().toString(Charsets.UTF_8)) }
    }
}
