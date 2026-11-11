package com.kivo.companion.transfer

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
import java.io.File
import java.io.InputStream
import java.nio.file.Files

class UploadSourceTest {

    private lateinit var cacheDir: File
    private var freeBytes = 1024L * 1024L * 1024L

    @Before
    fun setUp() {
        cacheDir = Files.createTempDirectory("kivo_source_test_").toFile()
    }

    @After
    fun tearDown() {
        cacheDir.deleteRecursively()
    }

    private fun preparer(minFree: Long = 64L * 1024L * 1024L) = UploadPreparer(
        cacheDir = cacheDir,
        freeSpaceBytes = { freeBytes },
        minFreeBytes = minFree
    )

    private class ByteSource(
        override val descriptor: DocumentDescriptor,
        private val bytes: ByteArray,
        private val failOpen: Boolean = false
    ) : UploadSource {
        var opens = 0

        override fun openStream(): InputStream {
            opens += 1
            if (failOpen) {
                throw TransferSourceException("source_unavailable", "provider unavailable")
            }
            return ByteArrayInputStream(bytes)
        }
    }

    private fun descriptor(
        name: String? = "report.pdf",
        size: Long? = 10L,
        mime: String? = "application/pdf",
        directory: Boolean = false,
        persistable: Boolean = true,
        reopenable: Boolean = true
    ) = DocumentDescriptor(
        uri = "content://test/document/1",
        displayName = name,
        sizeBytes = size,
        mimeType = mime,
        isDirectory = directory,
        persistable = persistable,
        reopenable = reopenable
    )

    @Test
    fun `reopenable source is hashed in a first pass without copying`() {
        val bytes = ByteArray(64 * 1024) { (it % 251).toByte() }
        val source = ByteSource(descriptor(size = bytes.size.toLong()), bytes)

        val prepared = preparer().prepare(source)

        assertEquals(SourceStrategy.REOPEN_TWO_PASS, prepared.strategy)
        assertNull(prepared.spoolName)
        assertEquals(bytes.size.toLong(), prepared.size)
        assertEquals(TransferCrypto.sha256Hex(bytes), prepared.sha256Hex)
        assertEquals(1, source.opens)
        assertEquals(0, cacheDir.listFiles()?.size ?: 0)
        prepared.openStreamForTest().use { it.readBytes() }
    }

    @Test
    fun `non reopenable source is spooled once with bounded buffers`() {
        val bytes = ByteArray(100_000) { (it * 7 % 251).toByte() }
        val source = ByteSource(
            descriptor(size = bytes.size.toLong(), persistable = false, reopenable = false),
            bytes
        )

        val prepared = preparer().prepare(source)

        assertEquals(SourceStrategy.PRIVATE_SPOOL, prepared.strategy)
        assertNotNull(prepared.spoolName)
        assertEquals(bytes.size.toLong(), prepared.size)
        assertEquals(TransferCrypto.sha256Hex(bytes), prepared.sha256Hex)
        val spool = File(cacheDir, prepared.spoolName!!)
        assertTrue(spool.isFile)
        assertArrayEquals(bytes, spool.readBytes())
        assertEquals(1, source.opens)

        prepared.cleanup()
        assertFalse(spool.exists())
    }

    @Test
    fun `unknown size source is spooled safely`() {
        val bytes = ByteArray(32_000) { (it % 113).toByte() }
        val source = ByteSource(descriptor(size = null, reopenable = false), bytes)

        val prepared = preparer().prepare(source)

        assertEquals(SourceStrategy.PRIVATE_SPOOL, prepared.strategy)
        assertEquals(bytes.size.toLong(), prepared.size)
        assertEquals(TransferCrypto.sha256Hex(bytes), prepared.sha256Hex)
    }

    @Test
    fun `oversized source is rejected before any stream is opened`() {
        val source = ByteSource(
            descriptor(size = TransferValidation.MAX_UPLOAD_BYTES + 1L),
            ByteArray(0),
            failOpen = true
        )
        try {
            preparer().prepare(source)
            throw AssertionError("expected size rejection")
        } catch (e: TransferValidationException) {
            assertEquals("size_exceeds_limit", e.code)
        }
        assertEquals(0, source.opens)
    }

    @Test
    fun `invalid filename is rejected before any stream is opened`() {
        val source = ByteSource(
            descriptor(name = "../escape.txt"),
            ByteArray(4),
            failOpen = true
        )
        try {
            preparer().prepare(source)
            throw AssertionError("expected filename rejection")
        } catch (e: TransferValidationException) {
            assertEquals("invalid_filename", e.code)
        }
        assertEquals(0, source.opens)
    }

    @Test
    fun `directory sources are rejected`() {
        val source = ByteSource(descriptor(directory = true), ByteArray(0), failOpen = true)
        try {
            preparer().prepare(source)
            throw AssertionError("expected directory rejection")
        } catch (e: TransferSourceException) {
            assertEquals("source_is_directory", e.code)
        }
    }

    @Test
    fun `low storage rejects spool before reading`() {
        freeBytes = 1024L
        val source = ByteSource(
            descriptor(size = 10_000L, reopenable = false, persistable = false),
            ByteArray(10_000)
        )
        try {
            preparer(minFree = 64L * 1024L * 1024L).prepare(source)
            throw AssertionError("expected storage rejection")
        } catch (e: TransferSourceException) {
            assertEquals("insufficient_storage", e.code)
        }
        assertEquals(0, source.opens)
    }

    @Test
    fun `declared size mismatch is detected and staging is removed`() {
        val source = ByteSource(
            descriptor(size = 50L, reopenable = false, persistable = false),
            ByteArray(10)
        )
        try {
            preparer().prepare(source)
            throw AssertionError("expected size change rejection")
        } catch (e: TransferSourceException) {
            assertEquals("source_changed", e.code)
        }
        assertEquals(0, cacheDir.listFiles()?.size ?: 0)
    }

    @Test
    fun `empty file hashes to the canonical empty digest and no chunks`() {
        val source = ByteSource(descriptor(size = 0L), ByteArray(0))
        val prepared = preparer().prepare(source)
        assertEquals(
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            prepared.sha256Hex
        )
        assertEquals(0L, prepared.totalChunks())
    }

    @Test
    fun `chunk reads are exact and final chunk length is preserved`() {
        val size = TransferValidation.CHUNK_BYTES + 17L
        val bytes = ByteArray(size.toInt()) { (it % 251).toByte() }
        val source = ByteSource(descriptor(size = size), bytes)
        val prepared = preparer().prepare(source)

        prepared.openChunkStream(0L).use { stream ->
            val first = ChunkReader.readExactly(stream, TransferValidation.CHUNK_BYTES.toInt())
            assertEquals(TransferValidation.CHUNK_BYTES.toInt(), first.size)
        }
        prepared.openChunkStream(1L).use { stream ->
            val last = ChunkReader.readExactly(stream, 17)
            assertEquals(17, last.size)
            assertArrayEquals(bytes.copyOfRange(TransferValidation.CHUNK_BYTES.toInt(), size.toInt()), last)
        }
        assertEquals(2L, prepared.totalChunks())
    }

    @Test
    fun `chunk reader rejects a truncated stream`() {
        try {
            ChunkReader.readExactly(ByteArrayInputStream(ByteArray(5)), 10)
            throw AssertionError("expected source change")
        } catch (e: TransferSourceException) {
            assertEquals("source_changed", e.code)
        }
    }

    @Test
    fun `restore uses durable spool and rejects missing staging`() {
        val bytes = ByteArray(12_345) { (it % 97).toByte() }
        val source = ByteSource(
            descriptor(size = bytes.size.toLong(), persistable = false, reopenable = false),
            bytes
        )
        val prepared = preparer().prepare(source)

        val restored = preparer().restore(
            source = source,
            filename = prepared.filename,
            mimeType = prepared.mimeType,
            size = prepared.size,
            sha256Hex = prepared.sha256Hex,
            strategy = prepared.strategy,
            spoolName = prepared.spoolName
        )
        assertArrayEquals(bytes, restored.openStreamForTest().readBytes())

        File(cacheDir, prepared.spoolName!!).delete()
        try {
            preparer().restore(
                source = source,
                filename = prepared.filename,
                mimeType = prepared.mimeType,
                size = prepared.size,
                sha256Hex = prepared.sha256Hex,
                strategy = prepared.strategy,
                spoolName = prepared.spoolName
            )
            throw AssertionError("expected missing staging")
        } catch (e: TransferSourceException) {
            assertEquals("staging_missing", e.code)
        }
    }

    @Test
    fun `cancellation during spool removes the private staging copy`() {
        val bytes = ByteArray(256 * 1024) { (it % 251).toByte() }
        val source = ByteSource(
            descriptor(size = bytes.size.toLong(), persistable = false, reopenable = false),
            bytes
        )
        var checks = 0
        try {
            preparer().prepare(source) {
                checks += 1
                checks > 2
            }
            throw AssertionError("expected cancellation")
        } catch (e: TransferCancelledException) {
            assertTrue(checks >= 3)
        }
        assertEquals(0, cacheDir.listFiles()?.size ?: 0)
        assertEquals(1, source.opens)
    }

    @Test
    fun `cancellation during hashing never opens the source twice`() {
        val bytes = ByteArray(256 * 1024) { (it % 251).toByte() }
        val source = ByteSource(descriptor(size = bytes.size.toLong()), bytes)
        var checks = 0
        try {
            preparer().prepare(source) {
                checks += 1
                checks > 2
            }
            throw AssertionError("expected cancellation")
        } catch (e: TransferCancelledException) {
            assertTrue(checks >= 3)
        }
        assertEquals(1, source.opens)
        assertEquals(0, cacheDir.listFiles()?.size ?: 0)
    }

    private fun PreparedUpload.openStreamForTest(): InputStream =
        openChunkStream(0L)
}
