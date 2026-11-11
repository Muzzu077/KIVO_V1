package com.kivo.companion.transfer

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/**
 * Activity-facing facade for the upload runtime. Selection is held here so
 * rotation does not lose the user's chosen documents; the upload itself is
 * owned by the process-wide runtime and survives activity recreation.
 */
class TransferViewModel(
    private val runtime: TransferRuntime
) : ViewModel() {

    val snapshot: StateFlow<TransferSnapshot> = runtime.snapshot
    val downloadSnapshot: StateFlow<DownloadSnapshot> = runtime.downloadSnapshot
    val operation: StateFlow<TransferOperation> = runtime.operation
    val active: StateFlow<Boolean> = runtime.active

    private val _selection = MutableStateFlow<List<DocumentSummary>>(emptyList())
    val selection: StateFlow<List<DocumentSummary>> = _selection.asStateFlow()

    private val _pendingDownloadOffer = MutableStateFlow<DownloadOffer?>(null)
    val pendingDownloadOffer: StateFlow<DownloadOffer?> = _pendingDownloadOffer.asStateFlow()

    private val _downloadHandoffError = MutableStateFlow<String?>(null)
    val downloadHandoffError: StateFlow<String?> = _downloadHandoffError.asStateFlow()

    private val selectionMutex = Mutex()

    fun addSelectedUris(uris: List<String>) {
        val cleaned = uris
            .map { it.trim() }
            .filter { it.startsWith("content://") }
            .distinct()
        if (cleaned.isEmpty()) return

        viewModelScope.launch(Dispatchers.IO) {
            selectionMutex.withLock {
                val summaries = mutableListOf<DocumentSummary>()
                for (uri in cleaned) {
                    if (_selection.value.any { it.uri == uri }) continue
                    summaries += runtime.describe(uri)
                }
                if (summaries.isNotEmpty()) {
                    _selection.update { current ->
                        current + summaries.filter { candidate ->
                            current.none { it.uri == candidate.uri }
                        }
                    }
                }
            }
        }
    }

    fun removeSelected(uri: String) {
        _selection.update { current -> current.filterNot { it.uri == uri } }
    }

    fun clearSelection() {
        _selection.value = emptyList()
    }

    fun confirmUpload() {
        val chosen = _selection.value.filter { it.supported }.map { it.uri }
        if (chosen.isEmpty()) return
        if (runtime.startUploads(chosen)) {
            _selection.value = emptyList()
        }
    }

    fun cancelTransfer() {
        runtime.cancel()
    }

    fun resumeTransfer() {
        runtime.resume()
    }

    fun dismissOutcome() {
        when (runtime.snapshot.value.phase) {
            TransferPhase.INTERRUPTED -> runtime.cancel()
            TransferPhase.INIT_OUTCOME_UNKNOWN -> runtime.dismiss()
            TransferPhase.COMMITTED,
            TransferPhase.REJECTED,
            TransferPhase.CANCELLED,
            TransferPhase.EXPIRED,
            TransferPhase.FAILED -> runtime.acknowledgeOutcome()
            else -> Unit
        }
    }

    fun onPaired(sessionId: String) {
        runtime.reconcileSession(sessionId)
    }

    fun onUnpaired() {
        _pendingDownloadOffer.value = null
        _downloadHandoffError.value = null
        runtime.reconcileWithoutSession()
    }

    // ── Download handoff and operations ────────────────────────────────────

    /**
     * Parses a scanned `kivo-download://v1` payload and holds it for explicit
     * user confirmation. The ticket is never listed or browsed, and scanning
     * alone sends no request.
     */
    fun acceptDownloadHandoff(raw: String): Boolean {
        val deviceId = runtime.localDeviceId()
        if (deviceId == null) {
            _downloadHandoffError.value = "No paired session is available."
            return false
        }
        return try {
            val offer = DownloadTicketParser.parse(raw, deviceId)
            val existing = runtime.downloadSnapshot.value
            if (existing.phase in DownloadSnapshot.ACTIVE_PHASES ||
                existing.phase == DownloadPhase.READY_TO_PUBLISH
            ) {
                _downloadHandoffError.value = "Finish or cancel the current download first."
                return false
            }
            _pendingDownloadOffer.value = offer
            _downloadHandoffError.value = null
            true
        } catch (e: DownloadTicketException) {
            _downloadHandoffError.value = "${e.code}: ${e.message}"
            false
        } catch (e: Exception) {
            _downloadHandoffError.value = "The download QR code is not supported."
            false
        }
    }

    fun confirmDownload() {
        val offer = _pendingDownloadOffer.value ?: return
        if (runtime.startDownload(offer)) {
            _pendingDownloadOffer.value = null
        }
    }

    fun dismissDownloadOffer() {
        _pendingDownloadOffer.value = null
    }

    fun clearDownloadHandoffError() {
        _downloadHandoffError.value = null
    }

    fun resumeDownload() {
        runtime.resumeDownload()
    }

    fun cancelDownload() {
        if (runtime.operation.value == TransferOperation.DOWNLOAD && runtime.active.value) {
            runtime.requestCancel()
        } else {
            runtime.cancelDownloadNow()
        }
    }

    fun publishDownload(uri: String) {
        runtime.publishDownload(uri)
    }

    fun dismissDownloadOutcome() {
        when (runtime.downloadSnapshot.value.phase) {
            DownloadPhase.INTERRUPTED -> runtime.cancelDownloadNow()
            DownloadPhase.COMPLETED,
            DownloadPhase.CANCELLED,
            DownloadPhase.EXPIRED,
            DownloadPhase.FAILED -> runtime.acknowledgeDownloadOutcome()
            else -> Unit
        }
    }

    /**
     * Cancels and cleans any transfer before pairing material is removed, then
     * lets the caller perform the existing disconnect/unpair flow.
     */
    fun disconnectThen(onDisconnected: () -> Unit) {
        viewModelScope.launch {
            runtime.shutdownForUnpair()
            onDisconnected()
        }
    }
}
