package com.kivo.companion.pairing

import com.kivo.companion.crypto.CanonicalContext
import com.kivo.companion.crypto.CanonicalPollResponse
import com.kivo.companion.crypto.PollSigner
import com.kivo.companion.crypto.SessionKeyDerivation
import com.kivo.companion.model.PairingOffer
import com.kivo.companion.network.PairingNetworkException
import com.kivo.companion.storage.KeystoreSecretStorage
import kotlinx.coroutines.test.runTest
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.security.SecureRandom
import java.util.Base64
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey

class PairingRecoveryTest {

    private lateinit var tempDir: File
    private lateinit var testKey: SecretKey
    private lateinit var storage: KeystoreSecretStorage
    private lateinit var store: PairingRecoveryStore

    private val now = System.currentTimeMillis() / 1000L
    private val sessionId = "sess_0123456789abcdef"
    private val clientId = "client_recovery_test"
    private val certFp = "A".repeat(64)
    private val pairingSecret = ByteArray(32) { (it + 1).toByte() }
    private val serverNonce = ByteArray(32) { (0xA0 + it).toByte() }
    private lateinit var offer: PairingOffer

    @Before
    fun setUp() {
        tempDir = Files.createTempDirectory("pairing_recovery_test_").toFile()
        val keyGen = KeyGenerator.getInstance("AES")
        keyGen.init(256)
        testKey = keyGen.generateKey()
        storage = KeystoreSecretStorage(
            storageDirectory = tempDir,
            customKeyProvider = { testKey }
        )
        store = PairingRecoveryStore(storage)
        offer = PairingOffer(
            version = "1",
            hostIp = "192.168.1.50",
            port = 8443,
            certFingerprint = certFp,
            pairingSecret = pairingSecret,
            sessionId = sessionId,
            serverNonce = serverNonce,
            expiresAt = now + 300
        )
    }

    @After
    fun tearDown() {
        tempDir.deleteRecursively()
    }

    private fun repository(
        factory: PairingTransportFactory,
        clock: () -> Long = { now },
        delay: suspend (Long) -> Unit = {}
    ) = PairingRepository(
        storage = storage,
        secureRandom = CountingRandom(),
        transportFactory = factory,
        nowSeconds = clock,
        wallClockMillis = { clock() * 1000L },
        pollDelayMs = 1L,
        delayFn = delay
    )

    @Test
    fun `recovery state is durable before the single handshake`() = runTest {
        val transport = FakePairingTransport(offer, clientId)
        transport.onHandshake = {
            val record = store.load()
            assertNotNull("Recovery record must exist before handshake", record)
            assertEquals(1L, record?.nextPollSequence)
            assertFalse(record?.toJson().toString()?.contains("bearerToken") ?: true)
        }
        transport.handshakeOutcome = HandshakeOutcome.LOST_RESPONSE
        transport.pollScript.addAll(listOf("pending_approval", "approved"))

        val credentials = repository(StaticFactory(transport)).executePairing(
            offer = offer,
            onSasDerived = { /* SAS displayed after persistence */ },
            onRecovering = {}
        )

        assertEquals(1, transport.handshakeCalls)
        assertEquals(listOf(1L, 2L), transport.pollRequests.map { it.sequence })
        assertEquals(2, transport.pollRequests.map { Base64.getEncoder().encodeToString(it.requestNonce) }.toSet().size)
        assertEquals(sessionId, credentials.sessionId)
        assertEquals("192.168.1.50", credentials.hostIp)
        assertEquals(8443, credentials.port)
        assertEquals(certFp, credentials.certFingerprint)
        assertEquals(1L, credentials.nextSequence)
        assertNull("Recovery state clears only after verified persistence", store.load())
        assertNotNull(storage.loadCredentials())
    }

    @Test
    fun `no credentials are written while approval is still pending`() = runTest {
        val transport = FakePairingTransport(offer, clientId)
        transport.handshakeOutcome = HandshakeOutcome.LOST_RESPONSE
        var sawPending = false
        transport.onPoll = { request ->
            if (request.sequence == 1L) {
                sawPending = true
                assertNull(storage.loadCredentials())
                assertEquals(2L, store.load()?.nextPollSequence)
            }
        }
        transport.pollScript.addAll(listOf("pending_approval", "approved"))

        repository(StaticFactory(transport)).executePairing(offer, {}, {})
        assertTrue(sawPending)
    }

    @Test
    fun `process restart resumes with fresh sequence and never replays handshake`() = runTest {
        val context = CanonicalContext.build(
            version = "1",
            sessionId = sessionId,
            certFingerprint = certFp,
            serverNonce = serverNonce,
            clientNonce = ByteArray(32) { 0x11 },
            clientId = clientId
        )
        val derived = SessionKeyDerivation.derive(pairingSecret, ByteArray(32) { 0x11 }, context)
        val kPoll = SessionKeyDerivation.derivePollKey(pairingSecret, ByteArray(32) { 0x11 }, context)
        val record = PendingPairingRecovery(
            sessionId = sessionId,
            hostIp = offer.hostIp,
            port = offer.port,
            certFingerprint = certFp,
            kPoll = kPoll,
            upstreamKey = derived.upstreamKey,
            downstreamKey = derived.downstreamKey,
            contextBytes = context,
            sasCode = "123 456",
            deviceId = clientId,
            bearerTokenSha256 = PendingPairingRecovery.bearerTokenHash(derived.bearerToken),
            nextPollSequence = 7L,
            deadlineEpochSeconds = now + 90,
            createdAtEpochSeconds = now
        )
        assertTrue(store.save(record))

        val transport = FakePairingTransport(offer, clientId, derived.bearerToken)
        transport.pollScript.add("approved")
        val credentials = repository(StaticFactory(transport)).resumePendingPairing({}, {})!!

        assertEquals(0, transport.handshakeCalls)
        assertEquals(listOf(7L), transport.pollRequests.map { it.sequence })
        assertEquals(derived.bearerToken, credentials.bearerToken)
        assertNull(store.load())
    }

    @Test
    fun `desktop rejection clears recovery and never persists credentials`() = runTest {
        val transport = FakePairingTransport(offer, clientId)
        transport.handshakeOutcome = HandshakeOutcome.LOST_RESPONSE
        transport.pollScript.add("rejected")

        val error = expectPairingError {
            repository(StaticFactory(transport)).executePairing(offer, {}, {})
        }

        assertEquals("pairing_rejected", error.code)
        assertNull(storage.loadCredentials())
        assertNull(store.load())
        assertEquals(1, transport.handshakeCalls)
    }

    @Test
    fun `lost poll authentication is terminal and clears recovery`() = runTest {
        val transport = FakePairingTransport(offer, clientId)
        transport.handshakeOutcome = HandshakeOutcome.LOST_RESPONSE
        transport.pollError = PairingNetworkException("invalid_poll_auth", "401")

        val error = expectPairingError {
            repository(StaticFactory(transport)).executePairing(offer, {}, {})
        }

        assertEquals("pairing_expired", error.code)
        assertNull(storage.loadCredentials())
        assertNull(store.load())
    }

    @Test
    fun `expired recovery deadline clears state`() = runTest {
        var clock = now
        val transport = FakePairingTransport(offer, clientId)
        transport.handshakeOutcome = HandshakeOutcome.LOST_RESPONSE
        transport.pollScript.addAll(List(20) { "pending_approval" })

        val error = expectPairingError {
            repository(StaticFactory(transport), clock = { clock }, delay = { clock += 10 }).executePairing(
                offer,
                {},
                {}
            )
        }

        assertEquals("pairing_expired", error.code)
        assertNull(store.load())
        assertNull(storage.loadCredentials())
    }

    @Test
    fun `tampered V2 recovery response is rejected and clears state`() = runTest {
        val transport = FakePairingTransport(offer, clientId)
        transport.handshakeOutcome = HandshakeOutcome.LOST_RESPONSE
        transport.badSignature = true
        transport.pollScript.add("approved")

        val error = expectPairingError {
            repository(StaticFactory(transport)).executePairing(offer, {}, {})
        }

        assertEquals("pairing_response_invalid", error.code)
        assertNull(storage.loadCredentials())
        assertNull(store.load())
    }

    @Test
    fun `recovery record contains derived keys but never pairing secret or token`() = runTest {
        val transport = FakePairingTransport(offer, clientId)
        transport.handshakeOutcome = HandshakeOutcome.LOST_RESPONSE
        transport.pollScript.addAll(List(20) { "pending_approval" })

        try {
            repository(
                StaticFactory(transport),
                delay = { throw StopRecovery() }
            ).executePairing(offer, {}, {})
        } catch (expected: StopRecovery) {
            // Inspect the durable record while the recovery attempt is still live.
        }

        val json = requireNotNull(storage.loadPairingRecovery())
        val text = json.toString()
        assertTrue(json.has("kPollB64"))
        assertTrue(json.has("upstreamKeyB64"))
        assertTrue(json.has("downstreamKeyB64"))
        assertFalse(text.contains("pairingSecret"))
        assertFalse(text.contains("serverNonce"))
        assertFalse(text.contains("bearerToken"))
        assertFalse(text.contains(transport.lastToken ?: "sess_tok_never"))
    }

    @Test
    fun `rejected handshake does not enter polling and clears state`() = runTest {
        val transport = FakePairingTransport(offer, clientId)
        transport.handshakeOutcome = HandshakeOutcome.REJECTED

        val error = expectPairingError {
            repository(StaticFactory(transport)).executePairing(offer, {}, {})
        }

        assertEquals("rejected_by_desktop", error.code)
        assertEquals(0, transport.pollRequests.size)
        assertNull(store.load())
    }

    private suspend fun expectPairingError(block: suspend () -> Unit): PairingNetworkException {
        try {
            block()
            throw AssertionError("expected PairingNetworkException")
        } catch (e: PairingNetworkException) {
            return e
        }
    }

    private class StaticFactory(private val transport: PairingTransport) : PairingTransportFactory {
        override fun create(hostIp: String, port: Int, certFingerprint: String): PairingTransport =
            transport
    }

    private class StopRecovery : RuntimeException("test stop")

    private class CountingRandom : SecureRandom() {
        private var counter = 1

        override fun nextBytes(bytes: ByteArray) {
            for (index in bytes.indices) {
                bytes[index] = (counter and 0xFF).toByte()
                counter += 1
            }
        }
    }

    private enum class HandshakeOutcome { SUCCESS, LOST_RESPONSE, REJECTED }

    private class FakePairingTransport(
        private val offer: PairingOffer,
        private val knownDeviceId: String,
        private val resumeToken: String? = null
    ) : PairingTransport {
        var handshakeOutcome = HandshakeOutcome.SUCCESS
        var badSignature = false
        var pollError: IOException? = null
        val pollScript = ArrayDeque<String>()
        val pollRequests = mutableListOf<PairingPollRequest>()
        var handshakeCalls = 0
        var onHandshake: (() -> Unit)? = null
        var onPoll: ((PairingPollRequest) -> Unit)? = null
        var lastToken: String? = null
        private var clientId = knownDeviceId
        private var kPoll = ByteArray(32)
        private var derivedToken = ""

        override fun handshake(request: PairingHandshakeRequest): JSONObject {
            handshakeCalls += 1
            onHandshake?.invoke()
            val nonce = Base64.getUrlDecoder().decode(request.clientNonceB64)
            val context = CanonicalContext.build(
                version = "1",
                sessionId = offer.sessionId,
                certFingerprint = offer.certFingerprint,
                serverNonce = offer.serverNonce,
                clientNonce = nonce,
                clientId = request.clientId
            )
            val derived = SessionKeyDerivation.derive(offer.pairingSecret, nonce, context)
            clientId = request.clientId
            kPoll = SessionKeyDerivation.derivePollKey(offer.pairingSecret, nonce, context)
            derivedToken = derived.bearerToken
            lastToken = derivedToken

            when (handshakeOutcome) {
                HandshakeOutcome.LOST_RESPONSE -> throw IOException("simulated lost handshake response")
                HandshakeOutcome.REJECTED -> throw PairingNetworkException(
                    "rejected_by_desktop",
                    "rejected"
                )
                HandshakeOutcome.SUCCESS -> Unit
            }
            return signedResponse(
                context = "handshake",
                sequence = 0L,
                nonce = nonce,
                status = "approved",
                deviceId = clientId,
                token = derivedToken
            )
        }

        override fun poll(request: PairingPollRequest): JSONObject {
            pollRequests.add(request)
            kPoll = request.kPoll
            onPoll?.invoke(request)
            pollError?.let { throw it }
            val status = pollScript.removeFirstOrNull() ?: "pending_approval"
            if (status == "rejected") {
                throw PairingNetworkException("rejected_by_desktop", "rejected")
            }
            if (status == "expired") {
                throw PairingNetworkException("polling_window_closed", "window closed")
            }
            return signedResponse(
                context = "poll",
                sequence = request.sequence,
                nonce = request.requestNonce,
                status = status,
                deviceId = clientId,
                token = if (status == "approved") {
                    derivedToken.ifEmpty { resumeToken ?: lastToken ?: "" }
                } else {
                    null
                }
            )
        }

        override fun close() = Unit

        private fun signedResponse(
            context: String,
            sequence: Long,
            nonce: ByteArray,
            status: String,
            deviceId: String,
            token: String?
        ): JSONObject {
            val delivered = token != null
            val credentialHash = if (delivered) {
                PollSigner.computeCredentialSha256(token!!)
            } else {
                ByteArray(0)
            }
            val canonical = CanonicalPollResponse.build(
                version = "1",
                responseContext = context,
                sessionId = offer.sessionId,
                httpStatus = 200,
                sequence = sequence,
                requestNonce = nonce,
                status = status,
                expiresIn = 7200L,
                deviceId = deviceId,
                credentialState = if (delivered) "delivered" else "none",
                credentialSha256 = credentialHash
            )
            var signature = PollSigner.computeResponseSignatureV2(kPoll, canonical)
            if (badSignature) {
                signature = signature.copyOf().also { it[0] = (it[0].toInt() xor 0x01).toByte() }
            }
            val json = JSONObject()
                .put("ok", true)
                .put("response_context", context)
                .put("http_status", 200)
                .put("session_id", offer.sessionId)
                .put("sequence", sequence)
                .put("status", status)
                .put("expires_in", 7200L)
                .put("device_id", deviceId)
                .put("credential_state", if (delivered) "delivered" else "none")
                .put("credential_sha256", credentialHash.joinToString("") { "%02x".format(it) })
                .put("response_sig", signature.joinToString("") { "%02x".format(it) })
            if (token != null) json.put("session_token", token)
            return json
        }
    }
}
