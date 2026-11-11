package com.kivo.companion.transfer

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class TransferUiModelsTest {

    private fun descriptor(
        name: String? = "report.pdf",
        size: Long? = 1024L,
        mime: String? = "application/pdf",
        directory: Boolean = false,
        persistable: Boolean = true,
        reopenable: Boolean = true,
        uri: String = "content://test/document/1"
    ) = DocumentDescriptor(
        uri = uri,
        displayName = name,
        sizeBytes = size,
        mimeType = mime,
        isDirectory = directory,
        persistable = persistable,
        reopenable = reopenable
    )

    @Test
    fun `supported document keeps display metadata and strategy hint`() {
        val summary = DocumentSummaryFactory.from(descriptor())

        assertTrue(summary.supported)
        assertEquals("report.pdf", summary.displayName)
        assertEquals(1024L, summary.sizeBytes)
        assertEquals("application/pdf", summary.mimeType)
        assertTrue(summary.accessHint.contains("without a private copy"))
    }

    @Test
    fun `non persistable document explains private staging`() {
        val summary = DocumentSummaryFactory.from(
            descriptor(persistable = false, reopenable = false)
        )
        assertTrue(summary.supported)
        assertFalse(summary.persistable)
        assertTrue(summary.accessHint.contains("private staging copy"))
    }

    @Test
    fun `directory is rejected before selection is confirmed`() {
        val summary = DocumentSummaryFactory.from(descriptor(directory = true))
        assertFalse(summary.supported)
        assertEquals("source_is_directory", summary.errorCode)
    }

    @Test
    fun `path separator in display name is rejected`() {
        val summary = DocumentSummaryFactory.from(descriptor(name = "../escape.pdf"))
        assertFalse(summary.supported)
        assertEquals("invalid_filename", summary.errorCode)
    }

    @Test
    fun `oversized document is rejected from provider metadata`() {
        val summary = DocumentSummaryFactory.from(
            descriptor(size = TransferValidation.MAX_UPLOAD_BYTES + 1L)
        )
        assertFalse(summary.supported)
        assertEquals("size_exceeds_limit", summary.errorCode)
    }

    @Test
    fun `unknown provider size remains selectable and spools later`() {
        val summary = DocumentSummaryFactory.from(descriptor(size = null))
        assertTrue(summary.supported)
        assertNull(summary.sizeBytes)
    }

    @Test
    fun `malformed mime hint is dropped instead of displayed`() {
        val summary = DocumentSummaryFactory.from(descriptor(mime = "text/plain\r\nX-Evil: 1"))
        assertTrue(summary.supported)
        assertNull(summary.mimeType)
    }

    @Test
    fun `non content uri is rejected`() {
        val summary = DocumentSummaryFactory.from(descriptor(uri = "file:///etc/passwd"))
        assertFalse(summary.supported)
        assertEquals("unsupported_source_scheme", summary.errorCode)
    }

    @Test
    fun `byte formatting is stable and bounded`() {
        assertEquals("0 B", formatByteCount(0))
        assertEquals("1023 B", formatByteCount(1023))
        assertEquals("1.0 KiB", formatByteCount(1024))
        assertEquals("1.5 MiB", formatByteCount(1024L * 1024L + 512L * 1024L))
        assertEquals("2.00 GiB", formatByteCount(2L * 1024L * 1024L * 1024L))
    }

    @Test
    fun `receiving notification exposes verified progress and cancel while active`() {
        val content = TransferNotificationFactory.from(
            TransferSnapshot(
                phase = TransferPhase.RECEIVING,
                filename = "report.pdf",
                size = 2L * 1024L * 1024L,
                receivedBytes = 1024L * 1024L,
                message = ""
            ),
            active = true
        )
        assertEquals("Uploading to KIVO", content.title)
        assertTrue(content.showProgress)
        assertFalse(content.indeterminate)
        assertEquals(50, content.progressPercent)
        assertTrue(content.showCancel)
        assertTrue(content.text.contains("50%"))
        assertTrue(content.text.contains("1.0 MiB of 2.0 MiB"))
    }

    @Test
    fun `approval notification states that no data was sent`() {
        val content = TransferNotificationFactory.from(
            TransferSnapshot(
                phase = TransferPhase.WAITING_DESKTOP_APPROVAL,
                filename = "report.pdf",
                size = 100L
            ),
            active = true
        )
        assertEquals("Waiting for desktop approval", content.title)
        assertTrue(content.text.contains("approve the upload"))
        assertTrue(content.showCancel)
    }

    @Test
    fun `committed notification reports Downloads publication and no cancel`() {
        val content = TransferNotificationFactory.from(
            TransferSnapshot(
                phase = TransferPhase.COMMITTED,
                filename = "report.pdf",
                size = 100L,
                receivedBytes = 100L,
                publishedFilename = "report.pdf"
            ),
            active = false
        )
        assertEquals("Upload complete", content.title)
        assertTrue(content.text.contains("Saved to Downloads as report.pdf"))
        assertEquals(100, content.progressPercent)
        assertFalse(content.showCancel)
    }

    @Test
    fun `interrupted notification never offers silent success`() {
        val content = TransferNotificationFactory.from(
            TransferSnapshot(
                phase = TransferPhase.INTERRUPTED,
                filename = "report.pdf",
                size = 200L,
                receivedBytes = 100L,
                message = "Lost contact with the desktop"
            ),
            active = false
        )
        assertEquals("Upload interrupted", content.title)
        assertTrue(content.text.contains("open KIVO Companion to resume"))
        assertFalse(content.showCancel)
    }

    @Test
    fun `init unknown notification asks the user to reopen the app`() {
        val content = TransferNotificationFactory.from(
            TransferSnapshot(
                phase = TransferPhase.INIT_OUTCOME_UNKNOWN,
                filename = "report.pdf",
                size = 200L
            ),
            active = false
        )
        assertEquals("Upload status unknown", content.title)
        assertTrue(content.text.contains("open KIVO Companion"))
    }

    @Test
    fun `notification progress is clamped`() {
        val content = TransferNotificationFactory.from(
            TransferSnapshot(
                phase = TransferPhase.RECEIVING,
                filename = "report.pdf",
                size = 10L,
                receivedBytes = 999L
            ),
            active = true
        )
        assertEquals(100, content.progressPercent)
    }
}
