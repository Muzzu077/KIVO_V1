package com.kivo.companion.transfer

import java.util.Locale

/**
 * Truthful download phases. `COMPLETED` is set only after the selected SAF
 * destination has been written, flushed, and (where the provider allows it)
 * reopened and re-hashed. `READY_TO_PUBLISH` means the whole file is verified
 * but no destination has been chosen yet.
 */
enum class DownloadPhase {
    IDLE,
    OFFER,
    PREPARING,
    DOWNLOADING,
    RECONNECTING,
    VERIFYING,
    READY_TO_PUBLISH,
    PUBLISHING,
    COMPLETED,
    CANCELLED,
    EXPIRED,
    FAILED,
    INTERRUPTED
}

data class DownloadSnapshot(
    val phase: DownloadPhase = DownloadPhase.IDLE,
    val ticketId: String? = null,
    val filename: String = "",
    val size: Long = 0L,
    val receivedBytes: Long = 0L,
    val desktopLabel: String = "",
    val sha256Hex: String? = null,
    val fileType: String? = null,
    val errorCode: String? = null,
    val message: String = "",
    val attempt: Int = 0,
    val resumable: Boolean = false,
    val publishedName: String? = null,
    val destinationVerified: Boolean? = null
) {
    val progressPercent: Int
        get() {
            if (phase == DownloadPhase.COMPLETED) return 100
            if (phase in VERIFIED_PHASES) return 100
            if (size <= 0L) return 0
            val percent = ((receivedBytes.coerceIn(0L, size) * 100L) / size).toInt()
            return percent.coerceIn(0, 99)
        }

    val offerReady: Boolean get() = phase == DownloadPhase.OFFER
    val requiresDestination: Boolean get() = phase == DownloadPhase.READY_TO_PUBLISH
    val canCancel: Boolean
        get() = phase in ACTIVE_PHASES || requiresDestination ||
            phase == DownloadPhase.INTERRUPTED
    val isTerminal: Boolean get() = phase in TERMINAL_PHASES

    companion object {
        val VERIFIED_PHASES = setOf(
            DownloadPhase.VERIFYING,
            DownloadPhase.READY_TO_PUBLISH,
            DownloadPhase.PUBLISHING,
            DownloadPhase.COMPLETED
        )
        val ACTIVE_PHASES = setOf(
            DownloadPhase.PREPARING,
            DownloadPhase.DOWNLOADING,
            DownloadPhase.RECONNECTING,
            DownloadPhase.VERIFYING,
            DownloadPhase.PUBLISHING
        )
        val TERMINAL_PHASES = setOf(
            DownloadPhase.COMPLETED,
            DownloadPhase.CANCELLED,
            DownloadPhase.EXPIRED,
            DownloadPhase.FAILED
        )
    }
}

fun formatDownloadPhase(phase: DownloadPhase): String = when (phase) {
    DownloadPhase.IDLE -> "No download"
    DownloadPhase.OFFER -> "Download offer"
    DownloadPhase.PREPARING -> "Preparing download"
    DownloadPhase.DOWNLOADING -> "Downloading"
    DownloadPhase.RECONNECTING -> "Reconnecting"
    DownloadPhase.VERIFYING -> "Verifying integrity"
    DownloadPhase.READY_TO_PUBLISH -> "Verified • choose destination"
    DownloadPhase.PUBLISHING -> "Saving to destination"
    DownloadPhase.COMPLETED -> "Saved"
    DownloadPhase.CANCELLED -> "Cancelled"
    DownloadPhase.EXPIRED -> "Expired"
    DownloadPhase.FAILED -> "Failed"
    DownloadPhase.INTERRUPTED -> "Interrupted"
}

object DownloadNotificationFactory {
    fun from(snapshot: DownloadSnapshot, active: Boolean): TransferNotificationContent {
        val label = snapshot.filename.ifBlank { "Desktop file" }
        val bytes = if (snapshot.size > 0L) {
            "${formatByteCount(snapshot.receivedBytes)} of ${formatByteCount(snapshot.size)}"
        } else {
            ""
        }
        return when (snapshot.phase) {
            DownloadPhase.OFFER -> content(
                title = "Download offered",
                text = "$label • open KIVO Companion to review"
            )
            DownloadPhase.PREPARING -> content(
                title = "Preparing download",
                text = "$label • claiming the desktop ticket",
                showProgress = true,
                indeterminate = true,
                cancel = active
            )
            DownloadPhase.DOWNLOADING -> content(
                title = "Downloading from KIVO",
                text = "$label • ${snapshot.progressPercent}% ($bytes)",
                showProgress = true,
                progress = snapshot.progressPercent,
                cancel = active
            )
            DownloadPhase.RECONNECTING -> content(
                title = "Reconnecting to KIVO",
                text = "$label • retry ${snapshot.attempt}",
                showProgress = true,
                indeterminate = true,
                cancel = active
            )
            DownloadPhase.VERIFYING -> content(
                title = "Verifying download",
                text = "$label • checking the whole-file SHA-256",
                showProgress = true,
                indeterminate = true,
                cancel = active
            )
            DownloadPhase.READY_TO_PUBLISH -> content(
                title = "Download verified",
                text = "$label • open KIVO Companion and choose a destination",
                showProgress = true,
                progress = 100
            )
            DownloadPhase.PUBLISHING -> content(
                title = "Saving download",
                text = "$label • writing to the selected destination",
                showProgress = true,
                indeterminate = true,
                cancel = active
            )
            DownloadPhase.COMPLETED -> content(
                title = "Download saved",
                text = snapshot.publishedName?.let { "Saved as $it" } ?: label,
                showProgress = true,
                progress = 100
            )
            DownloadPhase.INTERRUPTED -> content(
                title = "Download interrupted",
                text = "$label • open KIVO Companion to resume",
                showProgress = snapshot.size > 0L,
                progress = snapshot.progressPercent
            )
            DownloadPhase.CANCELLED -> content(
                title = "Download cancelled",
                text = snapshot.message.ifBlank { label }
            )
            DownloadPhase.EXPIRED -> content(
                title = "Download expired",
                text = snapshot.message.ifBlank { "$label is no longer available" }
            )
            DownloadPhase.FAILED -> content(
                title = "Download failed",
                text = snapshot.message.ifBlank { label }
            )
            DownloadPhase.IDLE -> content(
                title = "KIVO Companion",
                text = "No active download"
            )
        }
    }

    private fun content(
        title: String,
        text: String,
        showProgress: Boolean = false,
        indeterminate: Boolean = false,
        progress: Int = 0,
        cancel: Boolean = false
    ): TransferNotificationContent = TransferNotificationContent(
        title = title,
        text = text,
        showProgress = showProgress,
        indeterminate = indeterminate,
        progressPercent = progress.coerceIn(0, 100),
        showCancel = cancel
    )
}

/** MIME hint for the SAF create-document picker; never a filesystem path. */
fun suggestedDownloadMime(fileType: String?, filename: String): String {
    val extension = when {
        !fileType.isNullOrBlank() && fileType != "unknown" ->
            fileType.substringAfterLast('/').lowercase(Locale.ROOT)
        filename.contains('.') -> filename.substringAfterLast('.').lowercase(Locale.ROOT)
        else -> ""
    }
    return when (extension) {
        "pdf" -> "application/pdf"
        "txt" -> "text/plain"
        "png" -> "image/png"
        "jpg", "jpeg" -> "image/jpeg"
        "webp" -> "image/webp"
        "json" -> "application/json"
        "zip" -> "application/zip"
        "mp3" -> "audio/mpeg"
        "mp4" -> "video/mp4"
        else -> "application/octet-stream"
    }
}
