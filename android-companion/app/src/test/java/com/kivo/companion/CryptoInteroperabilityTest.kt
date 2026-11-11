package com.kivo.companion

import com.kivo.companion.crypto.*
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.Base64

class CryptoInteroperabilityTest {

    // Exact deterministic vectors generated from Python Stage 13.3.1
    private val version = "1"
    private val algorithmId = "HMAC-SHA256-HKDF-SAS-V1"
    private val sessionId = "sess_0123456789abcdef"
    private val clientId = "client_test_device_1"
    private val certFp = "E1CE2A45791117A9124F26201FF40CA4C13956CCF61D0D81EAF1A3DF6B047B21"

    private val pairingSecret = hexToBytes("0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20")
    private val serverNonce = hexToBytes("a0a1a2a3a4a5a6a7a8a9aaabacadaeafb0b1b2b3b4b5b6b7b8b9babbbcbdbebf")
    private val clientNonce = hexToBytes("c0c1c2c3c4c5c6c7c8c9cacbcccdcecfd0d1d2d3d4d5d6d7d8d9dadbdcdddedf")

    private val expectedContextHex = (
        "0000000131" +
        "00000015736573735f30313233343536373839616263646566" +
        "0000004045314345324134353739313131374139313234463236323031464634304341344331333935364343463631443044383145414631413344463642303437423231" +
        "00000020a0a1a2a3a4a5a6a7a8a9aaabacadaeafb0b1b2b3b4b5b6b7b8b9babbbcbdbebf" +
        "00000020c0c1c2c3c4c5c6c7c8c9cacbcccdcecfd0d1d2d3d4d5d6d7d8d9dadbdcdddedf" +
        "00000014636c69656e745f746573745f6465766963655f31" +
        "00000017484d41432d5348413235362d484b44462d5341532d5631"
    )

    private val expectedClientProofHex = "cefb4594c04f07a3084b8e6c5a86824a9e760b81e03d310aa56fc474ecebbc06"
    private val expectedClientProofB64 = "zvtFlMBPB6MIS45sWoaCSp52C4HgPTEKpW_EdOzrvAY"
    private val expectedSasInt = 779684
    private val expectedSasStr = "779 684"
    private val expectedUpstreamKeyHex = "a5050108248329e29e71564ed8bdea9ceaa0148cf3893d4150ecd7172715dc5b"
    private val expectedDownstreamKeyHex = "4a34cf264a911423387b284793fb6bab2ab1bd53df4c948ac957095cc683f8bf"
    private val expectedBearerToken = "sess_tok_buOWe22qTLQBHoVVlO-wrgLelKcUAzZL5FWjicDCdTk"
    private val expectedKPollHex = "a91e7c826c62d2c9a5b42dbefef6a2de0bf8b8fb83965160703d0fe2f79a9e7b"
    private val expectedPollNonceHex = "101112131415161718191a1b1c1d1e1f"
    private val expectedCanonicalPollReqHex = "000000013100000015736573735f3031323334353637383961626364656600000004504f5354000000132f6170692f76312f706169722f737461747573000000000000000100000010101112131415161718191a1b1c1d1e1f41d954fc40000000"
    private val expectedPollReqSigHex = "4640d07c4596f328b626bd655547ca39918ddcaca237a107248ccd74c800f9bd"

    // Stage 13.3.2.3 V2 response vectors (generated from Python)
    private val expectedCredentialSha256Hex =
        "8c2ddfd103a6e98ee17fb3bcbc0819fc9c54874db08bffc764cdae2c143f0556"
    private val expectedCanonicalPollRespHex =
        "000000013100000004706f6c6c00000015736573735f30313233343536373839616263646566" +
        "00c8000000000000000100000010101112131415161718191a1b1c1d1e1f00000008617070726f" +
        "7665640000000000001c2000000014636c69656e745f746573745f6465766963655f3100000009" +
        "64656c697665726564000000208c2ddfd103a6e98ee17fb3bcbc0819fc9c54874db08bffc764cd" +
        "ae2c143f0556"
    private val expectedPollRespSigV2Hex =
        "66a0a2a92f2cf68328d2e959f4c77e7543a9d9ee426dbf720aa9124351f5e1fe"
    private val expectedCanonicalPendingRespHex =
        "000000013100000004706f6c6c00000015736573735f30313233343536373839616263646566" +
        "00c8000000000000000100000010101112131415161718191a1b1c1d1e1f0000001070656e64" +
        "696e675f617070726f76616c000000000000000000000014636c69656e745f746573745f6465" +
        "766963655f31000000046e6f6e6500000000"
    private val expectedPendingRespSigV2Hex =
        "ec6c82f56c4a342703c2459c4281692af52084aaff098d68e4a99ee7c8c4f9ed"
    private val expectedCanonicalHandshakeRespHex =
        "00000001310000000968616e647368616b6500000015736573735f3031323334353637383961" +
        "626364656600c8000000000000000000000020c0c1c2c3c4c5c6c7c8c9cacbcccdcecfd0d1d2" +
        "d3d4d5d6d7d8d9dadbdcdddedf00000008617070726f7665640000000000001c200000001463" +
        "6c69656e745f746573745f6465766963655f310000000964656c697665726564000000208c2d" +
        "dfd103a6e98ee17fb3bcbc0819fc9c54874db08bffc764cdae2c143f0556"
    private val expectedHandshakeRespSigV2Hex =
        "db51bf809fc05df6bd7d953e6049b0fbf51a9bc9eae76fdbba19bcbb1ad7fc89"

    @Test
    fun testCanonicalContextMatchesPythonByteForByte() {
        val contextBytes = CanonicalContext.build(
            version = version,
            sessionId = sessionId,
            certFingerprint = certFp,
            serverNonce = serverNonce,
            clientNonce = clientNonce,
            clientId = clientId,
            algorithmId = algorithmId
        )
        assertEquals(expectedContextHex, bytesToHex(contextBytes))
    }

    @Test
    fun testClientProofMatchesPythonByteForByte() {
        val contextBytes = hexToBytes(expectedContextHex)
        val proof = PairingProof.compute(pairingSecret, contextBytes)
        assertEquals(expectedClientProofHex, bytesToHex(proof))

        val b64 = Base64.getUrlEncoder().withoutPadding().encodeToString(proof)
        assertEquals(expectedClientProofB64, b64)

        assertTrue(PairingProof.verify(pairingSecret, contextBytes, proof))
    }

    @Test
    fun testSasDerivationMatchesPythonByteForByte() {
        val contextBytes = hexToBytes(expectedContextHex)
        val (sasInt, sasStr) = SasDerivation.derive(pairingSecret, serverNonce, contextBytes)

        assertEquals(expectedSasInt, sasInt)
        assertEquals(expectedSasStr, sasStr)
    }

    @Test
    fun testSessionKeyDerivationMatchesPythonByteForByte() {
        val contextBytes = hexToBytes(expectedContextHex)
        val derived = SessionKeyDerivation.derive(pairingSecret, clientNonce, contextBytes)

        assertEquals(expectedUpstreamKeyHex, bytesToHex(derived.upstreamKey))
        assertEquals(expectedDownstreamKeyHex, bytesToHex(derived.downstreamKey))
        assertEquals(expectedBearerToken, derived.bearerToken)
    }

    @Test
    fun testPollKeyDerivationMatchesPythonByteForByte() {
        val contextBytes = hexToBytes(expectedContextHex)
        val kPoll = SessionKeyDerivation.derivePollKey(pairingSecret, clientNonce, contextBytes)
        assertEquals(expectedKPollHex, bytesToHex(kPoll))
    }

    @Test
    fun testCanonicalPollRequestAndSignatureMatchPythonByteForByte() {
        val kPoll = hexToBytes(expectedKPollHex)
        val nonce = hexToBytes(expectedPollNonceHex)
        val canonicalPoll = CanonicalPollRequest.build(
            version = "1",
            sessionId = sessionId,
            sequence = 1L,
            requestNonce = nonce,
            timestamp = 1700000000.0,
            method = "POST",
            endpoint = "/api/v1/pair/status"
        )
        assertEquals(expectedCanonicalPollReqHex, bytesToHex(canonicalPoll))

        val reqSig = PollSigner.computeSignature(kPoll, canonicalPoll)
        assertEquals(expectedPollReqSigHex, bytesToHex(reqSig))
    }

    @Test
    fun testCredentialSha256MatchesPythonByteForByte() {
        assertEquals(
            expectedCredentialSha256Hex,
            bytesToHex(PollSigner.computeCredentialSha256(expectedBearerToken))
        )
    }

    @Test
    fun testApprovedPollResponseV2MatchesPythonByteForByte() {
        val kPoll = hexToBytes(expectedKPollHex)
        val canonical = CanonicalPollResponse.build(
            version = version,
            responseContext = "poll",
            sessionId = sessionId,
            httpStatus = 200,
            sequence = 1L,
            requestNonce = hexToBytes(expectedPollNonceHex),
            status = "approved",
            expiresIn = 7200L,
            deviceId = clientId,
            credentialState = "delivered",
            credentialSha256 = hexToBytes(expectedCredentialSha256Hex)
        )
        assertEquals(expectedCanonicalPollRespHex, bytesToHex(canonical))

        val sig = PollSigner.computeResponseSignatureV2(kPoll, canonical)
        assertEquals(expectedPollRespSigV2Hex, bytesToHex(sig))
        assertTrue(PollSigner.verifyResponseSignatureV2(kPoll, canonical, sig))
    }

    @Test
    fun testPendingPollResponseV2MatchesPythonByteForByte() {
        val kPoll = hexToBytes(expectedKPollHex)
        val canonical = CanonicalPollResponse.build(
            version = version,
            responseContext = "poll",
            sessionId = sessionId,
            httpStatus = 200,
            sequence = 1L,
            requestNonce = hexToBytes(expectedPollNonceHex),
            status = "pending_approval",
            expiresIn = 0L,
            deviceId = clientId,
            credentialState = "none",
            credentialSha256 = ByteArray(0)
        )
        assertEquals(expectedCanonicalPendingRespHex, bytesToHex(canonical))
        assertEquals(
            expectedPendingRespSigV2Hex,
            bytesToHex(PollSigner.computeResponseSignatureV2(kPoll, canonical))
        )
    }

    @Test
    fun testHandshakeResponseV2MatchesPythonByteForByte() {
        val kPoll = hexToBytes(expectedKPollHex)
        val canonical = CanonicalPollResponse.build(
            version = version,
            responseContext = "handshake",
            sessionId = sessionId,
            httpStatus = 200,
            sequence = 0L,
            requestNonce = clientNonce,
            status = "approved",
            expiresIn = 7200L,
            deviceId = clientId,
            credentialState = "delivered",
            credentialSha256 = hexToBytes(expectedCredentialSha256Hex)
        )
        assertEquals(expectedCanonicalHandshakeRespHex, bytesToHex(canonical))
        assertEquals(
            expectedHandshakeRespSigV2Hex,
            bytesToHex(PollSigner.computeResponseSignatureV2(kPoll, canonical))
        )
    }

    @Test
    fun testResponseV2RejectsEverySingleFieldMutation() {
        val kPoll = hexToBytes(expectedKPollHex)
        val credentialHash = hexToBytes(expectedCredentialSha256Hex)

        fun canonical(
            version: String = "1",
            responseContext: String = "poll",
            sessionId: String = this.sessionId,
            httpStatus: Int = 200,
            sequence: Long = 1L,
            requestNonce: ByteArray = hexToBytes(expectedPollNonceHex),
            status: String = "approved",
            expiresIn: Long = 7200L,
            deviceId: String = clientId,
            credentialState: String = "delivered",
            credentialSha256: ByteArray = credentialHash
        ) = CanonicalPollResponse.build(
            version, responseContext, sessionId, httpStatus, sequence,
            requestNonce, status, expiresIn, deviceId, credentialState,
            credentialSha256
        )

        val sig = PollSigner.computeResponseSignatureV2(kPoll, canonical())

        val mutations: List<Pair<String, ByteArray>> = listOf(
            "version" to canonical(version = "2"),
            "response_context" to canonical(responseContext = "handshake"),
            "session_id" to canonical(sessionId = "sess_ffffffffffffffff"),
            "http_status" to canonical(httpStatus = 201),
            "sequence" to canonical(sequence = 2L),
            "request_nonce" to canonical(requestNonce = ByteArray(16) { 0xFF.toByte() }),
            "status" to canonical(status = "pending_approval"),
            "expires_in" to canonical(expiresIn = 7199L),
            "device_id" to canonical(deviceId = "client_other"),
            "credential_state" to canonical(credentialState = "none"),
            "credential_sha256" to canonical(
                credentialSha256 = PollSigner.computeCredentialSha256("sess_tok_attacker")
            )
        )

        for ((field, mutated) in mutations) {
            assertFalse(
                "MAC unexpectedly verified after mutating $field",
                PollSigner.verifyResponseSignatureV2(kPoll, mutated, sig)
            )
        }

        assertTrue(PollSigner.verifyResponseSignatureV2(kPoll, canonical(), sig))
    }

    private fun hexToBytes(hex: String): ByteArray {
        val len = hex.length
        val data = ByteArray(len / 2)
        var i = 0
        while (i < len) {
            data[i / 2] = ((Character.digit(hex[i], 16) shl 4) + Character.digit(hex[i + 1], 16)).toByte()
            i += 2
        }
        return data
    }

    private fun bytesToHex(bytes: ByteArray): String {
        return bytes.joinToString("") { "%02x".format(it) }
    }
}
