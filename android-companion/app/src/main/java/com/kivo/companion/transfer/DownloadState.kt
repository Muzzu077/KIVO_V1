package com.kivo.companion.transfer

import com.kivo.companion.storage.KeystoreSecretStorage
import org.json.JSONObject
import java.io.File

/**
 * Durable record for an interrupted download. It contains no keys, bearer
 * token, HMAC material, or filesystem paths outside the app-private staging
 * directory. The desktop ticket identifier is an opaque capability that still
 * requires the paired session's token and `K_up` MAC for every request.
 */
data class PersistedDownload(
    val sessionId: String,
    val ticketId: String,
    val deviceId: String,
    val filename: String,
    val fileType: String?,
    val size: Long,
    val sha256Hex: String,
    val nextChunkIndex: Long,
    val receivedBytes: Long,
    val stageName: String,
    val state: String,
    val completeAcknowledged: Boolean,
    val publishedName: String? = null,
    val errorCode: String? = null,
    val createdAt: Long,
    val updatedAt: Long,
    val leaseExpiresAt: Long
) {
    fun totalChunks(): Long = totalDownloadChunks(size)

    fun toJson(): JSONObject = JSONObject().apply {
        put("record_type", "download_state")
        put("record_version", 1)
        put("sessionId", sessionId)
        put("ticketId", ticketId)
        put("deviceId", deviceId)
        put("filename", filename)
        put("fileType", fileType ?: JSONObject.NULL)
        put("size", size)
        put("sha256Hex", sha256Hex)
        put("chunkBytes", DOWNLOAD_CHUNK_BYTES)
        put("nextChunkIndex", nextChunkIndex)
        put("receivedBytes", receivedBytes)
        put("stageName", stageName)
        put("state", state)
        put("completeAcknowledged", completeAcknowledged)
        put("publishedName", publishedName ?: JSONObject.NULL)
        put("errorCode", errorCode ?: JSONObject.NULL)
        put("createdAt", createdAt)
        put("updatedAt", updatedAt)
        put("leaseExpiresAt", leaseExpiresAt)
    }

    companion object {
        private val SESSION_PATTERN = Regex("^sess_[A-Za-z0-9_.-]{1,63}$")
        private val TICKET_PATTERN = Regex("^tkt_[0-9a-f]{64}$")
        private val DEVICE_PATTERN = Regex("^client_[A-Za-z0-9_.-]{1,57}$")
        private val SHA = Regex("^[0-9a-f]{64}$")
        val STAGE_PATTERN = Regex("^kivo_download_[a-f0-9]{32}\\.part$")
        val STATES = setOf(
            "preparing",
            "claimed",
            "downloading",
            "verified",
            "ready_to_publish",
            "publishing",
            "completed",
            "cancelled",
            "expired",
            "failed"
        )

        fun fromJson(json: JSONObject): PersistedDownload? {
            return try {
                if (json.optString("record_type") != "download_state") return null
                if (json.optInt("record_version", 0) != 1) return null
                val sessionId = json.optString("sessionId", "")
                val ticketId = json.optString("ticketId", "")
                val deviceId = json.optString("deviceId", "")
                val filename = json.optString("filename", "")
                val fileType = if (json.isNull("fileType")) {
                    null
                } else {
                    json.optString("fileType", "").ifEmpty { null }
                }
                val size = json.optLong("size", -1L)
                val sha = json.optString("sha256Hex", "")
                val chunkBytes = json.optLong("chunkBytes", -1L)
                val nextChunkIndex = json.optLong("nextChunkIndex", -1L)
                val receivedBytes = json.optLong("receivedBytes", -1L)
                val stageName = json.optString("stageName", "")
                val state = json.optString("state", "")
                val completeAcknowledged = json.optBoolean("completeAcknowledged", false)
                val publishedName = if (json.isNull("publishedName")) {
                    null
                } else {
                    json.optString("publishedName", "").ifEmpty { null }
                }
                val errorCode = if (json.isNull("errorCode")) {
                    null
                } else {
                    json.optString("errorCode", "").ifEmpty { null }
                }
                val createdAt = json.optLong("createdAt", 0L)
                val updatedAt = json.optLong("updatedAt", 0L)
                val leaseExpiresAt = json.optLong("leaseExpiresAt", 0L)

                if (!SESSION_PATTERN.matches(sessionId)) return null
                if (!TICKET_PATTERN.matches(ticketId)) return null
                if (!DEVICE_PATTERN.matches(deviceId)) return null
                if (filename.isEmpty() || filename.length > TransferValidation.MAX_FILENAME_CHARS) return null
                if (filename.contains('/') || filename.contains('\\')) return null
                if (filename.any { it.code < 0x20 || it.code == 0x7F }) return null
                if (fileType != null && (fileType.isEmpty() || fileType.length > 64)) return null
                if (fileType != null && fileType.any { it.code < 0x20 || it.code == 0x7F }) return null
                if (size !in 0L..DownloadTicketParser.MAX_DOWNLOAD_BYTES) return null
                if (!SHA.matches(sha)) return null
                if (chunkBytes != DOWNLOAD_CHUNK_BYTES) return null
                val totalChunks = totalDownloadChunks(size)
                if (nextChunkIndex !in 0L..totalChunks) return null
                if (receivedBytes != expectedDownloadBytes(nextChunkIndex, size)) return null
                if (!STAGE_PATTERN.matches(stageName)) return null
                if (state !in STATES) return null
                if (errorCode != null && (errorCode.isEmpty() || errorCode.length > 64)) return null
                if (publishedName != null) {
                    if (publishedName.isEmpty() || publishedName.length > 255) return null
                    if (publishedName.contains('/') || publishedName.contains('\\')) return null
                    if (publishedName.any { it.code < 0x20 || it.code == 0x7F }) return null
                }
                if (createdAt <= 0L || updatedAt <= 0L) return null
                if (updatedAt < createdAt) return null
                if (leaseExpiresAt < 0L) return null
                if (state in VERIFIED_STATES &&
                    (nextChunkIndex != totalChunks || receivedBytes != size)
                ) {
                    return null
                }
                if (state == "preparing" && nextChunkIndex != 0L) return null
                if (state == "completed" && !completeAcknowledged) return null

                PersistedDownload(
                    sessionId = sessionId,
                    ticketId = ticketId,
                    deviceId = deviceId,
                    filename = filename,
                    fileType = fileType,
                    size = size,
                    sha256Hex = sha,
                    nextChunkIndex = nextChunkIndex,
                    receivedBytes = receivedBytes,
                    stageName = stageName,
                    state = state,
                    completeAcknowledged = completeAcknowledged,
                    publishedName = publishedName,
                    errorCode = errorCode,
                    createdAt = createdAt,
                    updatedAt = updatedAt,
                    leaseExpiresAt = leaseExpiresAt
                )
            } catch (e: Exception) {
                null
            }
        }

        val VERIFIED_STATES = setOf("verified", "ready_to_publish", "publishing", "completed")
    }
}

class DownloadRecoveryStore(
    private val storage: KeystoreSecretStorage
) {
    @Synchronized
    fun save(record: PersistedDownload): Boolean = storage.saveDownloadState(record.toJson())

    @Synchronized
    fun load(): PersistedDownload? {
        val json = storage.loadDownloadState() ?: return null
        val record = PersistedDownload.fromJson(json)
        if (record == null) storage.clearDownloadState()
        return record
    }

    @Synchronized
    fun clear() {
        storage.clearDownloadState()
    }
}

const val DOWNLOAD_CHUNK_BYTES: Long = 1024L * 1024L

/** Fixed 1 MiB protocol geometry; an empty file has zero chunks. */
fun totalDownloadChunks(size: Long): Long {
    if (size <= 0L) return 0L
    return (size + DOWNLOAD_CHUNK_BYTES - 1L) / DOWNLOAD_CHUNK_BYTES
}

fun expectedDownloadChunkLength(size: Long, chunkIndex: Long): Long {
    if (chunkIndex < 0L || chunkIndex >= totalDownloadChunks(size)) return -1L
    val offset = chunkIndex * DOWNLOAD_CHUNK_BYTES
    return minOf(DOWNLOAD_CHUNK_BYTES, size - offset)
}

fun expectedDownloadBytes(nextChunkIndex: Long, size: Long): Long =
    minOf(size, nextChunkIndex * DOWNLOAD_CHUNK_BYTES)

/** Deletes app-private staging files that no durable download record references. */
fun cleanupOrphanDownloadStages(directory: File, keepName: String?) {
    val files = directory.listFiles() ?: return
    for (file in files) {
        if (PersistedDownload.STAGE_PATTERN.matches(file.name) && file.name != keepName) {
            try {
                file.delete()
            } catch (e: Exception) {
                // The file remains app-private and encrypted state no longer
                // references it; a later startup retries cleanup.
            }
        }
    }
}
