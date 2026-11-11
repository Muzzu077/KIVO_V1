package com.kivo.companion.transfer

import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.RandomAccessFile
import java.nio.file.Files
import java.security.MessageDigest
import java.security.SecureRandom

/**
 * App-private staging for a desktop download.
 *
 * The staging directory lives under `filesDir`, never shared storage. Names are
 * random and path-free; every operation resolves the name inside the directory
 * and rejects symlinks or canonical-path escapes. Only the current network
 * chunk is held in memory while bytes are appended with a 64 KiB copy buffer,
 * and each committed chunk is fsynced before its progress is persisted.
 */
class DownloadStager(
    private val directory: File,
    private val freeSpaceBytes: () -> Long = { directory.usableSpace },
    private val minFreeBytes: Long = DEFAULT_MIN_FREE_BYTES,
    private val bufferBytes: Int = BUFFER_BYTES,
    private val secureRandom: SecureRandom = SecureRandom()
) {
    companion object {
        const val BUFFER_BYTES = 64 * 1024
        const val DEFAULT_MIN_FREE_BYTES = 32L * 1024L * 1024L
        private const val MAX_NAME_ATTEMPTS = 8
    }

    init {
        require(bufferBytes in 4096..(1024 * 1024)) { "copy buffer must be bounded" }
        if (!directory.exists()) {
            require(directory.mkdirs() || directory.isDirectory) {
                "download staging directory is unavailable"
            }
        }
        require(directory.isDirectory) { "download staging path is not a directory" }
        require(!Files.isSymbolicLink(directory.toPath())) {
            "download staging directory must not be a symlink"
        }
    }

    fun stagingDirectory(): File = directory

    /** Creates a new empty app-private stage with a random 128-bit suffix. */
    @Synchronized
    fun newStage(): File {
        repeat(MAX_NAME_ATTEMPTS) {
            val suffix = ByteArray(16).also(secureRandom::nextBytes)
            val name = "kivo_download_${TransferCrypto.toHex(suffix)}.part"
            val file = File(directory, name)
            if (file.createNewFile()) {
                validateStage(file)
                return file
            }
        }
        throw TransferSourceException(
            "staging_unavailable",
            "Could not create a private download staging file"
        )
    }

    /** Resolves a durable stage name without ever escaping the staging directory. */
    fun stageByName(name: String): File? {
        if (!PersistedDownload.STAGE_PATTERN.matches(name)) return null
        val file = File(directory, name)
        if (!file.isFile) return null
        if (Files.isSymbolicLink(file.toPath())) return null
        val canonicalParent = try {
            file.canonicalFile.parentFile
        } catch (e: Exception) {
            null
        }
        val canonicalDirectory = try {
            directory.canonicalFile
        } catch (e: Exception) {
            null
        }
        if (canonicalParent == null || canonicalDirectory == null) return null
        return if (canonicalParent == canonicalDirectory) file else null
    }

    /** Reserves the declared file length after checking free space. */
    @Synchronized
    fun prepare(stage: File, size: Long) {
        validateStage(stage)
        require(size in 0L..DownloadTicketParser.MAX_DOWNLOAD_BYTES) {
            "download size is out of range"
        }
        if (size > 0L) {
            val available = freeSpaceBytes()
            if (available < size + minFreeBytes) {
                throw TransferSourceException(
                    "insufficient_storage",
                    "Not enough free space for the private download staging copy"
                )
            }
        }
        RandomAccessFile(stage, "rw").use { file ->
            file.setLength(size)
            file.fd.sync()
        }
    }

    /** Appends one verified network chunk at its exact file offset. */
    @Synchronized
    fun append(stage: File, offset: Long, data: ByteArray) {
        validateStage(stage)
        require(offset >= 0L) { "negative staging offset" }
        require(data.isNotEmpty()) { "empty chunk" }
        val expectedLength = stage.length()
        if (offset + data.size > expectedLength) {
            throw TransferSourceException(
                "staging_geometry",
                "The download chunk does not fit the reserved staging file"
            )
        }
        RandomAccessFile(stage, "rw").use { file ->
            file.seek(offset)
            file.write(data)
            file.fd.sync()
        }
    }

    @Synchronized
    fun truncate(stage: File, length: Long) {
        validateStage(stage)
        require(length in 0L..DownloadTicketParser.MAX_DOWNLOAD_BYTES) {
            "staging length is out of range"
        }
        RandomAccessFile(stage, "rw").use { file ->
            file.setLength(length)
            file.fd.sync()
        }
    }

    /**
     * Hashes the staged prefix in 64 KiB reads. The caller may cancel between
     * buffers; no whole-file buffer is ever allocated.
     */
    fun hashStage(stage: File, isCancelled: () -> Boolean = { false }): String {
        validateStage(stage)
        val digest = MessageDigest.getInstance("SHA-256")
        val buffer = ByteArray(bufferBytes)
        FileInputStream(stage).use { input ->
            while (true) {
                if (isCancelled()) throw TransferCancelledException()
                val read = input.read(buffer)
                if (read < 0) break
                if (read > 0) digest.update(buffer, 0, read)
            }
        }
        return TransferCrypto.toHex(digest.digest())
    }

    fun verifyStage(
        stage: File,
        size: Long,
        sha256Hex: String,
        isCancelled: () -> Boolean = { false }
    ): Boolean {
        if (stage.length() != size) return false
        return java.security.MessageDigest.isEqual(
            hashStage(stage, isCancelled).toByteArray(Charsets.US_ASCII),
            sha256Hex.lowercase(java.util.Locale.ROOT).toByteArray(Charsets.US_ASCII)
        )
    }

    /** Copies a verified staging file to an output with a bounded 64 KiB buffer. */
    @Synchronized
    fun copyTo(
        stage: File,
        output: java.io.OutputStream,
        isCancelled: () -> Boolean = { false }
    ): Long {
        validateStage(stage)
        val buffer = ByteArray(bufferBytes)
        var total = 0L
        FileInputStream(stage).use { input ->
            while (true) {
                if (isCancelled()) throw TransferCancelledException()
                val read = input.read(buffer)
                if (read < 0) break
                if (read > 0) {
                    output.write(buffer, 0, read)
                    total += read
                }
            }
        }
        output.flush()
        return total
    }

    @Synchronized
    fun delete(stage: File) {
        try {
            if (stage.exists() && !Files.isSymbolicLink(stage.toPath())) {
                stage.delete()
            }
        } catch (e: Exception) {
            // Best effort. The file remains app-private and a later startup
            // cleanup removes unreferenced stages.
        }
    }

    @Synchronized
    fun deleteByName(name: String) {
        stageByName(name)?.let { delete(it) }
    }

    @Synchronized
    fun cleanupOrphans(keepName: String?) {
        cleanupOrphanDownloadStages(directory, keepName)
    }

    private fun validateStage(stage: File) {
        require(PersistedDownload.STAGE_PATTERN.matches(stage.name)) {
            "invalid staging filename"
        }
        if (Files.isSymbolicLink(stage.toPath())) {
            throw TransferSourceException(
                "staging_invalid",
                "The private download staging file must not be a symlink"
            )
        }
        val canonical = try {
            stage.canonicalFile
        } catch (e: Exception) {
            throw TransferSourceException("staging_invalid", "The staging path is invalid")
        }
        if (canonical.parentFile != directory.canonicalFile) {
            throw TransferSourceException(
                "staging_invalid",
                "The private download staging file is outside its directory"
            )
        }
    }
}
