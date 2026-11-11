package com.kivo.companion.transfer

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.Base64

class TransferResponseVerifierTest {

    private val fixture = loadFixture()
    private val sessionId = fixture.getString("session_id")
    private val kUp = hex(fixture.getString("upstream_key_hex"))
    private val kDown = hex(fixture.getString("downstream_key_hex"))
    private val exact = fixture.getJSONObject("exact_response")
    private val nonce = hex(exact.getString("nonce_hex"))
    private val sequence = exact.getLong("sequence")
    private val payloadBytes = Base64.getDecoder().decode(exact.getString("payload_b64"))

    @Test
    fun `valid exact desktop response verifies and exposes payload`() {
        val verified = TransferResponseVerifier.verify(
            envelope = validEnvelope(),
            downstreamKey = kDown,
            expectedContext = "upload_init",
            sessionId = sessionId,
            sequence = sequence,
            requestNonce = nonce,
            actualHttpStatus = 202
        )
        assertEquals("awaiting_desktop_approval", verified.state)
        assertEquals("tr_aabbccddee112233aabbccdd", verified.transferId)
        assertEquals("report.pdf", verified.payload.getString("filename"))
        assertTrue(verified.ok)
    }

    @Test
    fun `unsigned error envelope is rejected`() {
        val envelope = JSONObject()
            .put("ok", false)
            .put("error_code", "invalid_signature")
            .put("error_message", "nope")
        assertFails("unauthenticated_error") {
            verify(envelope)
        }
    }

    @Test
    fun `signed error envelope verifies without trusting unsigned fields`() {
        val errorPayload = JSONObject()
            .put("transfer_id", "tr_aabbccddee112233aabbccdd")
            .put("state", "awaiting_desktop_approval")
            .put("error_code", "approval_expired")
            .put("error_message", "expired")
        val envelope = signedEnvelope(
            context = "upload_init",
            httpStatus = 410,
            state = "error",
            transferId = "tr_aabbccddee112233aabbccdd",
            payload = errorPayload.toString().toByteArray(Charsets.UTF_8)
        )
        val verified = verify(envelope, actualStatus = 410)
        assertEquals(410, verified.httpStatus)
        assertEquals("error", verified.state)
        assertEquals("approval_expired", verified.errorCode)
    }

    @Test
    fun `each authenticated envelope field mutation fails`() {
        val mutations: List<Pair<String, (JSONObject) -> Unit>> = listOf(
            "v" to { it.put("v", "2") },
            "ctx" to { it.put("ctx", "upload_chunk") },
            "sid" to { it.put("sid", "sess_ffffffffffffffff") },
            "seq" to { it.put("seq", sequence + 1) },
            "nonce" to { it.put("nonce_b64", TransferCrypto.encodeNonceB64(ByteArray(16) { 9 })) },
            "http_status" to { it.put("http_status", 200) },
            "state" to { it.put("state", "committed") },
            "tid" to { it.put("tid", "tr_ffffffffffffffffffffffff") },
            "payload_sha256" to { it.put("payload_sha256", "0".repeat(64)) },
            "sig" to { it.put("sig", "0".repeat(64)) }
        )
        for ((name, mutate) in mutations) {
            val envelope = validEnvelope()
            mutate(envelope)
            try {
                verify(envelope)
                throw AssertionError("mutation accepted: $name")
            } catch (expected: TransferProtocolException) {
                // expected
            }
        }
    }

    @Test
    fun `payload byte mutation and payload replacement fail`() {
        val mutatedPayload = payloadBytes.copyOf().also { it[10] = (it[10].toInt() xor 0x01).toByte() }
        val mutatedEnvelope = validEnvelope().put(
            "payload_b64",
            Base64.getEncoder().encodeToString(mutatedPayload)
        )
        assertFails("invalid_response_payload_hash") { verify(mutatedEnvelope) }

        val replacement = JSONObject()
            .put("transfer_id", "tr_aabbccddee112233aabbccdd")
            .put("state", "committed")
            .put("filename", "evil.pdf")
            .toString().toByteArray()
        val replacementEnvelope = validEnvelope().put(
            "payload_b64",
            Base64.getEncoder().encodeToString(replacement)
        )
        assertFails("invalid_response_payload_hash") { verify(replacementEnvelope) }
    }

    @Test
    fun `payload state contradiction fails even with recomputed MAC`() {
        val contradictory = JSONObject(exact.getString("payload_utf8"))
            .put("state", "committed")
            .toString().toByteArray()
        val envelope = signedEnvelope(
            context = "upload_init",
            httpStatus = 202,
            state = "awaiting_desktop_approval",
            transferId = "tr_aabbccddee112233aabbccdd",
            payload = contradictory
        )
        assertFails("invalid_response_payload_state") { verify(envelope) }
    }

    @Test
    fun `wrong downstream key fails`() {
        assertFails("invalid_response_signature") {
            TransferResponseVerifier.verify(
                validEnvelope(),
                ByteArray(32) { 0x42 },
                "upload_init",
                sessionId,
                sequence,
                nonce,
                202
            )
        }
    }

    @Test
    fun `transport status must match signed status`() {
        assertFails("invalid_response_http_status") {
            verify(validEnvelope(), actualStatus = 200)
        }
    }

    @Test
    fun `ok flag must agree with signed status`() {
        val envelope = validEnvelope()
        envelope.put("ok", false)
        assertFails("invalid_response_ok") { verify(envelope) }
    }

    @Test
    fun `payload with trailing data is rejected after a valid MAC`() {
        val trailing = payloadBytes + " trailing".toByteArray()
        val envelope = signedEnvelope(
            context = "upload_init",
            httpStatus = 202,
            state = "awaiting_desktop_approval",
            transferId = "tr_aabbccddee112233aabbccdd",
            payload = trailing
        )
        assertFails("invalid_response_payload") { verify(envelope) }
    }

    private fun validEnvelope(payloadOverride: ByteArray? = null): JSONObject {
        val payload = payloadOverride ?: payloadBytes
        return signedEnvelope(
            context = "upload_init",
            httpStatus = 202,
            state = "awaiting_desktop_approval",
            transferId = "tr_aabbccddee112233aabbccdd",
            payload = payload
        )
    }

    private fun signedEnvelope(
        context: String,
        httpStatus: Int,
        state: String,
        transferId: String,
        payload: ByteArray
    ): JSONObject {
        val payloadHash = TransferCrypto.sha256(payload)
        val canonical = TransferCrypto.buildCanonicalResponse(
            version = "1",
            responseContext = context,
            sessionId = sessionId,
            httpStatus = httpStatus,
            sequence = sequence,
            nonce = nonce,
            state = state,
            transferId = transferId,
            payloadLength = payload.size.toLong(),
            payloadSha256 = payloadHash
        )
        val signature = TransferCrypto.computeResponseSignature(kDown, canonical)
        return JSONObject()
            .put("ok", httpStatus in 200..299)
            .put("v", "1")
            .put("ctx", context)
            .put("sid", sessionId)
            .put("seq", sequence)
            .put("nonce_b64", TransferCrypto.encodeNonceB64(nonce))
            .put("http_status", httpStatus)
            .put("state", state)
            .put("tid", transferId)
            .put("payload_b64", Base64.getEncoder().encodeToString(payload))
            .put("payload_sha256", TransferCrypto.toHex(payloadHash))
            .put("sig", TransferCrypto.toHex(signature))
    }

    private fun verify(envelope: JSONObject, actualStatus: Int = 202): VerifiedTransferResponse =
        TransferResponseVerifier.verify(
            envelope,
            kDown,
            "upload_init",
            sessionId,
            sequence,
            nonce,
            actualStatus
        )

    private fun assertFails(code: String, block: () -> Unit) {
        try {
            block()
            throw AssertionError("expected $code")
        } catch (e: TransferProtocolException) {
            assertEquals(code, e.code)
        }
    }

    private fun hex(value: String): ByteArray =
        TransferCrypto.hexToBytes(value) ?: error("bad fixture hex")

    private fun loadFixture(): JSONObject {
        val stream = javaClass.classLoader!!.getResourceAsStream("transfer_test_vectors.json")
            ?: error("fixture missing")
        return stream.use { JSONObject(it.readBytes().toString(Charsets.UTF_8)) }
    }
}
