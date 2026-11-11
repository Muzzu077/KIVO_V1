package com.kivo.companion.ui.screens

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContract
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import com.kivo.companion.transfer.DocumentSummary
import com.kivo.companion.transfer.DownloadOffer
import com.kivo.companion.transfer.DownloadPhase
import com.kivo.companion.transfer.DownloadSnapshot
import com.kivo.companion.transfer.TransferPhase
import com.kivo.companion.transfer.TransferSnapshot
import com.kivo.companion.transfer.formatByteCount
import com.kivo.companion.transfer.formatDownloadPhase
import com.kivo.companion.transfer.formatTransferPhase
import com.kivo.companion.transfer.suggestedDownloadMime
import com.kivo.companion.ui.theme.Accent
import com.kivo.companion.ui.theme.Border
import com.kivo.companion.ui.theme.Danger
import com.kivo.companion.ui.theme.Mint
import com.kivo.companion.ui.theme.SurfaceRaised
import com.kivo.companion.ui.theme.SurfaceSunken
import com.kivo.companion.ui.theme.TextMuted
import com.kivo.companion.ui.theme.TextPrimary
import com.kivo.companion.ui.theme.TextSecondary
import com.kivo.companion.ui.theme.Warning

/**
 * SAF contract that permits both single and multiple document selection, keeps
 * the openable-document category, and asks for a persistable read grant. There
 * is no broad storage permission and no direct filesystem access.
 */
class OpenDocumentsContract : ActivityResultContract<Unit, List<Uri>>() {
    override fun createIntent(context: Context, input: Unit): Intent {
        return Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = "*/*"
            putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
            addFlags(
                Intent.FLAG_GRANT_READ_URI_PERMISSION or
                    Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION
            )
        }
    }

    override fun parseResult(resultCode: Int, intent: Intent?): List<Uri> {
        if (resultCode != Activity.RESULT_OK || intent == null) return emptyList()
        val clip = intent.clipData
        if (clip != null) {
            val uris = mutableListOf<Uri>()
            for (index in 0 until clip.itemCount) {
                clip.getItemAt(index).uri?.let { uris.add(it) }
            }
            if (uris.isNotEmpty()) return uris
        }
        return listOfNotNull(intent.data)
    }
}

private val ACTIVE_PHASES = setOf(
    TransferPhase.PREPARING,
    TransferPhase.WAITING_DESKTOP_APPROVAL,
    TransferPhase.RECEIVING,
    TransferPhase.VERIFYING,
    TransferPhase.AWAITING_FINAL_ACCEPTANCE
)

data class CreateDownloadRequest(
    val mimeType: String,
    val filename: String
)

/** Strict `ACTION_CREATE_DOCUMENT`; the returned URI is used once, never stored. */
class CreateDownloadDocumentContract : ActivityResultContract<CreateDownloadRequest, Uri?>() {
    override fun createIntent(context: Context, input: CreateDownloadRequest): Intent {
        return Intent(Intent.ACTION_CREATE_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = input.mimeType.ifBlank { "application/octet-stream" }
            putExtra(Intent.EXTRA_TITLE, input.filename.ifBlank { "kivo-download.bin" })
        }
    }

    override fun parseResult(resultCode: Int, intent: Intent?): Uri? {
        if (resultCode != Activity.RESULT_OK) return null
        return intent?.data
    }
}

@Composable
fun PairedScreen(
    hostAddress: String,
    fingerprintSuffix: String,
    transferSnapshot: TransferSnapshot,
    transferActive: Boolean,
    downloadSnapshot: DownloadSnapshot,
    downloadActive: Boolean,
    pendingDownloadOffer: DownloadOffer?,
    downloadHandoffError: String?,
    selection: List<DocumentSummary>,
    onUrisSelected: (List<String>) -> Unit,
    onRemoveSelected: (String) -> Unit,
    onClearSelection: () -> Unit,
    onConfirmUpload: () -> Unit,
    onCancelTransfer: () -> Unit,
    onResumeTransfer: () -> Unit,
    onDismissOutcome: () -> Unit,
    onDownloadQrScanned: (String) -> Unit,
    onConfirmDownload: () -> Unit,
    onDismissDownloadOffer: () -> Unit,
    onResumeDownload: () -> Unit,
    onCancelDownload: () -> Unit,
    onPublishDownload: (String) -> Unit,
    onDismissDownloadOutcome: () -> Unit,
    onDisconnect: () -> Unit
) {
    val context = LocalContext.current
    var showDownloadScanner by remember { mutableStateOf(false) }
    if (showDownloadScanner) {
        QrScannerScreen(
            onQrScanned = { raw ->
                showDownloadScanner = false
                onDownloadQrScanned(raw)
            },
            onCancel = { showDownloadScanner = false }
        )
        return
    }

    val documentPicker = rememberLauncherForActivityResult(OpenDocumentsContract()) { uris ->
        if (uris.isNotEmpty()) onUrisSelected(uris.map(Uri::toString))
    }
    val destinationPicker = rememberLauncherForActivityResult(
        CreateDownloadDocumentContract()
    ) { uri ->
        if (uri != null) onPublishDownload(uri.toString())
    }

    var notificationsDenied by remember { mutableStateOf(false) }
    var pendingAction by remember { mutableStateOf<(() -> Unit)?>(null) }
    val notificationPermission = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        notificationsDenied = !granted
        val action = pendingAction
        pendingAction = null
        action?.invoke()
    }

    fun withNotificationPermission(action: () -> Unit) {
        val needsPermission = Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.POST_NOTIFICATIONS
            ) != PackageManager.PERMISSION_GRANTED
        if (needsPermission) {
            pendingAction = action
            notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        } else {
            action()
        }
    }

    val blockingRecord = transferSnapshot.phase == TransferPhase.INTERRUPTED ||
        transferSnapshot.phase == TransferPhase.INIT_OUTCOME_UNKNOWN
    val downloadInProgress = pendingDownloadOffer != null ||
        downloadSnapshot.phase == DownloadPhase.INTERRUPTED ||
        downloadSnapshot.phase == DownloadPhase.READY_TO_PUBLISH ||
        downloadSnapshot.phase in DownloadSnapshot.ACTIVE_PHASES
    val showSelection = !transferActive && !blockingRecord && !downloadInProgress
    val supportedCount = selection.count { it.supported }
    val totalKnownBytes = selection.mapNotNull { it.sizeBytes }.sum()
    val canStartAnotherDownload = !transferActive &&
        !blockingRecord &&
        pendingDownloadOffer == null &&
        downloadSnapshot.phase in setOf(
            DownloadPhase.IDLE,
            DownloadPhase.COMPLETED,
            DownloadPhase.CANCELLED,
            DownloadPhase.EXPIRED,
            DownloadPhase.FAILED
        )

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .verticalScroll(rememberScrollState())
            .padding(20.dp),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Box(
            modifier = Modifier
                .size(56.dp)
                .clip(RoundedCornerShape(16.dp))
                .background(SurfaceRaised)
                .border(1.dp, Border, RoundedCornerShape(16.dp)),
            contentAlignment = Alignment.Center
        ) {
            Text(text = "✓", fontSize = 24.sp, color = Mint, fontWeight = FontWeight.Bold)
        }

        Spacer(modifier = Modifier.height(12.dp))

        Text(
            text = "Paired with KIVO",
            style = MaterialTheme.typography.headlineSmall,
            fontWeight = FontWeight.Bold,
            color = TextPrimary
        )

        Spacer(modifier = Modifier.height(16.dp))

        DestinationCard(hostAddress, fingerprintSuffix)

        Spacer(modifier = Modifier.height(16.dp))

        if (transferSnapshot.phase != TransferPhase.IDLE) {
            TransferStatusCard(
                snapshot = transferSnapshot,
                active = transferActive,
                onCancel = onCancelTransfer,
                onResume = onResumeTransfer,
                onDismiss = onDismissOutcome
            )
            Spacer(modifier = Modifier.height(16.dp))
        }

        if (downloadSnapshot.phase != DownloadPhase.IDLE) {
            DownloadStatusCard(
                snapshot = downloadSnapshot,
                active = downloadActive,
                onCancel = onCancelDownload,
                onResume = onResumeDownload,
                onChooseDestination = {
                    destinationPicker.launch(
                        CreateDownloadRequest(
                            mimeType = suggestedDownloadMime(
                                downloadSnapshot.fileType,
                                downloadSnapshot.filename
                            ),
                            filename = downloadSnapshot.filename
                        )
                    )
                },
                onDismiss = onDismissDownloadOutcome
            )
            Spacer(modifier = Modifier.height(16.dp))
        }

        if (pendingDownloadOffer != null) {
            DownloadOfferCard(
                offer = pendingDownloadOffer,
                hostAddress = hostAddress,
                fingerprintSuffix = fingerprintSuffix,
                errorMessage = downloadHandoffError,
                onConfirm = { withNotificationPermission(onConfirmDownload) },
                onDismiss = onDismissDownloadOffer
            )
            Spacer(modifier = Modifier.height(16.dp))
        } else if (downloadHandoffError != null) {
            Card(
                colors = CardDefaults.cardColors(containerColor = SurfaceSunken),
                shape = RoundedCornerShape(12.dp),
                modifier = Modifier.fillMaxWidth()
            ) {
                Text(
                    text = downloadHandoffError,
                    style = MaterialTheme.typography.bodySmall,
                    color = Danger,
                    modifier = Modifier.padding(12.dp)
                )
            }
            Spacer(modifier = Modifier.height(12.dp))
        }

        if (canStartAnotherDownload) {
            OutlinedButton(
                onClick = {
                    downloadHandoffError?.let { onDismissDownloadOffer() }
                    showDownloadScanner = true
                },
                shape = RoundedCornerShape(10.dp),
                border = ButtonDefaults.outlinedButtonBorder.copy(
                    brush = androidx.compose.ui.graphics.SolidColor(Accent)
                ),
                modifier = Modifier.fillMaxWidth()
            ) {
                Text(text = "Scan download QR from desktop", color = Accent)
            }
            Spacer(modifier = Modifier.height(12.dp))
        }

        if (showSelection) {
            SelectionCard(
                selection = selection,
                supportedCount = supportedCount,
                totalKnownBytes = totalKnownBytes,
                notificationsDenied = notificationsDenied,
                onPickFiles = { documentPicker.launch(Unit) },
                onRemoveSelected = onRemoveSelected,
                onClearSelection = onClearSelection,
                onConfirmUpload = {
                    withNotificationPermission(onConfirmUpload)
                }
            )
        }

        Spacer(modifier = Modifier.height(20.dp))

        Button(
            onClick = onDisconnect,
            colors = ButtonDefaults.buttonColors(containerColor = Danger),
            shape = RoundedCornerShape(12.dp),
            modifier = Modifier
                .fillMaxWidth()
                .height(48.dp)
        ) {
            Text(
                text = if (transferActive) "Cancel transfer and disconnect" else "Disconnect / Unpair",
                color = Color.Black,
                fontWeight = FontWeight.SemiBold
            )
        }

        Spacer(modifier = Modifier.height(12.dp))
        Text(
            text = "Transfers use pinned TLS 1.3 on the local network. " +
                "Downloads are staged app-private, verified, and saved only to the " +
                "destination you choose. Pairing keys and session tokens are never displayed or logged.",
            style = MaterialTheme.typography.bodySmall,
            color = TextMuted,
            textAlign = TextAlign.Center
        )
    }
}

@Composable
private fun DestinationCard(hostAddress: String, fingerprintSuffix: String) {
    Card(
        colors = CardDefaults.cardColors(containerColor = SurfaceSunken),
        shape = RoundedCornerShape(14.dp),
        modifier = Modifier.fillMaxWidth()
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text(text = "DESTINATION", style = MaterialTheme.typography.labelSmall, color = TextMuted)
            Text(text = hostAddress, style = MaterialTheme.typography.bodyMedium, color = TextPrimary)
            Spacer(modifier = Modifier.height(10.dp))
            Text(
                text = "CERTIFICATE FINGERPRINT",
                style = MaterialTheme.typography.labelSmall,
                color = TextMuted
            )
            Text(
                text = "…$fingerprintSuffix (Pinned TLS 1.3)",
                style = MaterialTheme.typography.bodySmall,
                color = Mint
            )
        }
    }
}

@Composable
private fun SelectionCard(
    selection: List<DocumentSummary>,
    supportedCount: Int,
    totalKnownBytes: Long,
    notificationsDenied: Boolean,
    onPickFiles: () -> Unit,
    onRemoveSelected: (String) -> Unit,
    onClearSelection: () -> Unit,
    onConfirmUpload: () -> Unit
) {
    val totalLabel = if (selection.isNotEmpty()) {
        buildString {
            append("${selection.size} selected")
            if (totalKnownBytes > 0L) append(" • ${formatByteCount(totalKnownBytes)} known total")
        }
    } else {
        ""
    }

    Card(
        colors = CardDefaults.cardColors(containerColor = SurfaceSunken),
        shape = RoundedCornerShape(14.dp),
        modifier = Modifier.fillMaxWidth()
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text(
                text = "Send files to this desktop",
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.SemiBold,
                color = TextPrimary
            )
            Spacer(modifier = Modifier.height(6.dp))
            Text(
                text = "Choose individual documents through the system picker. " +
                    "Nothing is uploaded until you review this list and confirm.",
                style = MaterialTheme.typography.bodySmall,
                color = TextSecondary
            )

            if (selection.isNotEmpty()) {
                Spacer(modifier = Modifier.height(10.dp))
                Text(text = totalLabel, style = MaterialTheme.typography.labelMedium, color = Mint)
                Spacer(modifier = Modifier.height(8.dp))
                selection.forEach { item ->
                    SelectedDocumentRow(item, onRemove = { onRemoveSelected(item.uri) })
                    Spacer(modifier = Modifier.height(8.dp))
                }
            }

            Spacer(modifier = Modifier.height(6.dp))

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                OutlinedButton(
                    onClick = onPickFiles,
                    shape = RoundedCornerShape(10.dp),
                    border = ButtonDefaults.outlinedButtonBorder.copy(
                        brush = androidx.compose.ui.graphics.SolidColor(Border)
                    ),
                    modifier = Modifier.weight(1f)
                ) {
                    Text(
                        text = if (selection.isEmpty()) "Select files" else "Add more",
                        color = TextSecondary
                    )
                }
                if (selection.isNotEmpty()) {
                    OutlinedButton(
                        onClick = onClearSelection,
                        shape = RoundedCornerShape(10.dp),
                        border = ButtonDefaults.outlinedButtonBorder.copy(
                            brush = androidx.compose.ui.graphics.SolidColor(Border)
                        )
                    ) {
                        Text(text = "Clear", color = TextSecondary)
                    }
                }
            }

            if (selection.isNotEmpty()) {
                Spacer(modifier = Modifier.height(10.dp))
                val canUpload = supportedCount == selection.size && supportedCount > 0
                Button(
                    onClick = onConfirmUpload,
                    enabled = canUpload,
                    colors = ButtonDefaults.buttonColors(containerColor = Accent),
                    shape = RoundedCornerShape(12.dp),
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(48.dp)
                ) {
                    Text(
                        text = "Upload $supportedCount file${if (supportedCount == 1) "" else "s"}",
                        color = Color.Black,
                        fontWeight = FontWeight.SemiBold
                    )
                }
                if (!canUpload) {
                    Spacer(modifier = Modifier.height(6.dp))
                    Text(
                        text = "Remove unsupported files before continuing.",
                        style = MaterialTheme.typography.bodySmall,
                        color = Warning
                    )
                }
            }

            if (notificationsDenied) {
                Spacer(modifier = Modifier.height(10.dp))
                Text(
                    text = "Notifications are disabled. Keep this screen open to monitor progress; " +
                        "the background transfer still continues.",
                    style = MaterialTheme.typography.bodySmall,
                    color = Warning
                )
            }

            Spacer(modifier = Modifier.height(12.dp))
            Text(
                text = "Quarantine: the desktop stores received bytes privately and publishes " +
                    "the file to Downloads only after you accept it there. Cancelling or " +
                    "failing never publishes anything.",
                style = MaterialTheme.typography.bodySmall,
                color = TextMuted
            )
        }
    }
}

@Composable
private fun SelectedDocumentRow(item: DocumentSummary, onRemove: () -> Unit) {
    Card(
        colors = CardDefaults.cardColors(
            containerColor = if (item.supported) SurfaceRaised else SurfaceRaised
        ),
        shape = RoundedCornerShape(10.dp),
        modifier = Modifier.fillMaxWidth()
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(12.dp),
            verticalAlignment = Alignment.Top
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = item.displayName.ifBlank { "Unsupported selection" },
                    style = MaterialTheme.typography.bodyMedium,
                    fontWeight = FontWeight.Medium,
                    color = if (item.supported) TextPrimary else Danger,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis
                )
                Spacer(modifier = Modifier.height(2.dp))
                if (item.supported) {
                    val sizeText = item.sizeBytes?.let(::formatByteCount) ?: "Size reported by provider at send time"
                    val mimeText = item.mimeType ?: "unknown type"
                    Text(
                        text = "$sizeText • $mimeText",
                        style = MaterialTheme.typography.bodySmall,
                        color = TextMuted
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = item.accessHint,
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary
                    )
                } else {
                    Text(
                        text = item.errorMessage ?: "This document cannot be uploaded",
                        style = MaterialTheme.typography.bodySmall,
                        color = Warning
                    )
                }
            }
            Spacer(modifier = Modifier.width(8.dp))
            TextButton(onClick = onRemove) {
                Text(text = "Remove", color = TextSecondary, style = MaterialTheme.typography.bodySmall)
            }
        }
    }
}

@Composable
private fun TransferStatusCard(
    snapshot: TransferSnapshot,
    active: Boolean,
    onCancel: () -> Unit,
    onResume: () -> Unit,
    onDismiss: () -> Unit
) {
    val isActivePhase = snapshot.phase in ACTIVE_PHASES
    val accent = when (snapshot.phase) {
        TransferPhase.COMMITTED -> Mint
        TransferPhase.REJECTED,
        TransferPhase.FAILED,
        TransferPhase.EXPIRED -> Danger
        TransferPhase.INTERRUPTED,
        TransferPhase.INIT_OUTCOME_UNKNOWN -> Warning
        else -> Accent
    }

    Card(
        colors = CardDefaults.cardColors(containerColor = SurfaceSunken),
        shape = RoundedCornerShape(14.dp),
        modifier = Modifier
            .fillMaxWidth()
            .border(1.dp, accent.copy(alpha = 0.45f), RoundedCornerShape(14.dp))
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    text = formatTransferPhase(snapshot.phase),
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.SemiBold,
                    color = accent,
                    modifier = Modifier.weight(1f)
                )
                if (isActivePhase && active) {
                    CircularProgressIndicator(
                        color = Accent,
                        strokeWidth = 2.dp,
                        modifier = Modifier.size(18.dp)
                    )
                }
            }

            if (snapshot.filename.isNotBlank()) {
                Spacer(modifier = Modifier.height(6.dp))
                Text(
                    text = snapshot.filename,
                    style = MaterialTheme.typography.bodyMedium,
                    color = TextPrimary,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis
                )
                Text(
                    text = if (snapshot.size > 0L) formatByteCount(snapshot.size) else "Empty file",
                    style = MaterialTheme.typography.bodySmall,
                    color = TextMuted
                )
            }

            when (snapshot.phase) {
                TransferPhase.PREPARING -> {
                    Spacer(modifier = Modifier.height(12.dp))
                    LinearProgressIndicator(
                        color = Accent,
                        trackColor = SurfaceRaised,
                        modifier = Modifier.fillMaxWidth()
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = "Validating metadata and hashing the document locally. " +
                            "Only the display name, size and SHA-256 are sent with the request; " +
                            "no file content is sent until the desktop approves it.",
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary
                    )
                }
                TransferPhase.WAITING_DESKTOP_APPROVAL -> {
                    Spacer(modifier = Modifier.height(12.dp))
                    Text(
                        text = "Open KIVO on your desktop and approve this upload. " +
                            "Only the display name, size and SHA-256 were sent with the request. " +
                            "No file content has been sent yet.",
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary
                    )
                }
                TransferPhase.RECEIVING -> {
                    Spacer(modifier = Modifier.height(12.dp))
                    LinearProgressIndicator(
                        progress = { snapshot.progressPercent / 100f },
                        color = Accent,
                        trackColor = SurfaceRaised,
                        modifier = Modifier.fillMaxWidth()
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = "${snapshot.progressPercent}% • " +
                            "${formatByteCount(snapshot.receivedBytes)} of ${formatByteCount(snapshot.size)} verified",
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = "Progress advances only from signed desktop acknowledgements.",
                        style = MaterialTheme.typography.bodySmall,
                        color = TextMuted
                    )
                }
                TransferPhase.VERIFYING -> {
                    Spacer(modifier = Modifier.height(12.dp))
                    LinearProgressIndicator(
                        progress = { 1f },
                        color = Accent,
                        trackColor = SurfaceRaised,
                        modifier = Modifier.fillMaxWidth()
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = "All chunks are committed. The desktop is verifying the whole-file SHA-256.",
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary
                    )
                }
                TransferPhase.AWAITING_FINAL_ACCEPTANCE -> {
                    Spacer(modifier = Modifier.height(12.dp))
                    LinearProgressIndicator(
                        progress = { 1f },
                        color = Accent,
                        trackColor = SurfaceRaised,
                        modifier = Modifier.fillMaxWidth()
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = "Verified. The file is still quarantined on the desktop. " +
                            "Accept it there to publish it to Downloads.",
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary
                    )
                }
                TransferPhase.COMMITTED -> {
                    Spacer(modifier = Modifier.height(10.dp))
                    Text(
                        text = snapshot.publishedFilename?.let { "Saved to Downloads as $it" }
                            ?: "Accepted by the desktop and saved to Downloads.",
                        style = MaterialTheme.typography.bodyMedium,
                        color = Mint
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = "Success is reported only after the desktop confirmed final acceptance.",
                        style = MaterialTheme.typography.bodySmall,
                        color = TextMuted
                    )
                }
                TransferPhase.INTERRUPTED -> {
                    Spacer(modifier = Modifier.height(10.dp))
                    Text(
                        text = snapshot.message.ifBlank {
                            "The upload lost contact with the desktop. Verified progress is retained."
                        },
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = "Resume verifies the desktop's committed chunk index before sending again. " +
                            "The upload is never restarted from zero automatically.",
                        style = MaterialTheme.typography.bodySmall,
                        color = TextMuted
                    )
                }
                TransferPhase.INIT_OUTCOME_UNKNOWN -> {
                    Spacer(modifier = Modifier.height(10.dp))
                    Text(
                        text = snapshot.message.ifBlank {
                            "The desktop may have received the upload request, but the response was lost."
                        },
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = "For safety the app will not retry this request automatically. " +
                            "Dismiss it and choose the file again if needed.",
                        style = MaterialTheme.typography.bodySmall,
                        color = TextMuted
                    )
                }
                TransferPhase.IDLE -> Unit
                TransferPhase.CANCELLED -> {
                    Spacer(modifier = Modifier.height(10.dp))
                    Text(
                        text = "The upload was cancelled. No new file was published. " +
                            "The desktop may briefly retain quarantined bytes until it cleans them up.",
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary
                    )
                }
                TransferPhase.REJECTED,
                TransferPhase.EXPIRED,
                TransferPhase.FAILED -> {
                    Spacer(modifier = Modifier.height(10.dp))
                    Text(
                        text = snapshot.message.ifBlank { "The upload did not complete." },
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary
                    )
                    snapshot.errorCode?.let { code ->
                        Spacer(modifier = Modifier.height(4.dp))
                        Text(
                            text = code,
                            fontFamily = FontFamily.Monospace,
                            style = MaterialTheme.typography.bodySmall,
                            color = TextMuted
                        )
                    }
                }
            }

            Spacer(modifier = Modifier.height(12.dp))

            when {
                isActivePhase && active -> {
                    OutlinedButton(
                        onClick = onCancel,
                        shape = RoundedCornerShape(10.dp),
                        border = ButtonDefaults.outlinedButtonBorder.copy(
                            brush = androidx.compose.ui.graphics.SolidColor(Danger)
                        ),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text(text = "Cancel upload", color = Danger)
                    }
                }
                snapshot.phase == TransferPhase.INTERRUPTED -> {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        Button(
                            onClick = onResume,
                            enabled = snapshot.resumable,
                            colors = ButtonDefaults.buttonColors(containerColor = Accent),
                            shape = RoundedCornerShape(10.dp),
                            modifier = Modifier.weight(1f)
                        ) {
                            Text(text = "Resume upload", color = Color.Black, fontWeight = FontWeight.SemiBold)
                        }
                        OutlinedButton(
                            onClick = onDismiss,
                            shape = RoundedCornerShape(10.dp),
                            border = ButtonDefaults.outlinedButtonBorder.copy(
                                brush = androidx.compose.ui.graphics.SolidColor(Border)
                            )
                        ) {
                            Text(text = "Discard", color = TextSecondary)
                        }
                    }
                }
                else -> {
                    OutlinedButton(
                        onClick = onDismiss,
                        shape = RoundedCornerShape(10.dp),
                        border = ButtonDefaults.outlinedButtonBorder.copy(
                            brush = androidx.compose.ui.graphics.SolidColor(Border)
                        ),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text(text = "Done", color = TextSecondary)
                    }
                }
            }
        }
    }
}

/**
 * Preflight for a scanned download handoff. It shows the desktop identity,
 * display filename, exact byte count, and required free space. The opaque
 * ticket is never rendered. Nothing is requested until the user confirms.
 */
@Composable
private fun DownloadOfferCard(
    offer: DownloadOffer,
    hostAddress: String,
    fingerprintSuffix: String,
    errorMessage: String?,
    onConfirm: () -> Unit,
    onDismiss: () -> Unit
) {
    Card(
        colors = CardDefaults.cardColors(containerColor = SurfaceSunken),
        shape = RoundedCornerShape(14.dp),
        modifier = Modifier
            .fillMaxWidth()
            .border(1.dp, Accent.copy(alpha = 0.45f), RoundedCornerShape(14.dp))
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text(
                text = "Download offered by KIVO",
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.SemiBold,
                color = Accent
            )
            Spacer(modifier = Modifier.height(6.dp))
            Text(
                text = hostAddress,
                style = MaterialTheme.typography.bodyMedium,
                color = TextPrimary
            )
            Text(
                text = "…$fingerprintSuffix (Pinned TLS 1.3)",
                style = MaterialTheme.typography.bodySmall,
                color = Mint
            )
            Spacer(modifier = Modifier.height(10.dp))
            Text(
                text = offer.filename,
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.Medium,
                color = TextPrimary,
                maxLines = 3,
                overflow = TextOverflow.Ellipsis
            )
            Text(
                text = if (offer.size > 0L) formatByteCount(offer.size) else "Empty file",
                style = MaterialTheme.typography.bodySmall,
                color = TextMuted
            )
            Spacer(modifier = Modifier.height(10.dp))
            Text(
                text = "The download is staged in app-private storage, verified against " +
                    "the desktop's signed SHA-256, and then saved only to a destination " +
                    "you choose. The app needs free space for one private copy.",
                style = MaterialTheme.typography.bodySmall,
                color = TextSecondary
            )

            if (errorMessage != null) {
                Spacer(modifier = Modifier.height(10.dp))
                Text(
                    text = errorMessage,
                    style = MaterialTheme.typography.bodySmall,
                    color = Warning
                )
            }

            Spacer(modifier = Modifier.height(12.dp))
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                Button(
                    onClick = onConfirm,
                    colors = ButtonDefaults.buttonColors(containerColor = Accent),
                    shape = RoundedCornerShape(10.dp),
                    modifier = Modifier.weight(1f)
                ) {
                    Text(
                        text = "Download",
                        color = Color.Black,
                        fontWeight = FontWeight.SemiBold
                    )
                }
                OutlinedButton(
                    onClick = onDismiss,
                    shape = RoundedCornerShape(10.dp),
                    border = ButtonDefaults.outlinedButtonBorder.copy(
                        brush = androidx.compose.ui.graphics.SolidColor(Border)
                    )
                ) {
                    Text(text = "Dismiss", color = TextSecondary)
                }
            }
        }
    }
}

/**
 * Truthful download status. Progress comes only from verified, persisted
 * chunks. Publication is never implied: `READY_TO_PUBLISH` shows an explicit
 * destination button, and `COMPLETED` appears only after the destination write
 * and flush have finished.
 */
@Composable
private fun DownloadStatusCard(
    snapshot: DownloadSnapshot,
    active: Boolean,
    onCancel: () -> Unit,
    onResume: () -> Unit,
    onChooseDestination: () -> Unit,
    onDismiss: () -> Unit
) {
    val isActivePhase = snapshot.phase in DownloadSnapshot.ACTIVE_PHASES
    val accent = when (snapshot.phase) {
        DownloadPhase.COMPLETED -> Mint
        DownloadPhase.CANCELLED,
        DownloadPhase.EXPIRED,
        DownloadPhase.FAILED -> Danger
        DownloadPhase.INTERRUPTED -> Warning
        DownloadPhase.READY_TO_PUBLISH -> Mint
        else -> Accent
    }

    Card(
        colors = CardDefaults.cardColors(containerColor = SurfaceSunken),
        shape = RoundedCornerShape(14.dp),
        modifier = Modifier
            .fillMaxWidth()
            .border(1.dp, accent.copy(alpha = 0.45f), RoundedCornerShape(14.dp))
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    text = formatDownloadPhase(snapshot.phase),
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.SemiBold,
                    color = accent,
                    modifier = Modifier.weight(1f)
                )
                if (isActivePhase && active) {
                    CircularProgressIndicator(
                        color = Accent,
                        strokeWidth = 2.dp,
                        modifier = Modifier.size(18.dp)
                    )
                }
            }

            if (snapshot.desktopLabel.isNotBlank()) {
                Spacer(modifier = Modifier.height(4.dp))
                Text(
                    text = "From ${snapshot.desktopLabel}",
                    style = MaterialTheme.typography.bodySmall,
                    color = TextMuted
                )
            }

            if (snapshot.filename.isNotBlank()) {
                Spacer(modifier = Modifier.height(6.dp))
                Text(
                    text = snapshot.filename,
                    style = MaterialTheme.typography.bodyMedium,
                    color = TextPrimary,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis
                )
                Text(
                    text = if (snapshot.size > 0L) formatByteCount(snapshot.size) else "Empty file",
                    style = MaterialTheme.typography.bodySmall,
                    color = TextMuted
                )
            }

            when (snapshot.phase) {
                DownloadPhase.PREPARING -> {
                    Spacer(modifier = Modifier.height(12.dp))
                    LinearProgressIndicator(
                        color = Accent,
                        trackColor = SurfaceRaised,
                        modifier = Modifier.fillMaxWidth()
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = snapshot.message.ifBlank {
                            "Claiming the download ticket and checking signed progress with the desktop."
                        },
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary
                    )
                }
                DownloadPhase.DOWNLOADING -> {
                    Spacer(modifier = Modifier.height(12.dp))
                    LinearProgressIndicator(
                        progress = { snapshot.progressPercent / 100f },
                        color = Accent,
                        trackColor = SurfaceRaised,
                        modifier = Modifier.fillMaxWidth()
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = "${snapshot.progressPercent}% • " +
                            "${formatByteCount(snapshot.receivedBytes)} of " +
                            "${formatByteCount(snapshot.size)} verified",
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = "Progress advances only after the signed chunk digest and " +
                            "the on-disk write both succeed.",
                        style = MaterialTheme.typography.bodySmall,
                        color = TextMuted
                    )
                }
                DownloadPhase.RECONNECTING -> {
                    Spacer(modifier = Modifier.height(12.dp))
                    LinearProgressIndicator(
                        color = Accent,
                        trackColor = SurfaceRaised,
                        modifier = Modifier.fillMaxWidth()
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = snapshot.message.ifBlank {
                            "The connection was lost. Signed progress will be reconciled before retrying."
                        },
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary
                    )
                }
                DownloadPhase.VERIFYING -> {
                    Spacer(modifier = Modifier.height(12.dp))
                    LinearProgressIndicator(
                        progress = { 1f },
                        color = Accent,
                        trackColor = SurfaceRaised,
                        modifier = Modifier.fillMaxWidth()
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = "All chunks are staged. Verifying the whole-file SHA-256 " +
                            "before asking the desktop to complete the transfer.",
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary
                    )
                }
                DownloadPhase.READY_TO_PUBLISH -> {
                    Spacer(modifier = Modifier.height(12.dp))
                    LinearProgressIndicator(
                        progress = { 1f },
                        color = Mint,
                        trackColor = SurfaceRaised,
                        modifier = Modifier.fillMaxWidth()
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = snapshot.message.ifBlank {
                            "Download complete and verified. Choose where to save it."
                        },
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary
                    )
                }
                DownloadPhase.PUBLISHING -> {
                    Spacer(modifier = Modifier.height(12.dp))
                    LinearProgressIndicator(
                        color = Accent,
                        trackColor = SurfaceRaised,
                        modifier = Modifier.fillMaxWidth()
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = "Writing the verified copy to the destination you chose.",
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary
                    )
                }
                DownloadPhase.COMPLETED -> {
                    Spacer(modifier = Modifier.height(10.dp))
                    Text(
                        text = snapshot.publishedName?.let { "Saved as $it" }
                            ?: "Saved to the selected destination.",
                        style = MaterialTheme.typography.bodyMedium,
                        color = Mint
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = when (snapshot.destinationVerified) {
                            true -> "The saved file was reopened and matched the signed SHA-256."
                            false -> "The destination rejected a verification read. " +
                                "The write completed and was flushed, but the saved copy " +
                                "could not be re-hashed."
                            null -> "Success is reported only after the destination write and flush finished."
                        },
                        style = MaterialTheme.typography.bodySmall,
                        color = TextMuted
                    )
                }
                DownloadPhase.INTERRUPTED -> {
                    Spacer(modifier = Modifier.height(10.dp))
                    Text(
                        text = snapshot.message.ifBlank {
                            "The download lost contact with the desktop. Verified progress is retained."
                        },
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = "Resume reconciles the desktop's signed chunk index before " +
                            "requesting more bytes. The download is never restarted from zero " +
                            "automatically and is never published without your action.",
                        style = MaterialTheme.typography.bodySmall,
                        color = TextMuted
                    )
                }
                DownloadPhase.CANCELLED -> {
                    Spacer(modifier = Modifier.height(10.dp))
                    Text(
                        text = "The download was cancelled. The app-private staging copy " +
                            "was removed and no destination file was left behind.",
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary
                    )
                }
                DownloadPhase.EXPIRED,
                DownloadPhase.FAILED -> {
                    Spacer(modifier = Modifier.height(10.dp))
                    Text(
                        text = snapshot.message.ifBlank { "The download did not complete." },
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary
                    )
                    snapshot.errorCode?.let { code ->
                        Spacer(modifier = Modifier.height(4.dp))
                        Text(
                            text = code,
                            fontFamily = FontFamily.Monospace,
                            style = MaterialTheme.typography.bodySmall,
                            color = TextMuted
                        )
                    }
                }
                DownloadPhase.IDLE,
                DownloadPhase.OFFER -> Unit
            }

            Spacer(modifier = Modifier.height(12.dp))

            when {
                isActivePhase && active -> {
                    OutlinedButton(
                        onClick = onCancel,
                        shape = RoundedCornerShape(10.dp),
                        border = ButtonDefaults.outlinedButtonBorder.copy(
                            brush = androidx.compose.ui.graphics.SolidColor(Danger)
                        ),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text(text = "Cancel download", color = Danger)
                    }
                }
                snapshot.phase == DownloadPhase.READY_TO_PUBLISH -> {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        Button(
                            onClick = onChooseDestination,
                            colors = ButtonDefaults.buttonColors(containerColor = Accent),
                            shape = RoundedCornerShape(10.dp),
                            modifier = Modifier.weight(1f)
                        ) {
                            Text(
                                text = "Choose destination",
                                color = Color.Black,
                                fontWeight = FontWeight.SemiBold
                            )
                        }
                        OutlinedButton(
                            onClick = onCancel,
                            shape = RoundedCornerShape(10.dp),
                            border = ButtonDefaults.outlinedButtonBorder.copy(
                                brush = androidx.compose.ui.graphics.SolidColor(Border)
                            )
                        ) {
                            Text(text = "Discard", color = TextSecondary)
                        }
                    }
                    if (snapshot.message.isNotBlank() && snapshot.errorCode != null) {
                        Spacer(modifier = Modifier.height(6.dp))
                        Text(
                            text = snapshot.message,
                            style = MaterialTheme.typography.bodySmall,
                            color = Warning
                        )
                    }
                }
                snapshot.phase == DownloadPhase.INTERRUPTED -> {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        Button(
                            onClick = onResume,
                            enabled = snapshot.resumable,
                            colors = ButtonDefaults.buttonColors(containerColor = Accent),
                            shape = RoundedCornerShape(10.dp),
                            modifier = Modifier.weight(1f)
                        ) {
                            Text(
                                text = "Resume download",
                                color = Color.Black,
                                fontWeight = FontWeight.SemiBold
                            )
                        }
                        OutlinedButton(
                            onClick = onCancel,
                            shape = RoundedCornerShape(10.dp),
                            border = ButtonDefaults.outlinedButtonBorder.copy(
                                brush = androidx.compose.ui.graphics.SolidColor(Border)
                            )
                        ) {
                            Text(text = "Discard", color = TextSecondary)
                        }
                    }
                }
                else -> {
                    OutlinedButton(
                        onClick = onDismiss,
                        shape = RoundedCornerShape(10.dp),
                        border = ButtonDefaults.outlinedButtonBorder.copy(
                            brush = androidx.compose.ui.graphics.SolidColor(Border)
                        ),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text(text = "Done", color = TextSecondary)
                    }
                }
            }
        }
    }
}
