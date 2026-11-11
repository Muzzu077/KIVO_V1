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

class UploadCoordinatorTest {

    private val mib = TransferValidation.CHUNK_BYTES.toInt()

    private lateinit var tempDir: File
    private lateinit var cacheDir: File
    private lateinit var storage: KeystoreSecretStorage
    private lateinit var credentials: SessionCredentials
    private lateinit var desktop: FakeDesktop
    private lateinit var preparer: UploadPreparer
    private lateinit var resolver: TestResolver
    private var nowSeconds: Long = 0L
    private var snapshots = mutableListOf<TransferSnapshot>()
    private var reopenable = true
    private var sourceBytes = ByteArray(mib + 12_345) { (it % 251).toByte() }

    private val sessionId = "sess_0123456789abcdef"
    private val kUp = ByteArray(32) { (it + 11).toByte() }
    private val kDown = ByteArray(32) { (it + 91).toByte() }
    private val certFp = "A".repeat(64)

    @Before
    fun setUp() {
        tempDir = Files.createTempDirectory("coordinator_test_").toFile()
        cacheDir = File(tempDir, "cache").also { it.mkdirs() }
        val keyGen = KeyGenerator.getInstance("AES")
        keyGen.init(256)
        val key: SecretKey = keyGen.generateKey()
        storage = KeystoreSecretStorage(
            storageDirectory = tempDir,
            customKeyProvider = { key }
        )
        nowSeconds = System.currentTimeMillis() / 1000L
        credentials = SessionCredentials(
            sessionId = sessionId,
            deviceId = "client_test_device",
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
            kUp = kUp,
            kDown = kDown,
            nowSeconds = { nowSeconds },
            nextSequence = { storage.loadCredentials()?.nextSequence ?: -1L }
        )
        resolver = TestResolver()
        preparer = UploadPreparer(
            cacheDir = cacheDir,
            freeSpaceBytes = { 2L * 1024L * 1024L * 1024L }
        )
    }

    @After
    fun tearDown() {
        tempDir.deleteRecursively()
    }

    private fun coordinator(maxFailures: Int = 2): UploadCoordinator = UploadCoordinator(
        storage = storage,
        resolver = resolver,
        preparer = preparer,
        transportFactory = object : TransferTransportFactory {
            override fun create(credentials: SessionCredentials): TransferTransport = desktop
        },
        onSnapshot = { snapshots.add(it) },
        credentialsLoader = { storage.loadCredentials() },
        sequenceManager = TransferSequenceManager(storage),
        queueStore = TransferQueueStore(storage),
        nowSeconds = { nowSeconds },
        clockMillis = { nowSeconds * 1000L },
        delayFn = { delayMs -> nowSeconds += (delayMs / 1000L).coerceAtLeast(1L) },
        pollIntervalMs = 1L,
        maxConsecutiveNetworkFailures = maxFailures,
        secureRandom = SecureRandom()
    )

    private fun approveAndAccept(acceptAfterPolls: Int = 0): (FakeDesktop) -> Unit {
        var approvalPolls = 0
        var finalPolls = 0
        return { d ->
            when (d.state) {
                "awaiting_desktop_approval" -> {
                    approvalPolls += 1
                    if (approvalPolls > 0) d.approve()
                }
                "awaiting_final_acceptance" -> {
                    if (finalPolls >= acceptAfterPolls) d.acceptFinal()
                    finalPolls += 1
                }
            }
        }
    }

    @Test
    fun `happy path waits for both gates and streams fixed chunks`() = runBlocking {
        desktop.onStatus = approveAndAccept(acceptAfterPolls = 2)

        coordinator().startUploads(listOf("content://test/document/1"))

        assertEquals("committed", desktop.state)
        assertEquals(2, desktop.chunkCalls)
        assertEquals(listOf(0L, 1L), desktop.chunkIndices)
        assertEquals(desktop.size, desktop.receivedBytes)
        assertEquals(0, desktop.chunksBeforeApproval)
        assertEquals(desktop.size, desktop.chunkData.values.sumOf { it.size.toLong() })
        assertTrue(desktop.requestSequences.zipWithNext().all { (a, b) -> b > a })
        assertEquals(1L, desktop.requestSequences.first())
        assertNull(TransferQueueStore(storage).load())
        assertEquals("report.pdf", snapshots.last().publishedFilename)
        assertEquals(TransferPhase.COMMITTED, snapshots.last().phase)
        assertTrue(snapshots.any { it.phase == TransferPhase.WAITING_DESKTOP_APPROVAL })
        assertTrue(snapshots.any { it.phase == TransferPhase.RECEIVING })
        assertTrue(snapshots.any { it.phase == TransferPhase.AWAITING_FINAL_ACCEPTANCE })
    }

    @Test
    fun `empty file completes without chunk requests`() = runBlocking {
        sourceBytes = ByteArray(0)
        desktop.onStatus = approveAndAccept()

        coordinator().startUploads(listOf("content://test/empty"))

        assertEquals("committed", desktop.state)
        assertEquals(0, desktop.chunkCalls)
        assertEquals(TransferPhase.COMMITTED, snapshots.last().phase)
    }

    @Test
    fun `lost chunk ack is reconciled without resending the committed chunk`() = runBlocking {
        desktop.dropNextChunkAckAfterCommit = true
        desktop.onStatus = approveAndAccept()

        coordinator().startUploads(listOf("content://test/document/1"))

        assertEquals("committed", desktop.state)
        assertEquals(listOf(0L, 1L), desktop.chunkIndices)
        assertEquals(2, desktop.chunkCalls)
        assertEquals(2, desktop.appliedChunks)
        assertEquals(desktop.size, desktop.receivedBytes)
    }

    @Test
    fun `lost chunk before commit is retried with a fresh sequence`() = runBlocking {
        desktop.dropNextChunkBeforeCommit = true
        desktop.onStatus = approveAndAccept()

        coordinator().startUploads(listOf("content://test/document/1"))

        assertEquals("committed", desktop.state)
        assertEquals(listOf(0L, 0L, 1L), desktop.chunkIndices)
        assertEquals(2, desktop.appliedChunks)
        assertEquals(desktop.size, desktop.receivedBytes)
        assertTrue(desktop.requestSequences.zipWithNext().all { (a, b) -> b > a })
    }

    @Test
    fun `lost status response is retried and does not abort approval`() = runBlocking {
        desktop.statusNetworkFailuresRemaining = 1
        desktop.onStatus = approveAndAccept()

        coordinator().startUploads(listOf("content://test/document/1"))

        assertEquals("committed", desktop.state)
        assertTrue(desktop.statusCalls >= 2)
        assertTrue(snapshots.last().phase == TransferPhase.COMMITTED)
    }

    @Test
    fun `lost init response is persisted as outcome unknown and never retried`() = runBlocking {
        desktop.dropInitResponse = true

        coordinator().startUploads(listOf("content://test/document/1"))

        assertEquals(1, desktop.initCalls)
        assertEquals(0, desktop.statusCalls)
        assertEquals(0, desktop.chunkCalls)
        assertEquals(TransferPhase.INIT_OUTCOME_UNKNOWN, snapshots.last().phase)
        val record = TransferQueueStore(storage).load()
        assertNotNull(record)
        assertTrue(record!!.initUncertain)
        assertTrue(record.transferId.isEmpty())

        coordinator().resume()
        assertEquals("No automatic retry after an ambiguous init", 1, desktop.initCalls)
        assertEquals(0, desktop.statusCalls)
        assertEquals(TransferPhase.INIT_OUTCOME_UNKNOWN, snapshots.last().phase)

        coordinator().dismiss()
        assertNull(TransferQueueStore(storage).load())
    }

    @Test
    fun `authenticated init refusal is terminal and clears local state`() = runBlocking {
        desktop.refuseInit = true

        coordinator().startUploads(listOf("content://test/document/1"))

        assertEquals(0, desktop.chunkCalls)
        assertEquals(TransferPhase.FAILED, snapshots.last().phase)
        assertEquals("invalid_params", snapshots.last().errorCode)
        assertNull(TransferQueueStore(storage).load())
    }

    @Test
    fun `desktop rejection during approval is terminal`() = runBlocking {
        desktop.onStatus = { d ->
            if (d.state == "awaiting_desktop_approval") d.reject()
        }

        coordinator().startUploads(listOf("content://test/document/1"))

        assertEquals("rejected", desktop.state)
        assertEquals(0, desktop.chunkCalls)
        assertEquals(TransferPhase.REJECTED, snapshots.last().phase)
        assertNull(TransferQueueStore(storage).load())
    }

    @Test
    fun `discard at final acceptance is not reported as success`() = runBlocking {
        var approved = false
        desktop.onStatus = { d ->
            if (d.state == "awaiting_desktop_approval") d.approve()
            if (d.state == "awaiting_final_acceptance") {
                d.discardFinal()
                approved = true
            }
        }

        coordinator().startUploads(listOf("content://test/document/1"))

        assertTrue(approved)
        assertEquals(TransferPhase.REJECTED, snapshots.last().phase)
        assertTrue(snapshots.none { it.phase == TransferPhase.COMMITTED })
        assertNull(TransferQueueStore(storage).load())
    }

    @Test
    fun `process restart resumes at the first uncommitted chunk with fresh sequences`() = runBlocking {
        desktop.failChunkOnceAfterChunksApplied = true
        desktop.failStatusAfterFirstChunk = true
        desktop.onStatus = approveAndAccept()

        coordinator(maxFailures = 1).startUploads(listOf("content://test/document/1"))

        assertEquals(TransferPhase.INTERRUPTED, snapshots.last().phase)
        val interrupted = TransferQueueStore(storage).load()
        assertNotNull(interrupted)
        assertEquals(1L, interrupted!!.nextChunkIndex)
        assertEquals(TransferValidation.CHUNK_BYTES, interrupted.receivedBytes)
        assertEquals(1, desktop.appliedChunks)

        val sequencesBeforeResume = desktop.requestSequences.toList()
        desktop.failChunkOnceAfterChunksApplied = false
        desktop.failStatusAfterFirstChunk = false
        desktop.statusNetworkFailuresRemaining = 0

        snapshots = mutableListOf()
        coordinator().resume()

        assertEquals("committed", desktop.state)
        assertEquals(1, desktop.initCalls)
        assertEquals(1, desktop.chunkIndices.count { it == 0L })
        assertEquals(2, desktop.appliedChunks)
        assertNull(TransferQueueStore(storage).load())
        assertTrue(desktop.requestSequences.last() > sequencesBeforeResume.last())
        assertEquals(TransferPhase.COMMITTED, snapshots.last().phase)
    }

    @Test
    fun `tampered signed status response is rejected and never updates progress`() = runBlocking {
        desktop.tamperNextStatusResponse = true
        desktop.onStatus = approveAndAccept()

        coordinator().startUploads(listOf("content://test/document/1"))

        assertEquals(0, desktop.chunkCalls)
        assertEquals(TransferPhase.FAILED, snapshots.last().phase)
        assertEquals("invalid_response_signature", snapshots.last().errorCode)
        assertNull(TransferQueueStore(storage).load())
    }

    @Test
    fun `source change between hashing and streaming is rejected by desktop verification`() = runBlocking {
        val originalFirstChunk = sourceBytes.copyOfRange(0, mib)
        var mutated = false
        desktop.onStatus = { d ->
            if (d.state == "awaiting_desktop_approval") {
                if (!mutated) {
                    sourceBytes[0] = (sourceBytes[0].toInt() xor 0x01).toByte()
                    mutated = true
                }
                d.approve()
            }
            if (d.state == "awaiting_final_acceptance") d.acceptFinal()
        }

        coordinator().startUploads(listOf("content://test/document/1"))

        assertTrue(mutated)
        assertFalse(originalFirstChunk.contentEquals(sourceBytes.copyOfRange(0, mib)))
        assertEquals("failed", desktop.state)
        assertEquals(TransferPhase.FAILED, snapshots.last().phase)
        assertTrue(snapshots.none { it.phase == TransferPhase.COMMITTED })
        assertNull(TransferQueueStore(storage).load())
    }

    @Test
    fun `cancellation sends a signed cancel and deletes private staging`() = runBlocking {
        reopenable = false
        lateinit var running: UploadCoordinator
        running = coordinator()
        desktop.onStatus = { d ->
            if (d.state == "awaiting_desktop_approval") {
                d.approve()
                running.requestCancel()
            }
        }

        running.startUploads(listOf("content://test/document/1"))

        assertEquals("cancelled", desktop.state)
        assertEquals(1, desktop.cancelCalls)
        assertEquals(TransferPhase.CANCELLED, snapshots.last().phase)
        assertNull(TransferQueueStore(storage).load())
        assertEquals(0, cacheDir.listFiles()?.size ?: 0)
    }

    @Test
    fun `interrupted private staging survives for resume and is deleted on dismiss`() = runBlocking {
        reopenable = false
        desktop.dropInitResponse = true

        coordinator().startUploads(listOf("content://test/document/1"))

        assertEquals(1, cacheDir.listFiles()?.size ?: 0)
        assertEquals(TransferPhase.INIT_OUTCOME_UNKNOWN, snapshots.last().phase)

        coordinator().dismiss()
        assertEquals(0, cacheDir.listFiles()?.size ?: 0)
        assertNull(TransferQueueStore(storage).load())
    }

    private inner class TestResolver : UploadSourceResolver {
        override fun resolve(uri: String): UploadSource {
            val declaredSize = sourceBytes.size.toLong()
            val descriptor = DocumentDescriptor(
                uri = uri,
                displayName = "report.pdf",
                sizeBytes = declaredSize,
                mimeType = "text/plain",
                isDirectory = false,
                persistable = reopenable,
                reopenable = reopenable
            )
            return object : UploadSource {
                override val descriptor: DocumentDescriptor = descriptor

                override fun openStream(): InputStream = ByteArrayInputStream(sourceBytes)
            }
        }
    }

    private class FakeDesktop(
        private val sessionId: String,
        private val kUp: ByteArray,
        private val kDown: ByteArray,
        private val nowSeconds: () -> Long,
        private val nextSequence: () -> Long
    ) : TransferTransport {
        var state = "none"
        var transferId = ""
        var filename = ""
        var size = 0L
        var receivedBytes = 0L
        var nextChunkIndex = 0L
        var sha256Hex = ""
        var mime: String? = null
        var errorCode: String? = null
        var publishedFilename: String? = null
        var expiresAt = nowSeconds().toDouble() + 60.0

        var initCalls = 0
        var statusCalls = 0
        var chunkCalls = 0
        var cancelCalls = 0
        var appliedChunks = 0
        var chunksBeforeApproval = 0
        val chunkIndices = mutableListOf<Long>()
        val requestSequences = mutableListOf<Long>()
        val chunkData = mutableMapOf<Long, ByteArray>()
        val requestNonces = mutableSetOf<String>()
        var onStatus: ((FakeDesktop) -> Unit)? = null

        var dropInitResponse = false
        var refuseInit = false
        var dropNextChunkAckAfterCommit = false
        var dropNextChunkBeforeCommit = false
        var failChunkOnceAfterChunksApplied = false
        var failStatusAfterFirstChunk = false
        var statusNetworkFailuresRemaining = 0
        var tamperNextStatusResponse = false

        private var lastSequence = 0L
        private var lastNonce = ByteArray(16)
        private var lastEndpoint = ""
        private var failedChunkOnce = false

        override fun execute(request: TransferTransportRequest): RawTransferHttpResponse {
            validateRequest(request)
            return when {
                request.endpoint == "/api/v1/transfers/uploads/init" -> handleInit(request)
                request.endpoint.endsWith("/cancel") -> handleCancel()
                request.method == "GET" -> handleStatus()
                else -> handleChunk(request)
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
            lastEndpoint = request.endpoint
            requestSequences.add(request.sequence)

            val nonceB64 = TransferCrypto.encodeNonceB64(request.nonce)
            assertTrue("nonce must be unique", requestNonces.add(nonceB64))

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
            filename = body.getString("filename")
            size = body.getLong("size")
            sha256Hex = body.getString("sha256")
            mime = if (body.has("mime")) body.getString("mime") else null

            if (refuseInit) {
                return signed(
                    context = "upload_init",
                    httpStatus = 400,
                    state = "error",
                    tid = "",
                    payload = JSONObject()
                        .put("error_code", "invalid_params")
                        .put("error_message", "refused by test")
                )
            }

            transferId = "tr_0123456789abcdef01234567"
            state = "awaiting_desktop_approval"
            receivedBytes = 0L
            nextChunkIndex = 0L
            errorCode = null
            publishedFilename = null
            expiresAt = nowSeconds().toDouble() + 60.0
            if (dropInitResponse) {
                dropInitResponse = false
                throw IOException("simulated lost init response")
            }
            return signed("upload_init", 202, state, transferId, publicPayload())
        }

        private fun handleStatus(): RawTransferHttpResponse {
            statusCalls += 1
            if (statusNetworkFailuresRemaining > 0) {
                statusNetworkFailuresRemaining -= 1
                throw IOException("simulated lost status response")
            }
            if (transferId.isEmpty() || state == "none") {
                return signed(
                    "upload_status",
                    404,
                    "error",
                    transferId,
                    JSONObject()
                        .put("transfer_id", transferId)
                        .put("error_code", "transfer_not_found")
                        .put("error_message", "not found")
                )
            }
            onStatus?.invoke(this)
            val response = signed("upload_status", 200, state, transferId, publicPayload())
            if (tamperNextStatusResponse) {
                tamperNextStatusResponse = false
                val json = JSONObject(response.bodyText)
                json.put("sig", "0".repeat(64))
                return response.copy(bodyText = json.toString())
            }
            return response
        }

        private fun handleChunk(request: TransferTransportRequest): RawTransferHttpResponse {
            chunkCalls += 1
            val index = Regex("/chunks/(\\d+)$").find(request.endpoint)!!.groupValues[1].toLong()
            chunkIndices.add(index)
            if (state != "receiving") {
                chunksBeforeApproval += 1
                return signedError("upload_chunk", 409, "invalid_transfer_state")
            }
            val data = request.body ?: ByteArray(0)
            val totalChunks = TransferValidation.totalChunks(size)
            if (index >= totalChunks) {
                return signedError("upload_chunk", 400, "invalid_chunk_index")
            }
            val offset = index * TransferValidation.CHUNK_BYTES
            val expectedLength = minOf(
                TransferValidation.CHUNK_BYTES,
                size - offset
            ).toInt()
            if (data.size != expectedLength) {
                val code = if (index == totalChunks - 1) {
                    "final_chunk_length_mismatch"
                } else {
                    "chunk_length_mismatch"
                }
                return signedError("upload_chunk", 400, code)
            }

            val digest = TransferCrypto.sha256(data)
            if (index < nextChunkIndex) {
                val existing = chunkData[index]
                if (existing != null && MessageDigest.isEqual(TransferCrypto.sha256(existing), digest)) {
                    return signed("upload_chunk", 200, state, transferId, publicPayload())
                }
                return signedError("upload_chunk", 409, "chunk_conflict")
            }
            if (index > nextChunkIndex) {
                return signedError("upload_chunk", 409, "chunk_out_of_order")
            }
            if (!MessageDigest.isEqual(digest, request.contentSha256)) {
                return signedError("upload_chunk", 422, "chunk_hash_mismatch")
            }
            if (dropNextChunkBeforeCommit) {
                dropNextChunkBeforeCommit = false
                throw IOException("simulated lost chunk before commit")
            }
            if (failChunkOnceAfterChunksApplied && appliedChunks >= 1 && !failedChunkOnce) {
                failedChunkOnce = true
                throw IOException("simulated lost chunk before commit")
            }

            chunkData[index] = data
            receivedBytes += data.size
            nextChunkIndex = index + 1L
            appliedChunks += 1
            if (failStatusAfterFirstChunk && appliedChunks == 1) {
                statusNetworkFailuresRemaining = 100
            }

            if (receivedBytes == size) {
                verifyWholeFile()
            }

            if (dropNextChunkAckAfterCommit) {
                dropNextChunkAckAfterCommit = false
                throw IOException("simulated lost chunk ack after commit")
            }
            return signed("upload_chunk", 200, state, transferId, publicPayload())
        }

        private fun handleCancel(): RawTransferHttpResponse {
            cancelCalls += 1
            if (state in setOf("committed", "rejected", "cancelled", "expired", "failed", "discarded")) {
                return signedError("upload_cancel", 409, "invalid_transfer_state")
            }
            state = "cancelled"
            errorCode = null
            return signed("upload_cancel", 200, state, transferId, publicPayload())
        }

        private fun verifyWholeFile() {
            val out = ByteArrayOutputStream()
            val totalChunks = TransferValidation.totalChunks(size)
            for (index in 0 until totalChunks) {
                val chunk = chunkData[index]
                if (chunk == null) {
                    state = "failed"
                    errorCode = "missing_chunk"
                    return
                }
                out.write(chunk)
            }
            val actual = TransferCrypto.sha256Hex(out.toByteArray())
            if (!actual.equals(sha256Hex, ignoreCase = true)) {
                state = "failed"
                errorCode = "integrity_failed"
                return
            }
            state = "awaiting_final_acceptance"
            errorCode = null
            expiresAt = nowSeconds().toDouble() + 900.0
        }

        fun approve() {
            if (state == "awaiting_desktop_approval") {
                state = "receiving"
                if (size == 0L) verifyWholeFile()
            }
        }

        fun reject() {
            if (state == "awaiting_desktop_approval") state = "rejected"
        }

        fun acceptFinal() {
            if (state == "awaiting_final_acceptance") {
                state = "committed"
                publishedFilename = filename
                errorCode = null
            }
        }

        fun discardFinal() {
            if (state == "awaiting_final_acceptance") {
                state = "discarded"
                errorCode = "discarded"
            }
        }

        fun publicPayload(): JSONObject = JSONObject().apply {
            put("transfer_id", transferId)
            put("filename", filename)
            put("size", size)
            put("received_bytes", receivedBytes)
            put("progress_percent", if (size == 0L) 100 else ((receivedBytes * 100L) / size).toInt())
            put("state", state)
            put("device_name", "Pixel Test")
            put("mime", mime ?: JSONObject.NULL)
            put("sha256", sha256Hex)
            put("error_code", errorCode ?: JSONObject.NULL)
            put("published_filename", publishedFilename ?: JSONObject.NULL)
            put("chunk_size", TransferValidation.CHUNK_BYTES)
            put("next_chunk_index", nextChunkIndex)
            put("created_at", nowSeconds().toDouble())
            put("updated_at", nowSeconds().toDouble())
            put("expires_at", expiresAt)
        }

        private fun signedError(
            context: String,
            httpStatus: Int,
            code: String
        ): RawTransferHttpResponse {
            val tid = Regex("/uploads/(tr_[0-9a-f]{24})").find(lastEndpoint)
                ?.groupValues?.get(1) ?: ""
            if (state == "none" || state == "error" || transferId.isEmpty()) {
                return signed(
                    context,
                    httpStatus,
                    "error",
                    tid,
                    JSONObject()
                        .put("transfer_id", tid)
                        .put("error_code", code)
                        .put("error_message", code)
                )
            }
            val payload = publicPayload()
                .put("error_code", code)
                .put("error_message", code)
            if (code == "chunk_out_of_order") {
                payload.put("expected_chunk_index", nextChunkIndex)
            }
            return signed(context, httpStatus, state, transferId, payload)
        }

        private fun signed(
            context: String,
            httpStatus: Int,
            state: String,
            tid: String,
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
                transferId = tid,
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
                .put("tid", tid)
                .put("payload_b64", Base64.getEncoder().encodeToString(payloadBytes))
                .put("payload_sha256", TransferCrypto.toHex(payloadHash))
                .put("sig", TransferCrypto.toHex(signature))
            return RawTransferHttpResponse(httpStatus, envelope.toString())
        }

        override fun cancelActiveCall() = Unit
        override fun close() = Unit
    }
}
