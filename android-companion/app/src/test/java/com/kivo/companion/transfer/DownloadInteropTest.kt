package com.kivo.companion.transfer

import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.security.MessageDigest
import java.util.Base64

/**
 * Byte-for-byte Python/Kotlin interoperability for download requests and
 * signed responses. The fixture is the single source of truth and contains
 * only synthetic metadata and pattern bytes.
 */
class DownloadInteropTest {

    private val fixture = loadFixture()
    private val sessionId = fixture.getString("session_id")
    private val kUp = hex(fixture.getString("upstream_key_hex"))
    private val kDown = hex(fixture.getString("downstream_key_hex"))

    @Test
    fun `download init body matches Python exactly`() {
        val request = fixture.getJSONObject("requests").getJSONObject("init")
        val body = TransferCrypto.buildDownloadInitBody(fixture.getString("ticket_id"))
        assertEquals(request.getString("body_utf8"), body.toString(Charsets.UTF_8))
        assertEquals(request.getInt("content_length"), body.size)
        assertEquals(request.getString("content_sha256"), TransferCrypto.sha256Hex(body))
    }

    @Test
    fun `download request canonical bytes and MACs match Python`() {
        val requests = fixture.getJSONObject("requests")
        for (name in listOf("init", "status", "chunk", "complete", "cancel")) {
            val vector = requests.getJSONObject(name)
            val body = vector.getString("body_utf8").toByteArray(Charsets.UTF_8)
            assertEquals(name, vector.getInt("content_length"), body.size)
            assertEquals(name, vector.getString("content_sha256"), TransferCrypto.sha256Hex(body))

            val canonical = TransferCrypto.buildCanonicalRequest(
                version = fixture.getString("protocol_version"),
                sessionId = sessionId,
                method = vector.getString("method"),
                endpoint = vector.getString("endpoint"),
                sequence = vector.getLong("sequence"),
                nonce = hex(vector.getString("nonce_hex")),
                timestamp = vector.getDouble("timestamp"),
                contentLength = body.size.toLong(),
                contentSha256 = TransferCrypto.sha256(body)
            )
            assertEquals(name, vector.getString("canonical_hex"), TransferCrypto.toHex(canonical))
            assertEquals(
                name,
                vector.getString("signature_hex"),
                TransferCrypto.toHex(TransferCrypto.computeRequestSignature(kUp, canonical))
            )
        }
    }

    @Test
    fun `all signed download responses verify with K_down`() {
        val responses = fixture.getJSONObject("responses")
        for (name in listOf("init", "status", "chunk", "complete", "cancel", "expired")) {
            val vector = responses.getJSONObject(name)
            val requestName = vector.getString("request")
            val request = fixture.getJSONObject("requests").getJSONObject(requestName)
            val envelope = vector.getJSONObject("envelope")
            val maxPayload = if (name == "chunk") {
                TransferHttpClient.MAX_DOWNLOAD_PAYLOAD_BYTES
            } else {
                TransferResponseVerifier.MAX_PAYLOAD_BYTES
            }
            val verified = TransferResponseVerifier.verify(
                envelope = envelope,
                downstreamKey = kDown,
                expectedContext = "download_$requestName",
                sessionId = sessionId,
                sequence = request.getLong("sequence"),
                requestNonce = hex(request.getString("nonce_hex")),
                actualHttpStatus = vector.getInt("http_status"),
                maxPayloadBytes = maxPayload
            )
            assertEquals(name, vector.getInt("http_status"), verified.httpStatus)
            assertEquals(name, vector.getString("state"), verified.state)
            assertEquals(name, fixture.getString("ticket_id"), verified.transferId)
            val payload = JSONObject(vector.getString("payload_utf8"))
            assertTrue(name, payload.similar(verified.payload))
        }
    }

    @Test
    fun `tampered response envelopes never verify`() {
        val vector = fixture.getJSONObject("responses").getJSONObject("init")
        val request = fixture.getJSONObject("requests").getJSONObject("init")
        val envelope = vector.getJSONObject("envelope")

        fun verify(mutated: JSONObject): String? {
            return try {
                TransferResponseVerifier.verify(
                    envelope = mutated,
                    downstreamKey = kDown,
                    expectedContext = "download_init",
                    sessionId = sessionId,
                    sequence = request.getLong("sequence"),
                    requestNonce = hex(request.getString("nonce_hex")),
                    actualHttpStatus = vector.getInt("http_status")
                )
                null
            } catch (e: TransferProtocolException) {
                e.code
            }
        }

        val flippedSig = JSONObject(envelope.toString()).put("sig", flipHex(envelope.getString("sig")))
        assertEquals("invalid_response_signature", verify(flippedSig))

        val flippedPayload = JSONObject(envelope.toString()).put(
            "payload_b64",
            flipBase64(envelope.getString("payload_b64"))
        )
        val payloadCode = verify(flippedPayload)
        assertTrue(payloadCode == "invalid_response_payload_hash" || payloadCode == "invalid_response_payload")

        val flippedNonce = JSONObject(envelope.toString()).put(
            "nonce_b64",
            TransferCrypto.encodeNonceB64(ByteArray(16) { 7 })
        )
        assertEquals("invalid_response_nonce", verify(flippedNonce))

        val wrongSequence = JSONObject(envelope.toString()).put("seq", request.getLong("sequence") + 1L)
        assertEquals("invalid_response_sequence", verify(wrongSequence))

        assertEquals(
            "invalid_response_context",
            try {
                TransferResponseVerifier.verify(
                    envelope = envelope,
                    downstreamKey = kDown,
                    expectedContext = "download_status",
                    sessionId = sessionId,
                    sequence = request.getLong("sequence"),
                    requestNonce = hex(request.getString("nonce_hex")),
                    actualHttpStatus = vector.getInt("http_status")
                )
                null
            } catch (e: TransferProtocolException) {
                e.code
            }
        )

        assertEquals(
            "invalid_response_signature",
            try {
                TransferResponseVerifier.verify(
                    envelope = envelope,
                    downstreamKey = ByteArray(32) { 0x5A },
                    expectedContext = "download_init",
                    sessionId = sessionId,
                    sequence = request.getLong("sequence"),
                    requestNonce = hex(request.getString("nonce_hex")),
                    actualHttpStatus = vector.getInt("http_status")
                )
                null
            } catch (e: TransferProtocolException) {
                e.code
            }
        )
    }

    @Test
    fun `chunk payload and whole-file digest are self-consistent`() {
        val payload = JSONObject(
            fixture.getJSONObject("responses").getJSONObject("chunk").getString("payload_utf8")
        )
        val chunk = Base64.getDecoder().decode(payload.getString("chunk_b64"))
        assertEquals(fixture.getInt("chunk_index").toLong(), payload.getLong("chunk_index"))
        assertEquals(0L, payload.getLong("offset"))
        assertEquals(fixture.getInt("chunk_length").toLong(), payload.getLong("length"))
        assertEquals(chunk.size.toLong(), payload.getLong("length"))
        assertEquals(payload.getString("chunk_sha256"), TransferCrypto.sha256Hex(chunk))
        assertEquals(fixture.getString("chunk_sha256"), payload.getString("chunk_sha256"))
        assertEquals(fixture.getString("sha256"), TransferCrypto.sha256Hex(chunk))
        assertArrayEquals(patternBytes(fixture.getInt("file_size")), chunk)
    }

    @Test
    fun `public download metadata is path free`() {
        val responses = fixture.getJSONObject("responses")
        for (name in listOf("init", "status", "chunk", "complete", "cancel", "expired")) {
            val payload = JSONObject(responses.getJSONObject(name).getString("payload_utf8"))
            for (key in listOf(
                "download_id", "device_id", "filename", "size", "sha256", "state",
                "bytes_sent", "next_chunk_index", "total_chunks", "chunk_size",
                "expires_at", "claim_expires_at", "active_expires_at"
            )) {
                assertTrue("$name missing $key", payload.has(key))
            }
            assertEquals(fixture.getString("ticket_id"), payload.getString("download_id"))
            assertEquals(fixture.getString("device_id"), payload.getString("device_id"))
            assertFalse(payload.getString("filename").contains('/'))
            assertFalse(payload.getString("filename").contains('\\'))
        }
        val encoded = fixture.toString()
        assertFalse(encoded.contains("/home/"))
        assertFalse(encoded.contains("/tmp/"))
        assertFalse(encoded.contains("file:///"))
        assertFalse(encoded.lowercase().contains("bearer"))
        assertFalse(encoded.contains("BEGIN "))
    }

    @Test
    fun `chunk response fits the bounded download envelope`() {
        val payload = JSONObject(
            fixture.getJSONObject("responses").getJSONObject("chunk").getString("payload_utf8")
        )
        val payloadBytes = fixture.getJSONObject("responses")
            .getJSONObject("chunk")
            .getString("payload_utf8")
            .toByteArray(Charsets.UTF_8)
        assertTrue(payloadBytes.size <= TransferHttpClient.MAX_DOWNLOAD_PAYLOAD_BYTES)
        assertTrue(payload.getString("chunk_b64").length < TransferHttpClient.MAX_DOWNLOAD_RESPONSE_BYTES)
        assertNotNull(TransferCrypto.hexToBytes(fixture.getString("chunk_sha256"), 32))
        assertTrue(
            MessageDigest.isEqual(
                TransferCrypto.sha256(payloadBytes),
                TransferCrypto.hexToBytes(
                    fixture.getJSONObject("responses").getJSONObject("chunk")
                        .getString("payload_sha256"),
                    32
                )
            )
        )
    }

    private fun patternBytes(size: Int): ByteArray = ByteArray(size) { (it % 251).toByte() }

    private fun flipHex(value: String): String {
        val bytes = hex(value)
        bytes[0] = (bytes[0].toInt() xor 0x01).toByte()
        return TransferCrypto.toHex(bytes)
    }

    private fun flipBase64(value: String): String {
        val bytes = Base64.getDecoder().decode(value)
        bytes[bytes.size / 2] = (bytes[bytes.size / 2].toInt() xor 0x01).toByte()
        return Base64.getEncoder().encodeToString(bytes)
    }

    private fun hex(value: String): ByteArray =
        TransferCrypto.hexToBytes(value) ?: error("bad hex in fixture")

    private fun loadFixture(): JSONObject {
        val stream = javaClass.classLoader!!.getResourceAsStream("download_interop_vectors.json")
            ?: error("download_interop_vectors.json missing from test resources")
        return stream.use { JSONObject(it.readBytes().toString(Charsets.UTF_8)) }
    }
}
