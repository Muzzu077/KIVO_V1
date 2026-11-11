package com.kivo.companion.transfer

import java.util.Locale

/**
 * UI-facing metadata for one user-selected document. It is derived before any
 * network request and contains no provider path. The URI is only used to reopen
 * the provider stream at upload time.
 */
data class DocumentSummary(
    val uri: String,
    val displayName: String,
    val sizeBytes: Long?,
    val mimeType: String?,
    val persistable: Boolean,
    val errorCode: String? = null,
    val errorMessage: String? = null
) {
    val supported: Boolean get() = errorCode == null

    /** Explains the local source strategy without exposing provider internals. */
    val accessHint: String
        get() = if (persistable) {
            "Read access retained. The file is hashed and streamed without a private copy."
        } else {
            "The provider does not grant durable access. A private staging copy is required."
        }

    companion object {
        fun rejected(uri: String, code: String, message: String): DocumentSummary =
            DocumentSummary(
                uri = uri,
                displayName = "",
                sizeBytes = null,
                mimeType = null,
                persistable = false,
                errorCode = code,
                errorMessage = message
            )
    }
}

object DocumentSummaryFactory {
    fun from(descriptor: DocumentDescriptor): DocumentSummary {
        val uri = descriptor.uri
        if (!uri.startsWith("content://")) {
            return DocumentSummary.rejected(
                uri,
                "unsupported_source_scheme",
                "Only documents chosen through the system picker can be uploaded"
            )
        }
        if (descriptor.isDirectory) {
            return DocumentSummary.rejected(
                uri,
                "source_is_directory",
                "Folders cannot be uploaded. Select individual files."
            )
        }

        val filename = try {
            TransferValidation.validateDisplayFilename(descriptor.displayName)
        } catch (e: TransferValidationException) {
            return DocumentSummary.rejected(uri, e.code, e.message ?: "The filename is not supported")
        }

        val size = descriptor.sizeBytes
        if (size != null) {
            try {
                TransferValidation.validateDeclaredSize(size)
            } catch (e: TransferValidationException) {
                return DocumentSummary.rejected(uri, e.code, e.message ?: "The file is too large")
            }
        }

        return DocumentSummary(
            uri = uri,
            displayName = filename,
            sizeBytes = size,
            mimeType = TransferValidation.sanitizeMimeHint(descriptor.mimeType),
            persistable = descriptor.persistable && descriptor.reopenable
        )
    }
}

/**
 * Pure notification projection so the foreground service never builds text
 * from unverified protocol fields and never exposes keys, nonces, or tokens.
 */
data class TransferNotificationContent(
    val title: String,
    val text: String,
    val showProgress: Boolean,
    val indeterminate: Boolean,
    val progressPercent: Int,
    val showCancel: Boolean
)

object TransferNotificationFactory {
    fun from(snapshot: TransferSnapshot, active: Boolean): TransferNotificationContent {
        val label = snapshot.filename.ifBlank { "Selected file" }
        val bytes = if (snapshot.size > 0L) {
            "${formatByteCount(snapshot.receivedBytes)} of ${formatByteCount(snapshot.size)}"
        } else {
            ""
        }
        return when (snapshot.phase) {
            TransferPhase.PREPARING -> content(
                title = "Preparing upload",
                text = "$label • validating and hashing",
                showProgress = true,
                indeterminate = true,
                cancel = active
            )
            TransferPhase.WAITING_DESKTOP_APPROVAL -> content(
                title = "Waiting for desktop approval",
                text = "$label • approve the upload in KIVO on your desktop",
                cancel = active
            )
            TransferPhase.RECEIVING -> content(
                title = "Uploading to KIVO",
                text = "$label • ${snapshot.progressPercent}% ($bytes)",
                showProgress = true,
                progress = snapshot.progressPercent,
                cancel = active
            )
            TransferPhase.VERIFYING -> content(
                title = "Verifying upload",
                text = "$label • the desktop is checking the file hash",
                showProgress = true,
                progress = 100,
                cancel = active
            )
            TransferPhase.AWAITING_FINAL_ACCEPTANCE -> content(
                title = "Waiting for final acceptance",
                text = "$label • verified, accept it on your desktop to finish",
                showProgress = true,
                progress = 100,
                cancel = active
            )
            TransferPhase.COMMITTED -> content(
                title = "Upload complete",
                text = snapshot.publishedFilename?.let { "Saved to Downloads as $it" }
                    ?: "Accepted by the desktop",
                showProgress = true,
                progress = 100
            )
            TransferPhase.REJECTED -> content(
                title = "Upload rejected",
                text = snapshot.message.ifBlank { "The desktop rejected $label" }
            )
            TransferPhase.CANCELLED -> content(
                title = "Upload cancelled",
                text = snapshot.message.ifBlank { label }
            )
            TransferPhase.EXPIRED -> content(
                title = "Upload expired",
                text = snapshot.message.ifBlank { "$label was not completed in time" }
            )
            TransferPhase.FAILED -> content(
                title = "Upload failed",
                text = snapshot.message.ifBlank { label }
            )
            TransferPhase.INTERRUPTED -> content(
                title = "Upload interrupted",
                text = "$label • open KIVO Companion to resume",
                showProgress = snapshot.size > 0L,
                indeterminate = false,
                progress = snapshot.progressPercent
            )
            TransferPhase.INIT_OUTCOME_UNKNOWN -> content(
                title = "Upload status unknown",
                text = "$label • open KIVO Companion for details"
            )
            TransferPhase.IDLE -> content(
                title = "KIVO Companion",
                text = "No active upload"
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

fun formatByteCount(bytes: Long): String {
    if (bytes < 0L) return "0 B"
    if (bytes < 1024L) return "$bytes B"
    val kib = bytes / 1024.0
    if (kib < 1024.0) return String.format(Locale.US, "%.1f KiB", kib)
    val mib = kib / 1024.0
    if (mib < 1024.0) return String.format(Locale.US, "%.1f MiB", mib)
    val gib = mib / 1024.0
    return String.format(Locale.US, "%.2f GiB", gib)
}

fun formatTransferPhase(phase: TransferPhase): String = when (phase) {
    TransferPhase.IDLE -> "No upload in progress"
    TransferPhase.PREPARING -> "Preparing"
    TransferPhase.WAITING_DESKTOP_APPROVAL -> "Waiting for desktop approval"
    TransferPhase.RECEIVING -> "Uploading"
    TransferPhase.VERIFYING -> "Verifying"
    TransferPhase.AWAITING_FINAL_ACCEPTANCE -> "Waiting for final acceptance"
    TransferPhase.COMMITTED -> "Completed"
    TransferPhase.REJECTED -> "Rejected"
    TransferPhase.CANCELLED -> "Cancelled"
    TransferPhase.EXPIRED -> "Expired"
    TransferPhase.FAILED -> "Failed"
    TransferPhase.INTERRUPTED -> "Interrupted"
    TransferPhase.INIT_OUTCOME_UNKNOWN -> "Status unknown"
}
