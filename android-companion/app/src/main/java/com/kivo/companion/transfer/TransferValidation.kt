package com.kivo.companion.transfer

import java.text.Normalizer
import java.util.Locale

class TransferValidationException(
    val code: String,
    message: String
) : IllegalArgumentException(message)

/**
 * Client-side mirror of `kivo/bridge/transfer_validation.py`.
 *
 * Filenames are display metadata only. They are validated locally so an
 * unsupported source is rejected before any network request, and they are
 * revalidated by the desktop before publication.
 */
object TransferValidation {
    const val MIB = 1024L * 1024L
    const val GIB = 1024L * MIB
    const val CHUNK_BYTES = 1L * MIB
    const val MAX_UPLOAD_BYTES = 2L * GIB
    const val MAX_FILENAME_CHARS = 128
    const val MAX_FILENAME_BYTES = 255
    const val MAX_MIME_CHARS = 128

    private val MIME_PATTERN = Regex(
        "^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(?:\\s*;\\s*[A-Za-z0-9=._-]+)*$"
    )

    private val BIDI_CONTROLS = buildSet {
        add(0x061C)
        add(0x200E)
        add(0x200F)
        for (value in 0x202A..0x202E) add(value)
        for (value in 0x2066..0x2069) add(value)
    }

    fun validateDisplayFilename(value: String?): String {
        if (value.isNullOrEmpty()) {
            throw TransferValidationException("invalid_filename", "filename must not be empty")
        }
        if (value.codePointCount(0, value.length) > MAX_FILENAME_CHARS) {
            throw TransferValidationException("invalid_filename", "filename exceeds 128 characters")
        }
        if (stripUnicodeWhitespace(value) != value) {
            throw TransferValidationException(
                "invalid_filename",
                "filename must not start or end with whitespace"
            )
        }
        if (value == "." || value == "..") {
            throw TransferValidationException("invalid_filename", "filename is reserved")
        }

        var offset = 0
        while (offset < value.length) {
            val codePoint = value.codePointAt(offset)
            if (codePoint == 0x2F || codePoint == 0x5C || codePoint == 0x00) {
                throw TransferValidationException(
                    "invalid_filename",
                    "filename must not contain path separators"
                )
            }
            if (codePoint in BIDI_CONTROLS) {
                throw TransferValidationException(
                    "invalid_filename",
                    "filename must not contain bidi controls"
                )
            }
            when (Character.getType(codePoint)) {
                Character.CONTROL.toInt(),
                Character.SURROGATE.toInt(),
                Character.LINE_SEPARATOR.toInt(),
                Character.PARAGRAPH_SEPARATOR.toInt() -> throw TransferValidationException(
                    "invalid_filename",
                    "filename must not contain control characters"
                )
            }
            offset += Character.charCount(codePoint)
        }

        val normalized = Normalizer.normalize(value, Normalizer.Form.NFC)
        if (normalized.codePointCount(0, normalized.length) > MAX_FILENAME_CHARS) {
            throw TransferValidationException(
                "invalid_filename",
                "filename exceeds 128 characters after normalization"
            )
        }
        if (normalized.toByteArray(Charsets.UTF_8).size > MAX_FILENAME_BYTES) {
            throw TransferValidationException(
                "invalid_filename",
                "filename exceeds the filesystem byte limit"
            )
        }
        if (normalized == "." || normalized == ".." || stripUnicodeWhitespace(normalized).isEmpty()) {
            throw TransferValidationException("invalid_filename", "filename is reserved")
        }
        return normalized
    }

    fun validateDeclaredSize(value: Long): Long {
        if (value < 0L) {
            throw TransferValidationException("invalid_size", "size must be non-negative")
        }
        if (value > MAX_UPLOAD_BYTES) {
            throw TransferValidationException("size_exceeds_limit", "file exceeds the 2 GiB upload limit")
        }
        return value
    }

    /**
     * Returns a safe display-only MIME hint, or null when the provider supplied
     * something malformed. Malformed hints are dropped rather than sent.
     */
    fun sanitizeMimeHint(value: String?): String? {
        if (value.isNullOrEmpty()) return null
        if (value.length > MAX_MIME_CHARS) return null
        if (value.any { it.code < 0x20 || it.code == 0x7F }) return null
        return if (MIME_PATTERN.matches(value)) value else null
    }

    fun validateSha256Hex(value: String): String {
        if (value.length != 64 || value.any { it !in "0123456789abcdefABCDEF" }) {
            throw TransferValidationException(
                "invalid_sha256",
                "sha256 must be exactly 64 hexadecimal characters"
            )
        }
        return value.lowercase(Locale.ROOT)
    }

    fun totalChunks(size: Long): Long {
        if (size == 0L) return 0L
        return (size + CHUNK_BYTES - 1L) / CHUNK_BYTES
    }

    fun expectedChunkLength(size: Long, index: Long): Int {
        require(index >= 0L) { "chunk index must be non-negative" }
        val total = totalChunks(size)
        require(index < total) { "chunk index outside the declared file" }
        val offset = index * CHUNK_BYTES
        val remaining = size - offset
        return minOf(CHUNK_BYTES, remaining).toInt()
    }

    fun validateChunkLength(size: Long, index: Long, received: Int) {
        val expected = expectedChunkLength(size, index)
        if (received != expected) {
            val code = if (index == totalChunks(size) - 1) {
                "final_chunk_length_mismatch"
            } else {
                "chunk_length_mismatch"
            }
            throw TransferValidationException(
                code,
                "chunk length $received does not match expected $expected"
            )
        }
    }

    private fun stripUnicodeWhitespace(value: String): String {
        var start = 0
        var end = value.length
        while (start < end) {
            val codePoint = value.codePointAt(start)
            if (!isUnicodeWhitespace(codePoint)) break
            start += Character.charCount(codePoint)
        }
        while (end > start) {
            val codePoint = value.codePointBefore(end)
            if (!isUnicodeWhitespace(codePoint)) break
            end -= Character.charCount(codePoint)
        }
        return value.substring(start, end)
    }

    private fun isUnicodeWhitespace(codePoint: Int): Boolean =
        Character.isWhitespace(codePoint) || Character.isSpaceChar(codePoint)
}
