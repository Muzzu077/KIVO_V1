package com.kivo.companion.transfer

import com.kivo.companion.model.SessionCredentials
import com.kivo.companion.storage.KeystoreSecretStorage
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.nio.file.Files
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey

/**
 * End-to-end download engine tests against a fake desktop that validates every
 * request MAC with `K_up`, signs every response with `K_down`, and enforces
 * sequence, nonce, chunk geometry, and completion rules exactly like the Python
 * backend.
 */
class DownloadCoordinatorTest {

    private val mib = DOWNLOAD_CHUNK_BYTES.toInt()
    private val ticketId = "tkt_" + "ab".repeat(32)
    private val sessionId = "sess_0123456789abcdef"
    private val deviceId = "client_download_test"
    private val kUp = ByteArray(32) { (it + 11).toByte() }
    private val kDown = ByteArray(32) { (it + 91).toByte() }
    private val certFp = "A".repeat(64)

    private lateinit var tempDir: File
    private lateinit var storageDir: File
    private lateinit var stagingDir: File
    private lateinit var storage: KeystoreSecretStorage
    private lateinit var stager: DownloadStager
    private lateinit var resolver: FakeResolver
    private lateinit var desktop: FakeDesktop
    private lateinit var credentials: SessionCredentials
    private var nowSeconds: Long = 0L
    private var snapshots = mutableListOf<DownloadSnapshot>()
    private var fileBytes = ByteArray(mib + 1234) { (it % 251).toByte() }

    @Before
    fun setUp() {
        tempDir = Files.createTempDirectory("download_coordinator_").toFile()
        storageDir = File(tempDir, "storage").also { it.mkdirs() }
        stagingDir = File(tempDir, "staging").also { it.mkdirs() }
        val keyGen = KeyGenerator.getInstance("AES")
        keyGen.init(256)
        val key: SecretKey = keyGen.generateKey()
        storage = KeystoreSecretStorage(
            storageDirectory = storageDir,
            customKeyProvider = { key }
        )
        nowSeconds = System.currentTimeMillis() / 1000L
        credentials = SessionCredentials(
            sessionId = sessionId,
            deviceId = deviceId,
            bearerToken = "sess_tok_test_only_not_a_mac_key",
            upstreamKey = kUp,
            downstreamKey = kDown,
            expiresAt = nowSeconds + 7200,
            nextSequence = 1L,
            hostIp = "192.168.1.50",
            port = 8443,
            certFingerprint = certFp
        )
        assertTrue(storage.saveCredentials(credentials))
        desktop = FakeDesktop(
            sessionId = sessionId,
            deviceId = deviceId,
            ticketId = ticketId,
            fileBytes = fileBytes,
            kUp = kUp,
            kDown = kDown,
            nowSeconds = { nowSeconds },
            nextSequence = { storage.loadCredentials()?.nextSequence ?: -1L }
        )
        stager = DownloadStager(
            directory = stagingDir,
            freeSpaceBytes = { 2L * 1024L * 1024L * 1024L }
        )
        resolver = FakeResolver()
    }

    @After
    fun tearDown() {
        tempDir.deleteRecursively()
    }

    private fun offer(
        bytes: ByteArray = fileBytes,
        ticket: String = ticketId,
        device: String = deviceId
    ): DownloadOffer = DownloadOffer(
        ticketId = ticket,
        deviceId = device,
        filename = "report.bin",
        size = bytes.size.toLong(),
        sha256Hex = TransferCrypto.sha256Hex(bytes)
    )

    private fun coordinator(
        maxFailures: Int = 2,
        onSnapshotExtra: ((DownloadSnapshot) -> Unit)? = null
    ): DownloadCoordinator = DownloadCoordinator(
        storage = storage,
        stager = stager,
        destinationResolver = resolver,
        publisher = DownloadPublisher(),
        transportFactory = object : TransferTransportFactory {
            override fun create(credentials: SessionCredentials): TransferTransport = desktop
        },
        onSnapshot = { snapshot ->
            snapshots.add(snapshot)
            onSnapshotExtra?.invoke(snapshot)
        },
        credentialsLoader = { storage.loadCredentials() },
        sequenceManager = TransferSequenceManager(storage),
        recoveryStore = DownloadRecoveryStore(storage),
        nowSeconds = { nowSeconds },
        clockMillis = { nowSeconds * 1000L },
        delayFn = { delayMs -> nowSeconds += (delayMs / 1000L).coerceAtLeast(1L) },
        maxNetworkFailures = maxFailures,
        secureRandom = SecureRandom()
    )

    @Test
    fun `happy path verifies chunks completes and waits for an explicit destination`() = runBlocking {
        coordinator().start(offer())

        assertEquals("completed", desktop.state)
        assertEquals(listOf(0L, 1L), desktop.chunkRequests)
        assertEquals(1, desktop.completeCalls)
        assertEquals(0, resolver.resolveCalls)
        assertEquals(DownloadPhase.READY_TO_PUBLISH, snapshots.last().phase)
        assertTrue(snapshots.any { it.phase == DownloadPhase.PREPARING })
        assertTrue(snapshots.any { it.phase == DownloadPhase.DOWNLOADING })
        assertTrue(snapshots.any { it.phase == DownloadPhase.VERIFYING })

        val record = DownloadRecoveryStore(storage).load()
        assertNotNull(record)
        assertEquals("ready_to_publish", record!!.state)
        assertTrue(record.completeAcknowledged)
        assertEquals(fileBytes.size.toLong(), record.receivedBytes)
        assertEquals(2L, record.nextChunkIndex)
        assertNotNull(stager.stageByName(record.stageName))

        assertTrue(desktop.requestSequences.zipWithNext().all { (a, b) -> b > a })
        assertEquals(desktop.requestSequences.size, desktop.requestNonces.size)
        assertTrue(desktop.initBodyOnlyTicket)

        coordinator().publish("content://test/document/1")

        assertEquals(1, resolver.resolveCalls)
        assertEquals(DownloadPhase.COMPLETED, snapshots.last().phase)
        assertEquals("saved.bin", snapshots.last().publishedName)
        assertEquals(true, snapshots.last().destinationVerified)
        assertTrue(
            fileBytes.contentEquals(resolver.destinations.single().sink.written.toByteArray())
        )
        assertNull(DownloadRecoveryStore(storage).load())
        assertEquals(0, stagingDir.listFiles()?.size ?: 0)
    }

    @Test
    fun `empty file has zero chunks and publishes an empty destination`() = runBlocking {
        fileBytes = ByteArray(0)
        desktop.fileBytes = fileBytes

        coordinator().start(offer())

        assertEquals(0, desktop.chunkRequests.size)
        assertEquals(1, desktop.completeCalls)
        assertEquals(DownloadPhase.READY_TO_PUBLISH, snapshots.last().phase)

        coordinator().publish("content://test/document/empty")

        assertEquals(DownloadPhase.COMPLETED, snapshots.last().phase)
        assertEquals(true, snapshots.last().destinationVerified)
        assertEquals(0, resolver.destinations.single().sink.written.size())
    }

    @Test
    fun `tampered chunk bytes are rejected and never completed or published`() = runBlocking {
        desktop.tamperNextChunk = true

        coordinator().start(offer())

        assertEquals(DownloadPhase.FAILED, snapshots.last().phase)
        assertEquals("invalid_download_response", snapshots.last().errorCode)
        assertEquals(0, desktop.completeCalls)
        assertEquals(0, resolver.resolveCalls)
        assertNull(DownloadRecoveryStore(storage).load())
        assertEquals(0, stagingDir.listFiles()?.size ?: 0)
    }

    @Test
    fun `whole-file digest mismatch fails before desktop completion`() = runBlocking {
        desktop.corruptWholeFileBytes = true

        coordinator().start(offer())

        assertEquals(DownloadPhase.FAILED, snapshots.last().phase)
        assertEquals("download_integrity_failed", snapshots.last().errorCode)
        assertEquals(0, desktop.completeCalls)
        assertEquals(0, resolver.resolveCalls)
        assertNull(DownloadRecoveryStore(storage).load())
        assertEquals(0, stagingDir.listFiles()?.size ?: 0)
    }

    @Test
    fun `signed metadata mismatch is rejected before any chunk`() = runBlocking {
        desktop.filenameOverride = "other.bin"

        coordinator().start(offer())

        assertEquals(DownloadPhase.FAILED, snapshots.last().phase)
        assertEquals("invalid_download_response", snapshots.last().errorCode)
        assertEquals(0, desktop.chunkRequests.size)
        assertEquals(0, desktop.completeCalls)
    }

    @Test
    fun `signed digest or device mismatch is rejected before any chunk`() = runBlocking {
        desktop.shaOverride = "0".repeat(64)
        coordinator().start(offer())
        assertEquals(DownloadPhase.FAILED, snapshots.last().phase)
        assertEquals(0, desktop.chunkRequests.size)

        desktop.resetForNewAttempt()
        desktop.shaOverride = null
        desktop.deviceOverride = "client_other"
        snapshots = mutableListOf()
        coordinator().start(offer())
        assertEquals(DownloadPhase.FAILED, snapshots.last().phase)
        assertEquals(0, desktop.chunkRequests.size)
    }

    @Test
    fun `tampered signed init response never advances state`() = runBlocking {
        desktop.tamperNextInitResponse = true

        coordinator().start(offer())

        assertEquals(DownloadPhase.FAILED, snapshots.last().phase)
        assertEquals("invalid_response_signature", snapshots.last().errorCode)
        assertEquals(0, desktop.chunkRequests.size)
        assertEquals(0, desktop.completeCalls)
        assertNull(DownloadRecoveryStore(storage).load())
    }

    @Test
    fun `device mismatch and malformed ticket are rejected without a request`() = runBlocking {
        coordinator().start(offer(device = "client_other_device"))

        assertEquals(DownloadPhase.FAILED, snapshots.last().phase)
        assertEquals("handoff_device_mismatch", snapshots.last().errorCode)
        assertEquals(0, desktop.initCalls)

        snapshots = mutableListOf()
        coordinator().start(offer(ticket = "tkt_" + "A".repeat(64)))

        assertEquals(DownloadPhase.FAILED, snapshots.last().phase)
        assertEquals("invalid_ticket", snapshots.last().errorCode)
        assertEquals(0, desktop.initCalls)
    }

    @Test
    fun `signed expiry is terminal and removes private staging`() = runBlocking {
        desktop.refuseInitCode = "download_expired"

        coordinator().start(offer())

        assertEquals(DownloadPhase.EXPIRED, snapshots.last().phase)
        assertEquals("download_expired", snapshots.last().errorCode)
        assertEquals(0, desktop.chunkRequests.size)
        assertNull(DownloadRecoveryStore(storage).load())
        assertEquals(0, stagingDir.listFiles()?.size ?: 0)
    }

    @Test
    fun `out-of-order chunk response is reconciled with a signed status query`() = runBlocking {
        desktop.forceOutOfOrderOnce = true

        coordinator().start(offer())

        assertEquals("completed", desktop.state)
        assertTrue(desktop.initCalls >= 2)
        assertEquals(listOf(0L, 0L, 1L), desktop.chunkRequests)
        assertEquals(1, desktop.completeCalls)
        assertEquals(DownloadPhase.READY_TO_PUBLISH, snapshots.last().phase)
    }

    @Test
    fun `lost chunk acknowledgement resumes from reconciled signed progress`() = runBlocking {
        desktop.failChunkAcksAfterCommit = 1

        coordinator(maxFailures = 0).start(offer())

        assertEquals(DownloadPhase.INTERRUPTED, snapshots.last().phase)
        val interrupted = DownloadRecoveryStore(storage).load()
        assertNotNull(interrupted)
        assertEquals(0L, interrupted!!.nextChunkIndex)
        assertNotNull(stager.stageByName(interrupted.stageName))

        val beforeResume = desktop.initCalls
        coordinator().resume()

        assertEquals("completed", desktop.state)
        assertTrue(desktop.initCalls > beforeResume)
        assertEquals(listOf(0L, 0L, 1L), desktop.chunkRequests)
        assertEquals(1, desktop.completeCalls)
        assertEquals(0, resolver.resolveCalls)
        assertEquals(DownloadPhase.READY_TO_PUBLISH, snapshots.last().phase)

        coordinator().publish("content://test/document/1")
        assertEquals(DownloadPhase.COMPLETED, snapshots.last().phase)
    }

    @Test
    fun `restored interrupted download requires explicit resume and never publishes`() = runBlocking {
        desktop.lostInitResponses = 1

        coordinator(maxFailures = 0).start(offer())
        assertEquals(DownloadPhase.INTERRUPTED, snapshots.last().phase)
        val record = DownloadRecoveryStore(storage).load()
        assertNotNull(record)

        val fresh = coordinator()
        val restored = fresh.restoredSnapshot()
        assertNotNull(restored)
        assertEquals(DownloadPhase.INTERRUPTED, restored!!.phase)
        assertTrue(restored.resumable)
        assertEquals(0, resolver.resolveCalls)

        // No network or destination work happened just by restoring the view.
        assertEquals(1, desktop.initCalls)
        assertEquals(0, desktop.completeCalls)
        assertNull(fresh.restoredSnapshot()?.publishedName)
    }

    @Test
    fun `publication failure keeps the verified staging copy and retries explicitly`() = runBlocking {
        coordinator().start(offer())
        assertEquals(DownloadPhase.READY_TO_PUBLISH, snapshots.last().phase)
        val stageName = DownloadRecoveryStore(storage).load()!!.stageName
        assertNotNull(stager.stageByName(stageName))

        resolver.failCode = "destination_unavailable"
        coordinator().publish("content://test/document/1")

        assertEquals(DownloadPhase.READY_TO_PUBLISH, snapshots.last().phase)
        assertEquals("destination_unavailable", snapshots.last().errorCode)
        assertEquals("ready_to_publish", DownloadRecoveryStore(storage).load()!!.state)
        assertNotNull(stager.stageByName(stageName))

        resolver.failCode = null
        val fresh = coordinator()
        assertEquals(DownloadPhase.READY_TO_PUBLISH, fresh.restoredSnapshot()!!.phase)

        fresh.publish("content://test/document/2")

        assertEquals(DownloadPhase.COMPLETED, snapshots.last().phase)
        assertEquals(true, snapshots.last().destinationVerified)
        assertNull(DownloadRecoveryStore(storage).load())
        assertNull(stager.stageByName(stageName))
    }

    @Test
    fun `interrupted publication returns to the picker without overwrite`() = runBlocking {
        coordinator().start(offer())
        val store = DownloadRecoveryStore(storage)
        val ready = store.load()!!
        assertTrue(store.save(ready.copy(state = "publishing")))

        val fresh = coordinator()
        val restored = fresh.restoredSnapshot()
        assertNotNull(restored)
        assertEquals(DownloadPhase.READY_TO_PUBLISH, restored!!.phase)
        assertTrue(restored.message.contains("Choose the destination again"))
        assertEquals(0, resolver.resolveCalls)

        fresh.publish("content://test/document/3")
        assertEquals(DownloadPhase.COMPLETED, snapshots.last().phase)
        assertEquals(1, resolver.resolveCalls)
    }

    @Test
    fun `cancel during download sends a signed cancel and deletes staging`() = runBlocking {
        lateinit var running: DownloadCoordinator
        var requested = false
        running = coordinator(
            onSnapshotExtra = { snapshot ->
                if (!requested &&
                    snapshot.phase == DownloadPhase.DOWNLOADING &&
                    desktop.nextChunkIndex == 1L
                ) {
                    requested = true
                    running.requestCancel()
                }
            }
        )

        running.start(offer())

        assertTrue(requested)
        assertEquals(DownloadPhase.CANCELLED, snapshots.last().phase)
        assertEquals("cancelled", snapshots.last().errorCode)
        assertEquals(1, desktop.cancelCalls)
        assertEquals("cancelled", desktop.state)
        assertNull(DownloadRecoveryStore(storage).load())
        assertEquals(0, stagingDir.listFiles()?.size ?: 0)
    }

    @Test
    fun `cancel while interrupted removes staging without publishing`() = runBlocking {
        desktop.lostInitResponses = 1
        coordinator(maxFailures = 0).start(offer())
        assertEquals(DownloadPhase.INTERRUPTED, snapshots.last().phase)

        coordinator().cancelNow()

        assertEquals(DownloadPhase.CANCELLED, snapshots.last().phase)
        assertNull(DownloadRecoveryStore(storage).load())
        assertEquals(0, stagingDir.listFiles()?.size ?: 0)
        assertEquals(0, resolver.resolveCalls)
    }

    @Test
    fun `resume of a record bound to another device is rejected`() = runBlocking {
        desktop.lostInitResponses = 1
        coordinator(maxFailures = 0).start(offer())
        assertEquals(DownloadPhase.INTERRUPTED, snapshots.last().phase)

        assertTrue(storage.saveCredentials(credentials.copy(deviceId = "client_other")))
        coordinator().resume()

        assertEquals(DownloadPhase.FAILED, snapshots.last().phase)
        assertEquals("handoff_device_mismatch", snapshots.last().errorCode)
        assertNull(DownloadRecoveryStore(storage).load())
        assertEquals(0, stagingDir.listFiles()?.size ?: 0)
    }

    @Test
    fun `resume of a record from another session is rejected`() = runBlocking {
        desktop.lostInitResponses = 1
        coordinator(maxFailures = 0).start(offer())
        assertEquals(DownloadPhase.INTERRUPTED, snapshots.last().phase)

        assertTrue(
            storage.saveCredentials(
                credentials.copy(
                    sessionId = "sess_ffffffffffffffff",
                    expiresAt = nowSeconds + 7200
                )
            )
        )
        coordinator().resume()

        assertEquals(DownloadPhase.FAILED, snapshots.last().phase)
        assertEquals("session_expired", snapshots.last().errorCode)
        assertNull(DownloadRecoveryStore(storage).load())
    }

    private class FakeResolver : DownloadDestinationResolver {
        var resolveCalls = 0
        var failCode: String? = null
        val destinations = mutableListOf<FakeDestination>()

        override fun resolve(uri: String): DownloadDestination {
            resolveCalls += 1
            failCode?.let { throw DownloadPublicationException(it, "simulated destination failure") }
            return FakeDestination(uri).also { destinations.add(it) }
        }
    }

    private class FakeDestination(private val value: String) : DownloadDestination {
        override val uri: String get() = value
        val sink = FakeSink()
        var deleted = false

        override fun displayName(): String = "saved.bin"

        override fun openSink(): DownloadSink = sink

        override fun reopenInput(): InputStream = ByteArrayInputStream(sink.written.toByteArray())

        override fun delete(): Boolean {
            deleted = true
            return true
        }
    }

    private class FakeSink : DownloadSink {
        val written = ByteArrayOutputStream()
        var flushed = false
        var closed = false

        override fun write(buffer: ByteArray, offset: Int, length: Int) {
            written.write(buffer, offset, length)
        }

        override fun flushAndSync() {
            flushed = true
        }

        override fun close() {
            closed = true
        }
    }

    private inner class FakeDesktop(
        private val sessionId: String,
        private val deviceId: String,
        private val ticketId: String,
        var fileBytes: ByteArray,
        private val kUp: ByteArray,
        private val kDown: ByteArray,
        private val nowSeconds: () -> Long,
        private val nextSequence: () -> Long
    ) : TransferTransport {
        var state = "none"
        var nextChunkIndex = 0L
        var bytesSent = 0L
        var initCalls = 0
        var statusCalls = 0
        var completeCalls = 0
        var cancelCalls = 0
        val chunkRequests = mutableListOf<Long>()
        val requestSequences = mutableListOf<Long>()
        val requestNonces = mutableSetOf<String>()
        var initBodyOnlyTicket = true

        var lostInitResponses = 0
        var failChunkAcksAfterCommit = 0
        var forceOutOfOrderOnce = false
        var tamperNextInitResponse = false
        var tamperNextChunk = false
        var corruptWholeFileBytes = false
        var refuseInitCode: String? = null
        var filenameOverride: String? = null
        var shaOverride: String? = null
        var deviceOverride: String? = null

        private var lastSequence = 0L
        private var lastNonce = ByteArray(16)
        private var lastEndpoint = ""

        fun resetForNewAttempt() {
            state = "none"
            nextChunkIndex = 0L
            bytesSent = 0L
            initCalls = 0
            statusCalls = 0
            completeCalls = 0
            cancelCalls = 0
            chunkRequests.clear()
            lastSequence = 0L
            lastNonce = ByteArray(16)
            lastEndpoint = ""
            lostInitResponses = 0
            failChunkAcksAfterCommit = 0
            forceOutOfOrderOnce = false
            tamperNextInitResponse = false
            tamperNextChunk = false
            corruptWholeFileBytes = false
            refuseInitCode = null
            filenameOverride = null
            shaOverride = null
            deviceOverride = null
        }

        override fun execute(request: TransferTransportRequest): RawTransferHttpResponse {
            validateRequest(request)
            lastEndpoint = request.endpoint
            return when {
                request.endpoint == "/api/v1/transfers/downloads/init" -> handleInit(request)
                request.endpoint.endsWith("/complete") -> handleComplete()
                request.endpoint.endsWith("/cancel") -> handleCancel()
                request.method == "GET" && request.endpoint.contains("/chunks/") -> handleChunk(request)
                else -> handleStatus()
            }
        }

        private fun validateRequest(request: TransferTransportRequest) {
            assertTrue("sequence must be monotonic", request.sequence > lastSequence)
            assertEquals(
                "sequence must be durably advanced before transmit",
                request.sequence + 1L,
                nextSequence()
            )
            lastSequence = request.sequence
            lastNonce = request.nonce
            requestSequences.add(request.sequence)
            assertTrue(
                "nonce must be unique",
                requestNonces.add(TransferCrypto.encodeNonceB64(request.nonce))
            )

            val body = request.body ?: ByteArray(0)
            assertEquals(body.size.toLong(), request.contentLength)
            assertArrayEquals(TransferCrypto.sha256(body), request.contentSha256)
            assertTrue(
                "timestamp must be fresh",
                kotlin.math.abs(request.timestamp - nowSeconds()) <= 120.0
            )
            val canonical = TransferCrypto.buildCanonicalRequest(
                version = "1",
                sessionId = sessionId,
                method = request.method,
                endpoint = request.endpoint,
                sequence = request.sequence,
                nonce = request.nonce,
                timestamp = request.timestamp,
                contentLength = request.contentLength,
                contentSha256 = request.contentSha256
            )
            assertTrue(
                "request MAC must verify with K_up",
                TransferCrypto.verifyRequestSignature(kUp, canonical, request.signature)
            )
        }

        private fun handleInit(request: TransferTransportRequest): RawTransferHttpResponse {
            initCalls += 1
            val body = JSONObject(String(request.body!!, Charsets.UTF_8))
            initBodyOnlyTicket = initBodyOnlyTicket &&
                body.keys().asSequence().toSet() == setOf("ticket_id") &&
                body.getString("ticket_id") == ticketId

            refuseInitCode?.let { code ->
                state = if (code == "download_expired") "expired" else "failed"
                val http = if (code == "download_expired") 410 else 403
                return signed(
                    context = "download_init",
                    httpStatus = http,
                    state = state,
                    payload = publicPayload().put("error_code", code).put("error_message", code)
                )
            }

            if (state == "none") state = "claimed"
            if (lostInitResponses > 0) {
                lostInitResponses -= 1
                throw IOException("simulated lost init response")
            }
            var response = signed("download_init", 200, state, publicPayload())
            if (tamperNextInitResponse) {
                tamperNextInitResponse = false
                val json = JSONObject(response.bodyText)
                json.put("sig", "0".repeat(64))
                response = response.copy(bodyText = json.toString())
            }
            return response
        }

        private fun handleStatus(): RawTransferHttpResponse {
            statusCalls += 1
            if (state == "none") {
                return signed(
                    context = "download_status",
                    httpStatus = 404,
                    state = "error",
                    payload = JSONObject()
                        .put("download_id", ticketId)
                        .put("error_code", "download_not_found")
                        .put("error_message", "not found")
                )
            }
            return signed("download_status", 200, state, publicPayload())
        }

        private fun handleChunk(request: TransferTransportRequest): RawTransferHttpResponse {
            val index = Regex("/chunks/(\\d+)$").find(request.endpoint)!!.groupValues[1].toLong()
            chunkRequests.add(index)
            val totalChunks = totalDownloadChunks(fileBytes.size.toLong())
            if (index >= totalChunks) {
                return signedError("download_chunk", 400, "invalid_chunk_index", "streaming")
            }
            if (forceOutOfOrderOnce && index == 0L) {
                forceOutOfOrderOnce = false
                return signedError("download_chunk", 409, "chunk_out_of_order", state)
            }
            if (index > nextChunkIndex) {
                return signedError("download_chunk", 409, "chunk_out_of_order", state)
            }
            if (state == "claimed") state = "streaming"
            if (index < nextChunkIndex) {
                // Re-request of an already committed chunk. Return the same
                // bytes without advancing progress on the desktop.
                return chunkResponse(index)
            }

            val offset = index * DOWNLOAD_CHUNK_BYTES
            val length = expectedDownloadChunkLength(fileBytes.size.toLong(), index).toInt()
            val data = fileBytes.copyOfRange(offset.toInt(), offset.toInt() + length)
            if (tamperNextChunk) {
                tamperNextChunk = false
                val originalDigest = TransferCrypto.sha256Hex(data)
                data[0] = (data[0].toInt() xor 0x01).toByte()
                return chunkResponse(index, data, originalDigest)
            }
            if (corruptWholeFileBytes) {
                data[0] = (data[0].toInt() xor 0x01).toByte()
            }
            bytesSent += data.size
            nextChunkIndex = index + 1L
            if (failChunkAcksAfterCommit > 0) {
                failChunkAcksAfterCommit -= 1
                throw IOException("simulated lost chunk acknowledgement after commit")
            }
            return chunkResponse(index, data, TransferCrypto.sha256Hex(data))
        }

        private fun chunkResponse(
            index: Long,
            data: ByteArray? = null,
            chunkSha: String? = null
        ): RawTransferHttpResponse {
            val total = fileBytes.size.toLong()
            val offset = index * DOWNLOAD_CHUNK_BYTES
            val length = expectedDownloadChunkLength(total, index).toInt()
            val actual = data ?: fileBytes.copyOfRange(offset.toInt(), offset.toInt() + length)
            val payload = publicPayload()
                .put("chunk_index", index)
                .put("offset", offset)
                .put("length", actual.size)
                .put("chunk_sha256", chunkSha ?: TransferCrypto.sha256Hex(actual))
                .put("chunk_b64", Base64.getEncoder().encodeToString(actual))
            return signed("download_chunk", 200, state, payload)
        }

        private fun handleComplete(): RawTransferHttpResponse {
            completeCalls += 1
            val total = fileBytes.size.toLong()
            if (state !in setOf("claimed", "streaming") ||
                nextChunkIndex != totalDownloadChunks(total) ||
                bytesSent != total
            ) {
                return signed(
                    context = "download_complete",
                    httpStatus = 409,
                    state = state,
                    payload = publicPayload()
                        .put("error_code", "download_incomplete")
                        .put("error_message", "desktop still reports missing chunks")
                )
            }
            state = "completed"
            return signed("download_complete", 200, state, publicPayload())
        }

        private fun handleCancel(): RawTransferHttpResponse {
            cancelCalls += 1
            if (state in setOf("completed", "cancelled", "expired", "failed")) {
                return signed(
                    context = "download_cancel",
                    httpStatus = 409,
                    state = state,
                    payload = publicPayload()
                        .put("error_code", "invalid_download_state")
                        .put("error_message", "terminal")
                )
            }
            state = "cancelled"
            return signed("download_cancel", 200, state, publicPayload())
        }

        fun publicPayload(): JSONObject {
            val total = fileBytes.size.toLong()
            val totalChunks = totalDownloadChunks(total)
            return JSONObject().apply {
                put("download_id", ticketId)
                put("device_id", deviceOverride ?: deviceId)
                put("device_name", "Pixel Download")
                put("filename", filenameOverride ?: "report.bin")
                put("file_type", "application/octet-stream")
                put("size", total)
                put("sha256", shaOverride ?: TransferCrypto.sha256Hex(fileBytes))
                put("state", state)
                put("bytes_sent", bytesSent)
                put("next_chunk_index", nextChunkIndex)
                put("total_chunks", totalChunks)
                put("chunk_size", DOWNLOAD_CHUNK_BYTES)
                put("progress_percent", if (total == 0L) 0 else (bytesSent * 100L / total).toInt())
                put("created_at", nowSeconds().toDouble())
                put("updated_at", nowSeconds().toDouble())
                put("expires_at", nowSeconds().toDouble() + 300.0)
                put("claim_expires_at", nowSeconds().toDouble() + 300.0)
                put("active_expires_at", nowSeconds().toDouble() + 1800.0)
                put("error_code", JSONObject.NULL)
                put("error_message", JSONObject.NULL)
            }
        }

        private fun signedError(
            context: String,
            httpStatus: Int,
            code: String,
            state: String
        ): RawTransferHttpResponse = signed(
            context = context,
            httpStatus = httpStatus,
            state = state,
            payload = publicPayload()
                .put("error_code", code)
                .put("error_message", code)
                .put("expected_chunk_index", nextChunkIndex)
        )

        private fun signed(
            context: String,
            httpStatus: Int,
            state: String,
            payload: JSONObject
        ): RawTransferHttpResponse {
            val payloadBytes = payload.toString().toByteArray(Charsets.UTF_8)
            val payloadHash = TransferCrypto.sha256(payloadBytes)
            val canonical = TransferCrypto.buildCanonicalResponse(
                version = "1",
                responseContext = context,
                sessionId = sessionId,
                httpStatus = httpStatus,
                sequence = lastSequence,
                nonce = lastNonce,
                state = state,
                transferId = ticketId,
                payloadLength = payloadBytes.size.toLong(),
                payloadSha256 = payloadHash
            )
            val signature = TransferCrypto.computeResponseSignature(kDown, canonical)
            val envelope = JSONObject()
                .put("ok", httpStatus in 200..299)
                .put("v", "1")
                .put("ctx", context)
                .put("sid", sessionId)
                .put("seq", lastSequence)
                .put("nonce_b64", TransferCrypto.encodeNonceB64(lastNonce))
                .put("http_status", httpStatus)
                .put("state", state)
                .put("tid", ticketId)
                .put("payload_b64", Base64.getEncoder().encodeToString(payloadBytes))
                .put("payload_sha256", TransferCrypto.toHex(payloadHash))
                .put("sig", TransferCrypto.toHex(signature))
            return RawTransferHttpResponse(httpStatus, envelope.toString())
        }

        override fun cancelActiveCall() = Unit
        override fun close() = Unit
    }
}
