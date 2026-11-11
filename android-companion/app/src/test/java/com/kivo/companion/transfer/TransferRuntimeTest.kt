package com.kivo.companion.transfer

import com.kivo.companion.model.SessionCredentials
import com.kivo.companion.storage.KeystoreSecretStorage
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
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
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey

class TransferRuntimeTest {

    private lateinit var storageDir: File
    private lateinit var cacheDir: File
    private lateinit var stagingDir: File
    private lateinit var storage: KeystoreSecretStorage
    private lateinit var resolver: FakeResolver
    private lateinit var preparer: UploadPreparer
    private lateinit var service: FakeServiceController
    private val sessionId = "sess_runtime_test_1"
    private val transferId = "tr_0123456789abcdef01234567"
    private val downloadTicketId = "tkt_" + "ab".repeat(32)
    private val downloadStageName = "kivo_download_${"c".repeat(32)}.part"
    private val spoolName = "kivo_spool_${"a".repeat(32)}.part"
    private val now = System.currentTimeMillis() / 1000L

    @Before
    fun setUp() {
        val root = Files.createTempDirectory("kivo_runtime_test_").toFile()
        storageDir = File(root, "files").also { it.mkdirs() }
        cacheDir = File(root, "cache").also { it.mkdirs() }
        stagingDir = File(root, "downloads").also { it.mkdirs() }
        val keyGen = KeyGenerator.getInstance("AES")
        keyGen.init(256)
        val key: SecretKey = keyGen.generateKey()
        storage = KeystoreSecretStorage(
            storageDirectory = storageDir,
            customKeyProvider = { key }
        )
        resolver = FakeResolver()
        preparer = UploadPreparer(cacheDir = cacheDir)
        service = FakeServiceController()
        assertTrue(
            storage.saveCredentials(
                SessionCredentials(
                    sessionId = sessionId,
                    deviceId = "client_runtime_test",
                    bearerToken = "sess_tok_runtime_test",
                    upstreamKey = ByteArray(32) { (it + 3).toByte() },
                    downstreamKey = ByteArray(32) { (it + 7).toByte() },
                    expiresAt = now + 7200,
                    nextSequence = 1L,
                    hostIp = "192.168.1.20",
                    port = 8443,
                    certFingerprint = "A".repeat(64)
                )
            )
        )
    }

    @After
    fun tearDown() {
        storageDir.parentFile?.deleteRecursively()
    }

    private fun runtime(
        stager: DownloadStager? = null,
        downloadResolver: DownloadDestinationResolver? = null
    ): TransferRuntime = TransferRuntime(
        storage = storage,
        resolver = resolver,
        preparer = preparer,
        transportFactory = NoNetworkTransportFactory,
        service = service,
        dispatcher = Dispatchers.Unconfined,
        unpairJoinTimeoutMs = 1_000L,
        unpairCancelTimeoutMs = 1_000L,
        downloadStager = stager,
        destinationResolver = downloadResolver
    )

    private fun downloadRuntime(
        resolver: DownloadDestinationResolver = FakeDestinationResolver()
    ): Pair<TransferRuntime, DownloadStager> {
        val stager = DownloadStager(directory = stagingDir)
        return runtime(stager = stager, downloadResolver = resolver) to stager
    }

    private fun record(
        state: String = "receiving",
        transferIdValue: String = transferId,
        initUncertain: Boolean = false,
        recordSessionId: String = sessionId,
        recordSpoolName: String? = spoolName
    ) = PersistedTransfer(
        sessionId = recordSessionId,
        transferId = transferIdValue,
        uri = "content://test/document/1",
        filename = "report.pdf",
        mimeType = "application/pdf",
        size = 2L * TransferValidation.CHUNK_BYTES,
        sha256Hex = "0".repeat(64),
        strategy = if (recordSpoolName != null) SourceStrategy.PRIVATE_SPOOL else SourceStrategy.REOPEN_TWO_PASS,
        spoolName = recordSpoolName,
        nextChunkIndex = 1L,
        receivedBytes = TransferValidation.CHUNK_BYTES,
        state = state,
        initUncertain = initUncertain,
        createdAt = now,
        updatedAt = now,
        expiresAt = now + 900L
    )

    private fun saveRecord(record: PersistedTransfer) {
        assertTrue(storage.saveTransferState(record.toJson()))
    }

    private fun createSpool(name: String = spoolName): File {
        val file = File(cacheDir, name)
        file.writeBytes(ByteArray(32) { 7 })
        return file
    }

    private fun createDownloadStage(
        name: String = downloadStageName,
        bytes: ByteArray = ByteArray((TransferValidation.CHUNK_BYTES + 1234).toInt()) { (it % 251).toByte() }
    ): File {
        val file = File(stagingDir, name)
        file.writeBytes(bytes)
        return file
    }

    private fun downloadRecord(
        state: String = "downloading",
        size: Long = TransferValidation.CHUNK_BYTES + 1234,
        nextChunkIndex: Long = 1L,
        receivedBytes: Long = TransferValidation.CHUNK_BYTES,
        completeAcknowledged: Boolean = false,
        stageNameValue: String = downloadStageName,
        sha256Hex: String = "0".repeat(64),
        recordSessionId: String = sessionId,
        recordDeviceId: String = "client_runtime_test"
    ) = PersistedDownload(
        sessionId = recordSessionId,
        ticketId = downloadTicketId,
        deviceId = recordDeviceId,
        filename = "desktop-file.bin",
        fileType = "application/octet-stream",
        size = size,
        sha256Hex = sha256Hex,
        nextChunkIndex = nextChunkIndex,
        receivedBytes = receivedBytes,
        stageName = stageNameValue,
        state = state,
        completeAcknowledged = completeAcknowledged,
        createdAt = now,
        updatedAt = now,
        leaseExpiresAt = now + 1800L
    )

    private fun saveDownloadRecord(record: PersistedDownload) {
        assertTrue(storage.saveDownloadState(record.toJson()))
    }

    @Test
    fun `restored interrupted download is resumable and keeps its staging copy`() {
        val stage = createDownloadStage()
        val orphan = createDownloadStage(
            name = "kivo_download_${"d".repeat(32)}.part",
            bytes = ByteArray(8)
        )
        saveDownloadRecord(downloadRecord())

        val (runtime, _) = downloadRuntime()

        assertEquals(DownloadPhase.INTERRUPTED, runtime.downloadSnapshot.value.phase)
        assertTrue(runtime.downloadSnapshot.value.resumable)
        assertEquals("desktop-file.bin", runtime.downloadSnapshot.value.filename)
        assertEquals(TransferValidation.CHUNK_BYTES, runtime.downloadSnapshot.value.receivedBytes)
        assertTrue(stage.exists())
        assertFalse("unreferenced download stage must be removed", orphan.exists())
        assertEquals(0, service.startCalls)
    }

    @Test
    fun `download record from a different pairing is purged with its staging file`() {
        val stage = createDownloadStage()
        saveDownloadRecord(downloadRecord(recordSessionId = "sess_previous_pairing"))

        val (runtime, _) = downloadRuntime()

        assertEquals(DownloadPhase.IDLE, runtime.downloadSnapshot.value.phase)
        assertNull(storage.loadDownloadState())
        assertFalse(stage.exists())
    }

    @Test
    fun `verified download awaiting publication restores without starting the service`() {
        val bytes = ByteArray(64) { (it * 3).toByte() }
        val stage = createDownloadStage(bytes = bytes)
        saveDownloadRecord(
            downloadRecord(
                state = "ready_to_publish",
                size = bytes.size.toLong(),
                nextChunkIndex = 1L,
                receivedBytes = bytes.size.toLong(),
                completeAcknowledged = true,
                sha256Hex = TransferCrypto.sha256Hex(bytes)
            )
        )

        val (runtime, _) = downloadRuntime()

        assertEquals(DownloadPhase.READY_TO_PUBLISH, runtime.downloadSnapshot.value.phase)
        assertTrue(runtime.downloadSnapshot.value.requiresDestination)
        assertTrue(stage.exists())
        assertEquals(0, service.startCalls)
    }

    @Test
    fun `publishing a verified download is a local copy and never starts the foreground service`() {
        val bytes = ByteArray(4096) { (it % 251).toByte() }
        val stage = createDownloadStage(bytes = bytes)
        val destination = FakeDestination()
        saveDownloadRecord(
            downloadRecord(
                state = "ready_to_publish",
                size = bytes.size.toLong(),
                nextChunkIndex = 1L,
                receivedBytes = bytes.size.toLong(),
                completeAcknowledged = true,
                sha256Hex = TransferCrypto.sha256Hex(bytes)
            )
        )
        // Restore projects the durable record; publication must stay local.
        val restored = runtime(
            stager = DownloadStager(stagingDir),
            downloadResolver = FakeDestinationResolver(destination)
        )

        assertTrue(restored.publishDownload("content://test/create"))

        assertEquals(DownloadPhase.COMPLETED, restored.downloadSnapshot.value.phase)
        assertTrue(restored.downloadSnapshot.value.destinationVerified == true)
        assertArrayEquals(bytes, destination.written.toByteArray())
        assertFalse(stage.exists())
        assertNull(storage.loadDownloadState())
        assertEquals("publication is a local copy, not a connected-device transfer", 0, service.startCalls)
        assertFalse(restored.active.value)
    }

    @Test
    fun `an upload is refused while a download record awaits explicit action`() {
        createDownloadStage()
        saveDownloadRecord(downloadRecord())
        val (runtime, _) = downloadRuntime()

        assertFalse(runtime.startUploads(listOf("content://test/document/1")))

        assertEquals(TransferPhase.FAILED, runtime.snapshot.value.phase)
        assertEquals("download_pending", runtime.snapshot.value.errorCode)
        assertEquals(0, service.startCalls)
        assertFalse(runtime.active.value)
    }

    @Test
    fun `a download is refused while an upload record is durable`() {
        saveRecord(record())
        val (runtime, _) = downloadRuntime()
        val offer = DownloadOffer(
            ticketId = downloadTicketId,
            deviceId = "client_runtime_test",
            filename = "desktop-file.bin",
            size = 1024L,
            sha256Hex = "0".repeat(64)
        )

        assertFalse(runtime.startDownload(offer))

        assertEquals(DownloadPhase.FAILED, runtime.downloadSnapshot.value.phase)
        assertEquals("upload_pending", runtime.downloadSnapshot.value.errorCode)
        assertEquals(0, service.startCalls)
        assertFalse(runtime.active.value)
    }

    @Test
    fun `unpair shutdown removes download state even without a live session`() = runBlocking {
        val stage = createDownloadStage()
        saveDownloadRecord(downloadRecord())
        val (runtime, _) = downloadRuntime()
        assertEquals(DownloadPhase.INTERRUPTED, runtime.downloadSnapshot.value.phase)

        runtime.shutdownForUnpair()

        assertEquals(DownloadPhase.IDLE, runtime.downloadSnapshot.value.phase)
        assertNull(storage.loadDownloadState())
        assertFalse(stage.exists())
    }

    @Test
    fun `restored interrupted transfer is resumable and keeps its staging copy`() {
        val spool = createSpool()
        val orphan = createSpool("kivo_spool_${"b".repeat(32)}.part")
        saveRecord(record())

        val runtime = runtime()

        assertEquals(TransferPhase.INTERRUPTED, runtime.snapshot.value.phase)
        assertTrue(runtime.snapshot.value.resumable)
        assertEquals("report.pdf", runtime.snapshot.value.filename)
        assertEquals(TransferValidation.CHUNK_BYTES, runtime.snapshot.value.receivedBytes)
        assertTrue(spool.exists())
        assertFalse("unreferenced spool must be removed", orphan.exists())
        assertEquals(0, service.startCalls)
    }

    @Test
    fun `restored init unknown record is never offered for resume`() {
        val spool = createSpool()
        saveRecord(
            record(
                state = "initiating",
                transferIdValue = "",
                initUncertain = true
            )
        )

        val runtime = runtime()

        assertEquals(TransferPhase.INIT_OUTCOME_UNKNOWN, runtime.snapshot.value.phase)
        assertFalse(runtime.snapshot.value.resumable)
        assertTrue(spool.exists())
    }

    @Test
    fun `record from a different pairing is purged with its spool`() {
        val spool = createSpool()
        saveRecord(record(recordSessionId = "sess_previous_pairing"))

        val runtime = runtime()

        assertEquals(TransferPhase.IDLE, runtime.snapshot.value.phase)
        assertNull(storage.loadTransferState())
        assertFalse(spool.exists())
    }

    @Test
    fun `reconcile with a new session purges the old interrupted transfer`() {
        val spool = createSpool()
        saveRecord(record())
        val runtime = runtime()
        assertEquals(TransferPhase.INTERRUPTED, runtime.snapshot.value.phase)

        runtime.reconcileSession("sess_new_pairing")

        assertEquals(TransferPhase.IDLE, runtime.snapshot.value.phase)
        assertNull(storage.loadTransferState())
        assertFalse(spool.exists())
    }

    @Test
    fun `dismissing an unknown init removes the encrypted record and staging`() = runBlocking {
        val spool = createSpool()
        saveRecord(
            record(
                state = "initiating",
                transferIdValue = "",
                initUncertain = true
            )
        )
        val runtime = runtime()
        assertEquals(TransferPhase.INIT_OUTCOME_UNKNOWN, runtime.snapshot.value.phase)

        runtime.dismiss()

        assertEquals(TransferPhase.IDLE, runtime.snapshot.value.phase)
        assertNull(storage.loadTransferState())
        assertFalse(spool.exists())
        assertFalse(runtime.active.value)
        assertTrue(service.stopCalls >= 1)
    }

    @Test
    fun `unpair shutdown removes interrupted state even without a live session`() = runBlocking {
        val spool = createSpool()
        saveRecord(
            record(
                state = "initiating",
                transferIdValue = "",
                initUncertain = true
            )
        )
        val runtime = runtime()

        runtime.shutdownForUnpair()

        assertEquals(TransferPhase.IDLE, runtime.snapshot.value.phase)
        assertNull(storage.loadTransferState())
        assertFalse(spool.exists())
    }

    @Test
    fun `reconcile without credentials removes a stale record`() {
        val spool = createSpool()
        saveRecord(record())
        val runtime = runtime()
        storage.clearCredentialsOnly()

        runtime.reconcileWithoutSession()

        assertEquals(TransferPhase.IDLE, runtime.snapshot.value.phase)
        assertNull(storage.loadTransferState())
        assertFalse(spool.exists())
    }

    @Test
    fun `describe returns safe metadata or a structured rejection`() {
        val runtime = runtime()
        resolver.descriptors["content://test/good"] = DocumentDescriptor(
            uri = "content://test/good",
            displayName = "photo.jpg",
            sizeBytes = 2048L,
            mimeType = "image/jpeg",
            isDirectory = false,
            persistable = true,
            reopenable = true
        )
        resolver.descriptors["content://test/bad"] = DocumentDescriptor(
            uri = "content://test/bad",
            displayName = "../escape",
            sizeBytes = 10L,
            mimeType = "text/plain",
            isDirectory = false,
            persistable = true,
            reopenable = true
        )

        val good = runtime.describe("content://test/good")
        assertTrue(good.supported)
        assertEquals("photo.jpg", good.displayName)

        val bad = runtime.describe("content://test/bad")
        assertFalse(bad.supported)
        assertEquals("invalid_filename", bad.errorCode)

        val missing = runtime.describe("content://test/missing")
        assertFalse(missing.supported)
        assertEquals("source_unavailable", missing.errorCode)
    }

    @Test
    fun `empty or non content selection never starts an operation`() {
        val runtime = runtime()
        assertFalse(runtime.startUploads(emptyList()))
        assertFalse(runtime.startUploads(listOf("file:///data/local/tmp/x")))
        assertEquals(0, service.startCalls)
        assertFalse(runtime.active.value)
    }

    @Test
    fun `acknowledge only clears terminal state when no durable record exists`() {
        saveRecord(record())
        val runtime = runtime()
        assertEquals(TransferPhase.INTERRUPTED, runtime.snapshot.value.phase)
        runtime.acknowledgeOutcome()
        assertEquals(TransferPhase.INTERRUPTED, runtime.snapshot.value.phase)
    }

    private class FakeDestination : DownloadDestination {
        val written = ByteArrayOutputStream()
        var deleted = false

        override val uri: String = "content://test/create"

        override fun displayName(): String = "saved.bin"

        override fun openSink(): DownloadSink = object : DownloadSink {
            override fun write(buffer: ByteArray, offset: Int, length: Int) {
                written.write(buffer, offset, length)
            }

            override fun flushAndSync() = Unit
            override fun close() = Unit
        }

        override fun reopenInput(): InputStream = ByteArrayInputStream(written.toByteArray())

        override fun delete(): Boolean {
            deleted = true
            written.reset()
            return true
        }
    }

    private class FakeDestinationResolver(
        private val destination: DownloadDestination = FakeDestination()
    ) : DownloadDestinationResolver {
        override fun resolve(uri: String): DownloadDestination = destination
    }

    private class FakeResolver : UploadSourceResolver {
        val descriptors = mutableMapOf<String, DocumentDescriptor>()

        override fun resolve(uri: String): UploadSource {
            val descriptor = descriptors[uri]
                ?: throw TransferSourceException("source_unavailable", "missing test descriptor")
            return object : UploadSource {
                override val descriptor: DocumentDescriptor = descriptor
                override fun openStream(): InputStream = ByteArrayInputStream(ByteArray(0))
            }
        }
    }

    private class FakeServiceController : TransferServiceController {
        var startCalls = 0
        var stopCalls = 0

        override fun start() {
            startCalls += 1
        }

        override fun stop() {
            stopCalls += 1
        }
    }

    private object NoNetworkTransportFactory : TransferTransportFactory {
        override fun create(credentials: SessionCredentials): TransferTransport =
            object : TransferTransport {
                override fun execute(request: TransferTransportRequest): RawTransferHttpResponse {
                    throw IOException("unexpected network call in runtime test")
                }

                override fun cancelActiveCall() = Unit
                override fun close() = Unit
            }
    }
}
