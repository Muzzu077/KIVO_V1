package com.kivo.companion.transfer

import com.kivo.companion.storage.KeystoreSecretStorage
import org.json.JSONObject
import java.util.Locale

/**
 * Durable record for an interrupted upload. Contains no keys or bearer token;
 * those live in the encrypted session record. Only the transfer identity,
 * verified progress, and app-private staging filename are retained so a later
 * signed status query can resume the first uncommitted chunk.
 */
data class PersistedTransfer(
    val sessionId: String,
    val transferId: String,
    val uri: String,
    val filename: String,
    val mimeType: String?,
    val size: Long,
    val sha256Hex: String,
    val strategy: SourceStrategy,
    val spoolName: String?,
    val nextChunkIndex: Long,
    val receivedBytes: Long,
    val state: String,
    val initUncertain: Boolean,
    val errorCode: String? = null,
    val publishedFilename: String? = null,
    val createdAt: Long,
    val updatedAt: Long,
    val expiresAt: Long
) {
    fun toJson(): JSONObject = JSONObject().apply {
        put("record_type", "transfer_state")
        put("record_version", 1)
        put("sessionId", sessionId)
        put("transferId", transferId)
        put("uri", uri)
        put("filename", filename)
        put("mimeType", mimeType ?: JSONObject.NULL)
        put("size", size)
        put("sha256Hex", sha256Hex)
        put("strategy", strategy.name)
        put("spoolName", spoolName ?: JSONObject.NULL)
        put("nextChunkIndex", nextChunkIndex)
        put("receivedBytes", receivedBytes)
        put("state", state)
        put("initUncertain", initUncertain)
        put("errorCode", errorCode ?: JSONObject.NULL)
        put("publishedFilename", publishedFilename ?: JSONObject.NULL)
        put("createdAt", createdAt)
        put("updatedAt", updatedAt)
        put("expiresAt", expiresAt)
    }

    companion object {
        private val SESSION_PATTERN = Regex("^sess_[A-Za-z0-9_.-]{1,63}$")
        private val TRANSFER_PATTERN = Regex("^tr_[0-9a-f]{24}$")
        private val SHA = Regex("^[0-9a-f]{64}$")
        private val SPOOL = Regex("^kivo_spool_[a-f0-9]{32}\\.part$")

        fun fromJson(json: JSONObject): PersistedTransfer? {
            return try {
                if (json.optString("record_type") != "transfer_state") return null
                if (json.optInt("record_version", 0) != 1) return null
                val sessionId = json.optString("sessionId", "")
                val transferId = json.optString("transferId", "")
                val uri = json.optString("uri", "")
                val filename = json.optString("filename", "")
                val size = json.optLong("size", -1L)
                val sha = json.optString("sha256Hex", "")
                val strategy = SourceStrategy.valueOf(json.optString("strategy", ""))
                val spoolName = if (json.isNull("spoolName")) null else json.optString("spoolName", "")
                val nextChunkIndex = json.optLong("nextChunkIndex", -1L)
                val receivedBytes = json.optLong("receivedBytes", -1L)
                val state = json.optString("state", "")
                val initUncertain = json.optBoolean("initUncertain", false)
                val errorCode = if (json.isNull("errorCode")) null else json.optString("errorCode", "").ifEmpty { null }
                val publishedFilename = if (json.isNull("publishedFilename")) {
                    null
                } else {
                    json.optString("publishedFilename", "").ifEmpty { null }
                }
                val createdAt = json.optLong("createdAt", 0L)
                val updatedAt = json.optLong("updatedAt", 0L)
                val expiresAt = json.optLong("expiresAt", 0L)

                if (!SESSION_PATTERN.matches(sessionId)) return null
                if (!initUncertain && !TRANSFER_PATTERN.matches(transferId)) return null
                if (initUncertain && transferId.isNotEmpty() && !TRANSFER_PATTERN.matches(transferId)) return null
                if (uri.isEmpty() || !uri.startsWith("content://")) return null
                if (filename.isEmpty() || filename.length > 128) return null
                if (size !in 0L..TransferValidation.MAX_UPLOAD_BYTES) return null
                if (!SHA.matches(sha)) return null
                if (strategy == SourceStrategy.PRIVATE_SPOOL && (spoolName == null || !SPOOL.matches(spoolName))) return null
                if (strategy == SourceStrategy.REOPEN_TWO_PASS && spoolName != null) return null
                val totalChunks = TransferValidation.totalChunks(size)
                if (nextChunkIndex !in 0L..totalChunks) return null
                if (receivedBytes !in 0L..size) return null
                if (state.isEmpty() || state.length > 64) return null
                if (errorCode != null && (errorCode.isEmpty() || errorCode.length > 64)) return null
                if (publishedFilename != null) {
                    if (publishedFilename.isEmpty() || publishedFilename.length > 255) return null
                    if (publishedFilename.contains('/') || publishedFilename.contains('\\')) return null
                    if (publishedFilename.any { it.code < 0x20 || it.code == 0x7F }) return null
                }
                if (createdAt <= 0L || updatedAt <= 0L || expiresAt < 0L) return null

                PersistedTransfer(
                    sessionId = sessionId,
                    transferId = transferId,
                    uri = uri,
                    filename = filename,
                    mimeType = if (json.isNull("mimeType")) null else json.optString("mimeType", "").ifEmpty { null },
                    size = size,
                    sha256Hex = sha,
                    strategy = strategy,
                    spoolName = spoolName,
                    nextChunkIndex = nextChunkIndex,
                    receivedBytes = receivedBytes,
                    state = state,
                    initUncertain = initUncertain,
                    errorCode = errorCode,
                    publishedFilename = publishedFilename,
                    createdAt = createdAt,
                    updatedAt = updatedAt,
                    expiresAt = expiresAt
                )
            } catch (e: Exception) {
                null
            }
        }
    }
}

class TransferQueueStore(
    private val storage: KeystoreSecretStorage
) {
    @Synchronized
    fun save(record: PersistedTransfer): Boolean = storage.saveTransferState(record.toJson())

    @Synchronized
    fun load(): PersistedTransfer? {
        val json = storage.loadTransferState() ?: return null
        val record = PersistedTransfer.fromJson(json)
        if (record == null) storage.clearTransferState()
        return record
    }

    @Synchronized
    fun clear() {
        storage.clearTransferState()
    }
}

enum class TransferOperation {
    NONE,
    UPLOAD,
    DOWNLOAD
}

enum class TransferPhase {
    IDLE,
    PREPARING,
    WAITING_DESKTOP_APPROVAL,
    RECEIVING,
    VERIFYING,
    AWAITING_FINAL_ACCEPTANCE,
    COMMITTED,
    REJECTED,
    CANCELLED,
    EXPIRED,
    FAILED,
    INTERRUPTED,
    INIT_OUTCOME_UNKNOWN
}

data class TransferSnapshot(
    val phase: TransferPhase = TransferPhase.IDLE,
    val transferId: String? = null,
    val filename: String = "",
    val size: Long = 0L,
    val receivedBytes: Long = 0L,
    val state: String = "",
    val publishedFilename: String? = null,
    val errorCode: String? = null,
    val message: String = "",
    val attempt: Int = 0,
    val resumable: Boolean = false
) {
    val progressPercent: Int
        get() = when {
            phase == TransferPhase.COMMITTED -> 100
            size <= 0L -> 0
            else -> ((receivedBytes.coerceIn(0L, size) * 100L) / size).toInt()
        }
}

data class TransferOutcome(
    val snapshot: TransferSnapshot,
    val record: PersistedTransfer? = null
)

object TransferProtocolLimits {
    const val MAX_AGGREGATE_BYTES = 2L * 1024L * 1024L * 1024L
    const val MAX_NETWORK_RETRIES = 5
    const val STATUS_POLL_INTERVAL_MS = 2_000L
    const val APPROVAL_WAIT_SECONDS = 75L
    const val FINAL_ACCEPTANCE_FALLBACK_SECONDS = 900L
    const val MAX_TRANSFER_ID_CHARS = 64
    const val MAX_PAYLOAD_FIELD_BYTES = 4096

    fun normalizedState(value: String): String = value.lowercase(Locale.ROOT)
}
