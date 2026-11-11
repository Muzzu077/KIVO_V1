package com.kivo.companion.transfer

import android.annotation.SuppressLint
import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.util.UUID

/**
 * Provider-neutral description of a user-selected document. The URI is only
 * used to reopen the provider stream; no filesystem path is transmitted.
 */
data class DocumentDescriptor(
    val uri: String,
    val displayName: String?,
    val sizeBytes: Long?,
    val mimeType: String?,
    val isDirectory: Boolean,
    val persistable: Boolean,
    val reopenable: Boolean
)

interface UploadSource {
    val descriptor: DocumentDescriptor
    fun openStream(): InputStream
}

/** Resolves a persisted URI back to a provider stream without filesystem paths. */
interface UploadSourceResolver {
    fun resolve(uri: String): UploadSource
}

enum class SourceStrategy { REOPEN_TWO_PASS, PRIVATE_SPOOL }

/**
 * A validated upload candidate. `sha256Hex` is computed before the init request.
 * No chunk is ever read into a buffer proportional to the file size.
 */
class PreparedUpload(
    val uri: String,
    val filename: String,
    val mimeType: String?,
    val size: Long,
    val sha256Hex: String,
    val strategy: SourceStrategy,
    private val source: UploadSource,
    private val spoolFile: File?
) {
    val spoolName: String? get() = spoolFile?.name

    fun totalChunks(): Long = TransferValidation.totalChunks(size)

    /**
     * Opens the source at a chunk boundary. Reopenable providers are reopened
     * and advanced by bounded reads; no seek capability is assumed.
     */
    fun openChunkStream(startChunkIndex: Long): InputStream {
        require(startChunkIndex >= 0L)
        val stream = spoolFile?.inputStream() ?: source.openStream()
        val skipBytes = startChunkIndex * TransferValidation.CHUNK_BYTES
        if (skipBytes == 0L) return stream
        try {
            skipExactly(stream, skipBytes)
        } catch (e: Exception) {
            try {
                stream.close()
            } catch (ignored: Exception) {
                // Preserve the original failure.
            }
            throw e
        }
        return stream
    }

    fun cleanup() {
        if (spoolFile != null && spoolFile.exists()) {
            spoolFile.delete()
        }
    }

    private fun skipExactly(stream: InputStream, count: Long) {
        val buffer = ByteArray(SKIP_BUFFER_BYTES)
        var remaining = count
        while (remaining > 0L) {
            val wanted = minOf(remaining, buffer.size.toLong()).toInt()
            val read = stream.read(buffer, 0, wanted)
            if (read < 0) {
                throw TransferSourceException(
                    "source_changed",
                    "The selected document ended before the committed offset"
                )
            }
            remaining -= read
        }
    }

    companion object {
        private const val SKIP_BUFFER_BYTES = 64 * 1024
    }
}

/**
 * Validates each source before any init request and chooses the cheapest safe
 * source strategy:
 *
 * - persistable/reopenable sources are hashed in a first pass and streamed in a
 *   second pass, avoiding an app-private copy;
 * - unknown-size, non-persistable, or non-reopenable sources are spooled once to
 *   app-private cache while hashing, with bounded buffers and storage checks.
 */
@SuppressLint("UsableSpace")
class UploadPreparer(
    private val cacheDir: File,
    private val freeSpaceBytes: () -> Long = { cacheDir.usableSpace },
    private val minFreeBytes: Long = DEFAULT_MIN_FREE_BYTES
) {
    companion object {
        const val BUFFER_BYTES = 64 * 1024
        const val DEFAULT_MIN_FREE_BYTES = 64L * 1024L * 1024L
        private val SPOOL_NAME = Regex("^kivo_spool_[a-f0-9]{32}\\.part$")
    }

    fun prepare(source: UploadSource, isCancelled: () -> Boolean = { false }): PreparedUpload {
        val descriptor = source.descriptor
        if (isCancelled()) throw TransferCancelledException()
        if (descriptor.isDirectory) {
            throw TransferSourceException("source_is_directory", "Directories cannot be uploaded")
        }
        val filename = TransferValidation.validateDisplayFilename(descriptor.displayName)
        val mime = TransferValidation.sanitizeMimeHint(descriptor.mimeType)
        val declaredSize = descriptor.sizeBytes
        if (declaredSize != null) {
            TransferValidation.validateDeclaredSize(declaredSize)
        }

        if (descriptor.reopenable && declaredSize != null) {
            val (digest, actualSize) = hashSource(source, declaredSize, isCancelled)
            if (actualSize != declaredSize) {
                throw TransferSourceException(
                    "source_changed",
                    "The document size changed while it was being hashed"
                )
            }
            return PreparedUpload(
                uri = descriptor.uri,
                filename = filename,
                mimeType = mime,
                size = actualSize,
                sha256Hex = digest,
                strategy = SourceStrategy.REOPEN_TWO_PASS,
                source = source,
                spoolFile = null
            )
        }

        val spool = spoolSource(source, declaredSize, isCancelled)
        return PreparedUpload(
            uri = descriptor.uri,
            filename = filename,
            mimeType = mime,
            size = spool.size,
            sha256Hex = spool.sha256Hex,
            strategy = SourceStrategy.PRIVATE_SPOOL,
            source = source,
            spoolFile = spool.file
        )
    }

    fun spoolFileByName(name: String): File? {
        if (!SPOOL_NAME.matches(name)) return null
        val file = File(cacheDir, name)
        return if (file.isFile) file else null
    }

    /**
     * Deletes app-private staging copies that no durable transfer references.
     * Called at startup so a process death cannot leak unbounded cache files.
     */
    @Synchronized
    fun cleanupOrphanSpools(keepName: String?) {
        val files = cacheDir.listFiles() ?: return
        for (file in files) {
            if (SPOOL_NAME.matches(file.name) && file.name != keepName) {
                file.delete()
            }
        }
    }

    /**
     * Reconstructs a prepared upload for an interrupted transfer. The original
     * provider may still be reopenable; otherwise the durable spool is used.
     */
    fun restore(
        source: UploadSource,
        filename: String,
        mimeType: String?,
        size: Long,
        sha256Hex: String,
        strategy: SourceStrategy,
        spoolName: String?
    ): PreparedUpload {
        val spool = if (strategy == SourceStrategy.PRIVATE_SPOOL) {
            spoolName?.let(::spoolFileByName)
                ?: throw TransferSourceException(
                    "staging_missing",
                    "The private staging copy for this upload is no longer available"
                )
        } else {
            null
        }
        if (spool != null && spool.length() != size) {
            throw TransferSourceException(
                "staging_modified",
                "The private staging copy no longer matches the declared size"
            )
        }
        return PreparedUpload(
            uri = source.descriptor.uri,
            filename = filename,
            mimeType = mimeType,
            size = size,
            sha256Hex = sha256Hex,
            strategy = strategy,
            source = source,
            spoolFile = spool
        )
    }

    private fun hashSource(
        source: UploadSource,
        expectedSize: Long,
        isCancelled: () -> Boolean
    ): Pair<String, Long> {
        val digest = java.security.MessageDigest.getInstance("SHA-256")
        val buffer = ByteArray(BUFFER_BYTES)
        var total = 0L
        source.openStream().use { stream ->
            while (true) {
                if (isCancelled()) throw TransferCancelledException()
                val read = stream.read(buffer)
                if (read < 0) break
                if (read == 0) continue
                total += read
                if (total > TransferValidation.MAX_UPLOAD_BYTES) {
                    throw TransferValidationException(
                        "size_exceeds_limit",
                        "file exceeds the 2 GiB upload limit"
                    )
                }
                digest.update(buffer, 0, read)
            }
        }
        return TransferCrypto.toHex(digest.digest()) to total
    }

    private data class SpoolResult(val file: File, val size: Long, val sha256Hex: String)

    private fun spoolSource(
        source: UploadSource,
        declaredSize: Long?,
        isCancelled: () -> Boolean
    ): SpoolResult {
        if (freeSpaceBytes() < minFreeBytes + (declaredSize ?: 0L)) {
            throw TransferSourceException(
                "insufficient_storage",
                "Not enough free app storage to stage this upload"
            )
        }
        val file = File(cacheDir, "kivo_spool_${UUID.randomUUID().toString().replace("-", "")}.part")
        if (!cacheDir.exists()) cacheDir.mkdirs()
        val digest = java.security.MessageDigest.getInstance("SHA-256")
        val buffer = ByteArray(BUFFER_BYTES)
        var total = 0L
        try {
            val output = FileOutputStream(file)
            try {
                source.openStream().use { stream ->
                    while (true) {
                        if (isCancelled()) throw TransferCancelledException()
                        val read = stream.read(buffer)
                        if (read < 0) break
                        if (read == 0) continue
                        total += read
                        if (total > TransferValidation.MAX_UPLOAD_BYTES) {
                            throw TransferValidationException(
                                "size_exceeds_limit",
                                "file exceeds the 2 GiB upload limit"
                            )
                        }
                        output.write(buffer, 0, read)
                        digest.update(buffer, 0, read)
                        if (freeSpaceBytes() < minFreeBytes) {
                            throw TransferSourceException(
                                "insufficient_storage",
                                "App storage was exhausted while staging this upload"
                            )
                        }
                    }
                }
                output.flush()
                output.fd.sync()
            } finally {
                output.close()
            }
            if (declaredSize != null && total != declaredSize) {
                throw TransferSourceException(
                    "source_changed",
                    "The document size changed while it was being staged"
                )
            }
            return SpoolResult(file, total, TransferCrypto.toHex(digest.digest()))
        } catch (e: Exception) {
            file.delete()
            throw e
        }
    }
}

object ChunkReader {
    /**
     * Reads exactly `expectedLength` bytes (at most 1 MiB) for one chunk.
     * The caller owns the stream and closes it explicitly.
     */
    fun readExactly(stream: InputStream, expectedLength: Int): ByteArray {
        require(expectedLength in 0..TransferValidation.CHUNK_BYTES.toInt())
        val buffer = ByteArray(expectedLength)
        var offset = 0
        while (offset < expectedLength) {
            val read = stream.read(buffer, offset, expectedLength - offset)
            if (read < 0) {
                throw TransferSourceException(
                    "source_changed",
                    "The document ended before the last complete chunk"
                )
            }
            offset += read
        }
        return buffer
    }

    fun writeEmpty(target: OutputStream) {
        target.flush()
    }
}
