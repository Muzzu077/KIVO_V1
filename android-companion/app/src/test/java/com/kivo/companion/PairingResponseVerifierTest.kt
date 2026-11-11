package com.kivo.companion

import com.kivo.companion.network.PairingNetworkException
import com.kivo.companion.network.PairingResponseVerifier
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.fail
import org.junit.Test

/**
 * Adversarial tests for the Stage 13.3.2.3 V2 response verifier.
 *
 * Vectors mirror the Python fixture in tests/fixtures/pairing_test_vectors.json
 * so a change on either side of the wire breaks this suite.
 */
class PairingResponseVerifierTest {

    private val kPoll = hexToBytes(
        "a91e7c826c62d2c9a5b42dbefef6a2de0bf8b8fb83965160703d0fe2f79a9e7b"
    )
    private val sessionId = "sess_0123456789abcdef"
    private val deviceId = "client_test_device_1"
    private val requestNonce = hexToBytes("101112131415161718191a1b1c1d1e1f")
    private val sequence = 1L
    private val sessionToken = "sess_tok_buOWe22qTLQBHoVVlO-wrgLelKcUAzZL5FWjicDCdTk"
    private val credentialHash =
        "8c2ddfd103a6e98ee17fb3bcbc0819fc9c54874db08bffc764cdae2c143f0556"
    private val responseSig =
        "66a0a2a92f2cf68328d2e959f4c77e7543a9d9ee426dbf720aa9124351f5e1fe"

    private fun approvedResponse(): JSONObject = JSONObject().apply {
        put("ok", true)
        put("status", "approved")
        put("response_context", "poll")
        put("http_status", 200)
        put("session_id", sessionId)
        put("sequence", sequence)
        put("expires_in", 7200)
        put("device_id", deviceId)
        put("credential_state", "delivered")
        put("credential_sha256", credentialHash)
        put("session_token", sessionToken)
        put("response_sig", responseSig)
    }

    private fun verify(
        json: JSONObject = approvedResponse(),
        nonce: ByteArray = requestNonce,
        seq: Long = sequence
    ) = PairingResponseVerifier.verify(
        responseJson = json,
        kPoll = kPoll,
        expectedContext = "poll",
        sessionId = sessionId,
        sequence = seq,
        requestNonce = nonce
    )

    private fun assertRejected(
        expectedCode: String,
        json: JSONObject = approvedResponse(),
        nonce: ByteArray = requestNonce,
        seq: Long = sequence
    ) {
        try {
            verify(json, nonce, seq)
            fail("Expected PairingNetworkException($expectedCode)")
        } catch (e: PairingNetworkException) {
            assertEquals(expectedCode, e.code)
        }
    }

    @Test
    fun testValidApprovedResponseVerifies() {
        val verified = verify()
        assertEquals("approved", verified.status)
        assertEquals(sessionToken, verified.sessionToken)
        assertEquals(7200L, verified.expiresIn)
        assertEquals(deviceId, verified.deviceId)
        assertNotNull(verified.sessionToken)
    }

    @Test
    fun testAlteredTokenIsRejected() {
        val json = approvedResponse()
        json.put("session_token", "sess_tok_attacker")
        assertRejected("credential_hash_mismatch", json)
    }

    @Test
    fun testAlteredExpiryIsRejected() {
        val json = approvedResponse()
        json.put("expires_in", 7201)
        assertRejected("invalid_response_signature", json)
    }

    @Test
    fun testAlteredDeviceIdIsRejected() {
        val json = approvedResponse()
        json.put("device_id", "client_other_device")
        assertRejected("invalid_response_signature", json)
    }

    @Test
    fun testAlteredStatusIsRejected() {
        val json = approvedResponse()
        json.put("status", "pending_approval")
        json.put("credential_state", "none")
        json.put("credential_sha256", "")
        json.remove("session_token")
        assertRejected("invalid_response_signature", json)
    }

    @Test
    fun testAlteredNonceIsRejected() {
        assertRejected(
            "invalid_response_signature",
            nonce = ByteArray(16) { 0xEE.toByte() }
        )
    }

    @Test
    fun testAlteredSequenceIsRejected() {
        // The stored MAC covers sequence 1. Presenting it as sequence 2 must fail
        // even when the JSON field and the verifier expectation agree.
        val json = approvedResponse()
        json.put("sequence", 2L)
        assertRejected("invalid_response_signature", json, seq = 2L)
    }

    @Test
    fun testAlteredSequenceFieldIsRejected() {
        val json = approvedResponse()
        json.put("sequence", 5L)
        assertRejected("invalid_response_sequence", json)
    }

    @Test
    fun testAlteredHttpStatusIsRejected() {
        val json = approvedResponse()
        json.put("http_status", 201)
        assertRejected("invalid_response_http_status", json)
    }

    @Test
    fun testAlteredResponseContextIsRejected() {
        val json = approvedResponse()
        json.put("response_context", "handshake")
        assertRejected("invalid_response_context", json)
    }

    @Test
    fun testAlteredSessionIdIsRejected() {
        val json = approvedResponse()
        json.put("session_id", "sess_ffffffffffffffff")
        assertRejected("invalid_response_session", json)
    }

    @Test
    fun testRejectedStatusIsNotAccepted() {
        val json = approvedResponse()
        json.put("status", "rejected")
        json.put("credential_state", "none")
        json.put("credential_sha256", "")
        json.remove("session_token")
        assertRejected("invalid_response_status", json)
    }

    @Test
    fun testPendingResponseMustNotCarryCredentials() {
        val json = JSONObject().apply {
            put("ok", true)
            put("status", "pending_approval")
            put("response_context", "poll")
            put("http_status", 200)
            put("session_id", sessionId)
            put("sequence", sequence)
            put("expires_in", 0)
            put("device_id", deviceId)
            put("credential_state", "none")
            put("credential_sha256", "")
            put("session_token", sessionToken)
            put("response_sig", responseSig)
        }
        assertRejected("invalid_credential_state", json)
    }

    @Test
    fun testApprovedResponseWithoutCredentialIsRejected() {
        val json = approvedResponse()
        json.put("credential_state", "none")
        json.put("credential_sha256", "")
        json.remove("session_token")
        assertRejected("invalid_credential_state", json)
    }

    @Test
    fun testOutOfRangeExpiryIsRejected() {
        val json = approvedResponse()
        json.put("expires_in", 86_401)
        assertRejected("invalid_response_expiry", json)
    }

    @Test
    fun testMalformedSignatureIsRejected() {
        val json = approvedResponse()
        json.put("response_sig", "00")
        assertRejected("invalid_response_signature", json)
    }

    private fun hexToBytes(hex: String): ByteArray {
        val data = ByteArray(hex.length / 2)
        var i = 0
        while (i < hex.length) {
            data[i / 2] = ((Character.digit(hex[i], 16) shl 4) +
                Character.digit(hex[i + 1], 16)).toByte()
            i += 2
        }
        return data
    }
}
