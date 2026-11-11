package com.kivo.companion.transfer

import org.junit.After
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

/**
 * Publisher tests use an in-memory SAF-like destination. The publisher must
 * hash while copying, flush, optionally reopen and verify, and delete a
 * partially written destination on every failure path.
 */
class DownloadPublisherTest {

    private lateinit var root: File
    private lateinit var stage: File
    private val bytes = ByteArray(70_000) { ((it * 7) % 251).toByte() }

    @Before
    fun setUp() {
        root = Files.createTempDirectory("download_publisher_").toFile()
        stage = File(root, "kivo_download_${"a".repeat(32)}.part")
        stage.writeBytes(bytes)
    }

    @After
    fun tearDown() {
        root.deleteRecursively()
    }

    @Test
    fun `verified publication writes flushed bytes and re-hashes the destination`() {
        val destination = FakeDestination(readable = true)
        val result = DownloadPublisher().publish(
            stage = stage,
            expectedSize = bytes.size.toLong(),
            expectedSha256Hex = TransferCrypto.sha256Hex(bytes),
            destination = destination
        )

        assertEquals(bytes.size.toLong(), result.bytesWritten)
        assertEquals(TransferCrypto.sha256Hex(bytes), result.sha256Hex)
        assertEquals(true, result.destinationVerified)
        assertEquals("saved.bin", result.displayName)
        assertTrue(bytes.contentEquals(destination.sink.written.toByteArray()))
        assertTrue(destination.sink.flushed)
        assertTrue(destination.sink.closed)
        assertFalse(destination.deleted)
    }

    @Test
    fun `provider without verification read reports truthfully unverified`() {
        val destination = FakeDestination(readable = false)
        val result = DownloadPublisher().publish(
            stage = stage,
            expectedSize = bytes.size.toLong(),
            expectedSha256Hex = TransferCrypto.sha256Hex(bytes),
            destination = destination
        )
        assertEquals(false, result.destinationVerified)
        assertFalse(destination.deleted)
        assertTrue(bytes.contentEquals(destination.sink.written.toByteArray()))
    }

    @Test
    fun `destination that reopens with different bytes is deleted`() {
        val destination = FakeDestination(readable = true).apply {
            reopenOverride = { ByteArrayInputStream(ByteArray(bytes.size) { 9 }) }
        }
        try {
            DownloadPublisher().publish(
                stage = stage,
                expectedSize = bytes.size.toLong(),
                expectedSha256Hex = TransferCrypto.sha256Hex(bytes),
                destination = destination
            )
            throw AssertionError("accepted a mismatched destination")
        } catch (e: DownloadPublicationException) {
            assertEquals("publication_verification_failed", e.code)
        }
        assertTrue(destination.deleted)
    }

    @Test
    fun `changed staging bytes are rejected before success is reported`() {
        val destination = FakeDestination(readable = true)
        try {
            DownloadPublisher().publish(
                stage = stage,
                expectedSize = bytes.size.toLong(),
                expectedSha256Hex = "0".repeat(64),
                destination = destination
            )
            throw AssertionError("accepted a mismatched staged digest")
        } catch (e: DownloadPublicationException) {
            assertEquals("publication_digest_mismatch", e.code)
        }
        assertTrue(destination.deleted)
    }

    @Test
    fun `staging length mismatch is rejected before touching the destination`() {
        val destination = FakeDestination(readable = true)
        try {
            DownloadPublisher().publish(
                stage = stage,
                expectedSize = bytes.size.toLong() + 1L,
                expectedSha256Hex = TransferCrypto.sha256Hex(bytes),
                destination = destination
            )
            throw AssertionError("accepted a truncated stage")
        } catch (e: DownloadPublicationException) {
            assertEquals("staging_size_mismatch", e.code)
        }
        assertFalse(destination.sink.opened)
        assertFalse(destination.deleted)
    }

    @Test
    fun `write failure deletes the destination and never reports completion`() {
        val destination = FakeDestination(readable = true).apply {
            sink.failAfterBytes = 1024L
        }
        try {
            DownloadPublisher().publish(
                stage = stage,
                expectedSize = bytes.size.toLong(),
                expectedSha256Hex = TransferCrypto.sha256Hex(bytes),
                destination = destination
            )
            throw AssertionError("accepted a failed write")
        } catch (e: DownloadPublicationException) {
            assertEquals("publication_failed", e.code)
        }
        assertTrue(destination.deleted)
        assertTrue(destination.sink.closed)
    }

    @Test
    fun `cancellation deletes the destination and preserves staging`() {
        val destination = FakeDestination(readable = true)
        var checks = 0
        try {
            DownloadPublisher().publish(
                stage = stage,
                expectedSize = bytes.size.toLong(),
                expectedSha256Hex = TransferCrypto.sha256Hex(bytes),
                destination = destination,
                isCancelled = {
                    checks += 1
                    checks > 1
                }
            )
            throw AssertionError("publication ignored cancellation")
        } catch (e: TransferCancelledException) {
            assertTrue(e.message!!.contains("cancelled", ignoreCase = true))
        }
        assertTrue(destination.deleted)
        assertTrue(stage.exists())
    }

    @Test
    fun `open sink failure propagates without deleting an unmodified destination`() {
        val destination = FakeDestination(readable = true).apply {
            openFailure = IOException("provider refused write")
        }
        try {
            DownloadPublisher().publish(
                stage = stage,
                expectedSize = bytes.size.toLong(),
                expectedSha256Hex = TransferCrypto.sha256Hex(bytes),
                destination = destination
            )
            throw AssertionError("accepted an unavailable sink")
        } catch (e: IOException) {
            assertEquals("provider refused write", e.message)
        }
        assertFalse(destination.deleted)
    }

    @Test
    fun `copy buffer must be bounded`() {
        for (size in listOf(0, 1024, 2 * 1024 * 1024)) {
            try {
                DownloadPublisher(bufferBytes = size)
                throw AssertionError("accepted buffer size $size")
            } catch (e: IllegalArgumentException) {
                // expected
            }
        }
    }

    @Test
    fun `empty file publishes as an empty verified destination`() {
        stage.writeBytes(ByteArray(0))
        val destination = FakeDestination(readable = true)
        val result = DownloadPublisher().publish(
            stage = stage,
            expectedSize = 0L,
            expectedSha256Hex = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            destination = destination
        )
        assertEquals(0L, result.bytesWritten)
        assertEquals(true, result.destinationVerified)
        assertEquals(0, destination.sink.written.size())
        assertNull(destination.sink.error)
    }

    private class FakeDestination(
        private val readable: Boolean
    ) : DownloadDestination {
        override val uri: String = "content://test/document/1"
        val sink = FakeSink()
        var deleted = false
        var openFailure: IOException? = null
        var reopenOverride: (() -> InputStream)? = null

        override fun displayName(): String = "saved.bin"

        override fun openSink(): DownloadSink {
            openFailure?.let { throw it }
            sink.opened = true
            return sink
        }

        override fun reopenInput(): InputStream? {
            if (!readable) return null
            return reopenOverride?.invoke() ?: ByteArrayInputStream(sink.written.toByteArray())
        }

        override fun delete(): Boolean {
            deleted = true
            return true
        }
    }

    private class FakeSink : DownloadSink {
        val written = ByteArrayOutputStream()
        var opened = false
        var flushed = false
        var closed = false
        var failAfterBytes: Long = -1L
        var error: String? = null

        override fun write(buffer: ByteArray, offset: Int, length: Int) {
            if (failAfterBytes >= 0 && written.size().toLong() + length > failAfterBytes) {
                error = "provider write failed"
                throw IOException(error)
            }
            written.write(buffer, offset, length)
        }

        override fun flushAndSync() {
            flushed = true
        }

        override fun close() {
            closed = true
        }
    }
}
