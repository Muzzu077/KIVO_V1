package com.kivo.companion.transfer

import java.io.File
import java.io.FileInputStream
import java.io.InputStream
import java.security.MessageDigest
import java.util.Locale

/**
 * A user-selected publication target. Implementations must use the storage
 * access framework; the app never derives a filesystem path from this value.
 */
interface DownloadDestination {
    val uri: String
    fun displayName(): String?
    fun openSink(): DownloadSink
    fun reopenInput(): InputStream?
    fun delete(): Boolean
}

interface DownloadSink {
    fun write(buffer: ByteArray, offset: Int, length: Int)
    fun flushAndSync()
    fun close()
}

fun interface DownloadDestinationResolver {
    fun resolve(uri: String): DownloadDestination
}

class DownloadPublicationException(
    val code: String,
    message: String,
    cause: Throwable? = null
) : Exception(message, cause)

data class PublicationResult(
    val bytesWritten: Long,
    val sha256Hex: String,
    val destinationVerified: Boolean,
    val displayName: String?
)

/**
 * Copies a fully verified app-private staging file into a SAF destination.
 *
 * The copy uses a bounded 64 KiB buffer, hashes while writing, flushes and
 * fsyncs where the provider exposes a file descriptor, then reopens the
 * destination and re-hashes it when the provider supports reading. A failed
 * write or verification deletes the destination document instead of leaving a
 * partially written file behind.
 */
class DownloadPublisher(
    private val bufferBytes: Int = DownloadStager.BUFFER_BYTES
) {
    init {
        require(bufferBytes in 4096..(1024 * 1024)) { "copy buffer must be bounded" }
    }

    fun publish(
        stage: File,
        expectedSize: Long,
        expectedSha256Hex: String,
        destination: DownloadDestination,
        isCancelled: () -> Boolean = { false }
    ): PublicationResult {
        if (stage.length() != expectedSize) {
            throw DownloadPublicationException(
                "staging_size_mismatch",
                "The private staging file no longer matches the verified download"
            )
        }
        val expected = expectedSha256Hex.lowercase(Locale.ROOT)
        val digest = MessageDigest.getInstance("SHA-256")
        var total = 0L
        val sink = destination.openSink()
        try {
            FileInputStream(stage).use { input ->
                val buffer = ByteArray(bufferBytes)
                while (true) {
                    if (isCancelled()) throw TransferCancelledException()
                    val read = input.read(buffer)
                    if (read < 0) break
                    if (read == 0) continue
                    if (total + read > expectedSize) {
                        throw DownloadPublicationException(
                            "publication_geometry",
                            "The staged file grew beyond its verified size"
                        )
                    }
                    digest.update(buffer, 0, read)
                    sink.write(buffer, 0, read)
                    total += read
                }
            }
            if (isCancelled()) throw TransferCancelledException()
            sink.flushAndSync()
            if (total != expectedSize) {
                throw DownloadPublicationException(
                    "publication_incomplete",
                    "The destination received fewer bytes than the verified download"
                )
            }
            val written = TransferCrypto.toHex(digest.digest())
            if (!MessageDigest.isEqual(
                    written.toByteArray(Charsets.US_ASCII),
                    expected.toByteArray(Charsets.US_ASCII)
                )
            ) {
                throw DownloadPublicationException(
                    "publication_digest_mismatch",
                    "The bytes written to the destination did not match the verified digest"
                )
            }
        } catch (e: TransferCancelledException) {
            closeQuietly(sink)
            destination.delete()
            throw e
        } catch (e: Exception) {
            closeQuietly(sink)
            destination.delete()
            if (e is DownloadPublicationException) throw e
            throw DownloadPublicationException(
                "publication_failed",
                e.message ?: "Could not write the selected destination",
                e
            )
        }
        closeQuietly(sink)

        val verified = verifyDestination(
            destination = destination,
            expectedSize = expectedSize,
            expectedSha256Hex = expected,
            isCancelled = isCancelled
        )
        if (verified == false) {
            destination.delete()
            throw DownloadPublicationException(
                "publication_verification_failed",
                "The destination did not match the verified download and was removed"
            )
        }
        return PublicationResult(
            bytesWritten = total,
            sha256Hex = expected,
            destinationVerified = verified == true,
            displayName = destination.displayName()
        )
    }

    /** Returns true/false when the provider allows reading, or null when it does not. */
    private fun verifyDestination(
        destination: DownloadDestination,
        expectedSize: Long,
        expectedSha256Hex: String,
        isCancelled: () -> Boolean
    ): Boolean? {
        val input = try {
            destination.reopenInput()
        } catch (e: Exception) {
            null
        } ?: return null
        return try {
            val digest = MessageDigest.getInstance("SHA-256")
            val buffer = ByteArray(bufferBytes)
            var total = 0L
            input.use { stream ->
                while (true) {
                    if (isCancelled()) throw TransferCancelledException()
                    val read = stream.read(buffer)
                    if (read < 0) break
                    if (read == 0) continue
                    total += read
                    if (total > expectedSize) return false
                    digest.update(buffer, 0, read)
                }
            }
            val actual = TransferCrypto.toHex(digest.digest())
            total == expectedSize && MessageDigest.isEqual(
                actual.toByteArray(Charsets.US_ASCII),
                expectedSha256Hex.toByteArray(Charsets.US_ASCII)
            )
        } catch (e: TransferCancelledException) {
            throw e
        } catch (e: Exception) {
            // A provider may revoke read access after creation. The write was
            // flushed and closed, so report an unverified but completed
            // publication rather than fabricating a verification result.
            null
        }
    }

    private fun closeQuietly(sink: DownloadSink) {
        try {
            sink.close()
        } catch (e: Exception) {
            // The write error or cancellation is authoritative.
        }
    }
}
