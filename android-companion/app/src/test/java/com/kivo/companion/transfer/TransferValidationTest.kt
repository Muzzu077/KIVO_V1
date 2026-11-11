package com.kivo.companion.transfer

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class TransferValidationTest {

    private val fixture = loadFixture()
    private val validation = fixture.getJSONObject("validation")

    @Test
    fun `valid filenames from shared fixture are accepted`() {
        val values = validation.getJSONArray("valid_filenames")
        for (index in 0 until values.length()) {
            val raw = values.getString(index)
            assertEquals(raw, TransferValidation.validateDisplayFilename(raw))
        }
    }

    @Test
    fun `invalid filenames from shared fixture are rejected`() {
        val values = validation.getJSONArray("invalid_filenames")
        for (index in 0 until values.length()) {
            try {
                TransferValidation.validateDisplayFilename(values.getString(index))
                throw AssertionError("accepted invalid filename at $index")
            } catch (expected: TransferValidationException) {
                assertEquals("invalid_filename", expected.code)
            }
        }
    }

    @Test
    fun `unicode normalization is applied after validation`() {
        val decomposed = "re\u0301sume\u0301.docx"
        assertEquals("résumé.docx", TransferValidation.validateDisplayFilename(decomposed))
    }

    @Test
    fun `size limit matches desktop`() {
        assertEquals(validation.getLong("max_file_size"), TransferValidation.MAX_UPLOAD_BYTES)
        assertEquals(2_147_483_648L, TransferValidation.validateDeclaredSize(2_147_483_648L))
        assertFails("size_exceeds_limit") {
            TransferValidation.validateDeclaredSize(2_147_483_649L)
        }
        assertFails("invalid_size") { TransferValidation.validateDeclaredSize(-1L) }
    }

    @Test
    fun `malformed MIME hints are dropped rather than transmitted`() {
        assertNull(TransferValidation.sanitizeMimeHint(null))
        assertNull(TransferValidation.sanitizeMimeHint(""))
        assertNull(TransferValidation.sanitizeMimeHint("not a mime"))
        assertNull(TransferValidation.sanitizeMimeHint("text/plain\r\nX-Evil: 1"))
        assertNull(TransferValidation.sanitizeMimeHint("a/" + "b".repeat(200)))
        assertEquals(
            "application/pdf",
            TransferValidation.sanitizeMimeHint("application/pdf")
        )
        assertEquals(
            "text/plain; charset=utf-8",
            TransferValidation.sanitizeMimeHint("text/plain; charset=utf-8")
        )
    }

    @Test
    fun `chunk geometry handles empty, exact, and final partial chunks`() {
        assertEquals(0L, TransferValidation.totalChunks(0L))
        assertEquals(1L, TransferValidation.totalChunks(1L))
        assertEquals(1L, TransferValidation.totalChunks(TransferValidation.CHUNK_BYTES))
        assertEquals(2L, TransferValidation.totalChunks(TransferValidation.CHUNK_BYTES + 1))
        assertEquals(1, TransferValidation.expectedChunkLength(1L, 0L))
        assertEquals(
            TransferValidation.CHUNK_BYTES.toInt(),
            TransferValidation.expectedChunkLength(TransferValidation.CHUNK_BYTES, 0L)
        )
        assertEquals(
            1,
            TransferValidation.expectedChunkLength(TransferValidation.CHUNK_BYTES + 1, 1L)
        )
        assertFails("chunk_length_mismatch") {
            TransferValidation.validateChunkLength(2L * TransferValidation.CHUNK_BYTES, 0L, 1)
        }
    }

    @Test
    fun `filename length is counted in code points not UTF-16 units`() {
        val emoji = "\uD83D\uDE00" // one code point, two UTF-16 units
        val name = emoji.repeat(128) + ".txt"
        assertFails("invalid_filename") {
            TransferValidation.validateDisplayFilename(name)
        }
        val allowed = emoji.repeat(60) + ".txt"
        assertTrue(TransferValidation.validateDisplayFilename(allowed).isNotEmpty())
    }

    private fun assertFails(code: String, block: () -> Unit) {
        try {
            block()
            throw AssertionError("expected $code")
        } catch (e: TransferValidationException) {
            assertEquals(code, e.code)
        }
    }

    private fun loadFixture(): JSONObject {
        val stream = javaClass.classLoader!!.getResourceAsStream("transfer_test_vectors.json")
            ?: error("fixture missing")
        return stream.use { JSONObject(it.readBytes().toString(Charsets.UTF_8)) }
    }
}
