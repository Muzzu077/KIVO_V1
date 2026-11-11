package com.kivo.companion.transfer

import android.content.Context
import com.kivo.companion.model.SessionCredentials
import com.kivo.companion.storage.KeystoreSecretStorage
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import java.io.File

/** Indirection that keeps lifecycle logic testable without an Android service. */
internal interface TransferServiceController {
    fun start()
    fun stop()
}

private class AndroidTransferServiceController(
    private val context: Context
) : TransferServiceController {
    override fun start() {
        try {
            TransferService.start(context)
        } catch (e: Exception) {
            // The in-app screen still shows progress and cancellation. A failed
            // foreground start only means the process is not protected from
            // background eviction; the durable record supports a later resume.
        }
    }

    override fun stop() {
        try {
            TransferService.stop(context)
        } catch (e: Exception) {
            // Nothing to release.
        }
    }
}

/**
 * Process-wide owner of the upload and download engines plus their durable
 * recovery records. Exactly one operation may run at a time because uploads and
 * downloads share one durable sequence counter and one desktop replay window.
 * The runtime never starts a transfer on its own: a caller must have obtained
 * explicit user confirmation.
 */
class TransferRuntime internal constructor(
    private val storage: KeystoreSecretStorage,
    private val resolver: UploadSourceResolver,
    private val preparer: UploadPreparer,
    transportFactory: TransferTransportFactory,
    private val service: TransferServiceController,
    dispatcher: CoroutineDispatcher,
    private val unpairJoinTimeoutMs: Long = 15_000L,
    private val unpairCancelTimeoutMs: Long = 10_000L,
    private val downloadStager: DownloadStager? = null,
    private val destinationResolver: DownloadDestinationResolver? = null,
    publisher: DownloadPublisher = DownloadPublisher()
) {
    constructor(context: Context) : this(
        storage = KeystoreSecretStorage(context.applicationContext),
        resolver = AndroidUploadSourceResolver(context.applicationContext),
        preparer = UploadPreparer(File(context.applicationContext.cacheDir, "kivo-transfer")),
        transportFactory = OkHttpTransferTransportFactory(),
        service = AndroidTransferServiceController(context.applicationContext),
        dispatcher = Dispatchers.IO,
        downloadStager = DownloadStager(
            File(context.applicationContext.filesDir, "kivo-downloads")
        ),
        destinationResolver = AndroidDownloadDestinationResolver(context.applicationContext),
        publisher = DownloadPublisher()
    )

    companion object {
        @Volatile
        private var instance: TransferRuntime? = null

        fun get(context: Context): TransferRuntime =
            instance ?: synchronized(this) {
                instance ?: TransferRuntime(context.applicationContext).also { instance = it }
            }
    }

    private val scope = CoroutineScope(SupervisorJob() + dispatcher)
    private val queueStore = TransferQueueStore(storage)
    private val downloadRecoveryStore = DownloadRecoveryStore(storage)
    private val requestGate = TransferRequestGate()

    private val _snapshot = MutableStateFlow(TransferSnapshot())
    val snapshot: StateFlow<TransferSnapshot> = _snapshot.asStateFlow()

    private val _downloadSnapshot = MutableStateFlow(DownloadSnapshot())
    val downloadSnapshot: StateFlow<DownloadSnapshot> = _downloadSnapshot.asStateFlow()

    private val _operation = MutableStateFlow(TransferOperation.NONE)
    val operation: StateFlow<TransferOperation> = _operation.asStateFlow()

    private val _active = MutableStateFlow(false)
    val active: StateFlow<Boolean> = _active.asStateFlow()

    private val coordinator = UploadCoordinator(
        storage = storage,
        resolver = resolver,
        preparer = preparer,
        transportFactory = transportFactory,
        onSnapshot = { snapshot -> _snapshot.value = snapshot },
        credentialsLoader = { storage.loadCredentials() },
        sequenceManager = TransferSequenceManager(storage),
        queueStore = queueStore,
        requestGate = requestGate
    )

    private val downloadCoordinator: DownloadCoordinator? =
        if (downloadStager != null && destinationResolver != null) {
            DownloadCoordinator(
                storage = storage,
                stager = downloadStager,
                destinationResolver = destinationResolver,
                publisher = publisher,
                transportFactory = transportFactory,
                onSnapshot = { snapshot -> _downloadSnapshot.value = snapshot },
                credentialsLoader = { storage.loadCredentials() },
                sequenceManager = TransferSequenceManager(storage),
                recoveryStore = downloadRecoveryStore,
                requestGate = requestGate
            )
        } else {
            null
        }

    private val lock = Any()
    private var operationJob: Job? = null

    init {
        restorePersistedState()
    }

    // ── Upload selection metadata ──────────────────────────────────────────

    /** Local paired device identifier used to enforce the QR handoff binding. */
    fun localDeviceId(): String? = try {
        storage.loadCredentials()?.deviceId
    } catch (e: Exception) {
        null
    }

    /**
     * Resolves display metadata without reading file contents. Taking a
     * persistable grant here is intentional: it is what allows a later process
     * to reopen the source instead of forcing a full private copy.
     */
    fun describe(uri: String): DocumentSummary {
        return try {
            val source = resolver.resolve(uri)
            DocumentSummaryFactory.from(source.descriptor)
        } catch (e: TransferValidationException) {
            DocumentSummary.rejected(uri, e.code, e.message ?: "The selected document is not supported")
        } catch (e: TransferException) {
            DocumentSummary.rejected(uri, e.code, e.message ?: "The selected document is unavailable")
        } catch (e: SecurityException) {
            DocumentSummary.rejected(
                uri,
                "permission_denied",
                "The document provider did not grant read access"
            )
        } catch (e: Exception) {
            DocumentSummary.rejected(
                uri,
                "source_unavailable",
                e.message ?: "The selected document could not be inspected"
            )
        }
    }

    // ── User-confirmed upload operations ───────────────────────────────────

    /**
     * Uploads a confirmed batch sequentially. Selection alone never calls this.
     * Returns false when another transfer operation is already active.
     */
    fun startUploads(uris: List<String>): Boolean {
        val cleaned = uris
            .map { it.trim() }
            .filter { it.startsWith("content://") }
            .distinct()
        if (cleaned.isEmpty()) return false
        if (pendingDownload() != null) {
            _snapshot.value = TransferSnapshot(
                phase = TransferPhase.FAILED,
                errorCode = "download_pending",
                message = "Finish or cancel the pending download before uploading."
            )
            return false
        }
        if (!beginOperation(TransferOperation.UPLOAD)) return false

        _snapshot.value = TransferSnapshot(
            phase = TransferPhase.PREPARING,
            message = "Validating and hashing the selected document"
        )
        service.start()
        launchUpload { coordinator.startUploads(cleaned) }
        return true
    }

    /** Resumes the durable interrupted upload after explicit user action. */
    fun resume() {
        val record = queueStore.load()
        if (record == null) {
            if (!_active.value) _snapshot.value = TransferSnapshot()
            return
        }
        if (!beginOperation(TransferOperation.UPLOAD)) return

        _snapshot.value = TransferSnapshot(
            phase = TransferPhase.PREPARING,
            transferId = record.transferId.takeIf { it.isNotEmpty() },
            filename = record.filename,
            size = record.size,
            receivedBytes = record.receivedBytes,
            state = record.state,
            message = "Resuming upload and checking its status with the desktop"
        )
        service.start()
        launchUpload { coordinator.resume() }
    }

    // ── User-confirmed download operations ─────────────────────────────────

    /**
     * Claims and downloads a ticket from an explicit QR handoff. The offer card
     * has already shown desktop identity, filename, size, and storage needs.
     */
    fun startDownload(offer: DownloadOffer): Boolean {
        if (downloadCoordinator == null) return false
        if (queueStore.load() != null) {
            _downloadSnapshot.value = DownloadSnapshot(
                phase = DownloadPhase.FAILED,
                filename = offer.filename,
                size = offer.size,
                errorCode = "upload_pending",
                message = "Finish or cancel the pending upload before downloading."
            )
            return false
        }
        if (!beginOperation(TransferOperation.DOWNLOAD)) return false
        service.start()
        launchDownload { downloadCoordinator.start(offer) }
        return true
    }

    /** Resumes the durable interrupted download after explicit user action. */
    fun resumeDownload(): Boolean {
        if (downloadCoordinator == null) return false
        if (downloadRecoveryStore.load() == null) {
            if (!_active.value) _downloadSnapshot.value = DownloadSnapshot()
            return false
        }
        if (!beginOperation(TransferOperation.DOWNLOAD)) return false
        service.start()
        launchDownload { downloadCoordinator.resume() }
        return true
    }

    /**
     * Publishes the verified staged copy to a SAF destination. This is a local
     * copy, not a connected-device transfer, so it does not start the
     * foreground service; the picker result is used immediately.
     */
    fun publishDownload(uri: String): Boolean {
        if (downloadCoordinator == null) return false
        if (!beginOperation(TransferOperation.DOWNLOAD)) return false
        launchDownload { downloadCoordinator.publish(uri) }
        return true
    }

    /** Cancels an interrupted download while no operation coroutine is running. */
    fun cancelDownloadNow(): Boolean {
        if (downloadCoordinator == null) return false
        if (!beginOperation(TransferOperation.DOWNLOAD)) return false
        service.start()
        launchDownload { downloadCoordinator.cancelNow() }
        return true
    }

    fun dismissDownload(): Boolean {
        if (downloadCoordinator == null) return false
        if (!beginOperation(TransferOperation.DOWNLOAD)) return false
        launchDownload { downloadCoordinator.dismiss() }
        return true
    }

    /** Hides a terminal download result after its durable record is gone. */
    fun acknowledgeDownloadOutcome() {
        if (_active.value) return
        if (downloadRecoveryStore.load() != null) return
        _downloadSnapshot.value = DownloadSnapshot()
    }

    // ── Cancellation ───────────────────────────────────────────────────────

    /**
     * Cancels whichever operation is running. Download cancellation removes the
     * app-private staging copy and sends a best-effort signed cancel; an
     * already-completed desktop ticket simply returns its terminal state.
     */
    fun requestCancel() {
        when (_operation.value) {
            TransferOperation.DOWNLOAD -> downloadCoordinator?.requestCancel()
            else -> coordinator.requestCancel()
        }
    }

    fun cancel() {
        if (_operation.value == TransferOperation.DOWNLOAD) {
            if (_active.value) {
                downloadCoordinator?.requestCancel()
            } else {
                cancelDownloadNow()
            }
            return
        }
        if (_active.value) {
            coordinator.requestCancel()
            return
        }
        val record = queueStore.load() ?: run {
            _snapshot.value = TransferSnapshot()
            return
        }
        if (!beginOperation(TransferOperation.UPLOAD)) return
        if (!record.initUncertain && record.transferId.isNotEmpty()) {
            service.start()
        }
        launchUpload { coordinator.cancelNow() }
    }

    /** Dismisses a local upload outcome-unknown record and its staging copy. */
    fun dismiss() {
        if (_active.value) return
        if (queueStore.load() == null) {
            _snapshot.value = TransferSnapshot()
            return
        }
        if (!beginOperation(TransferOperation.UPLOAD)) return
        launchUpload { coordinator.dismiss() }
    }

    /** Hides a terminal upload outcome after its durable record has been removed. */
    fun acknowledgeOutcome() {
        if (_active.value) return
        if (queueStore.load() != null) return
        _snapshot.value = TransferSnapshot()
    }

    // ── Pairing reconciliation ─────────────────────────────────────────────

    /**
     * Removes any transfer state that belongs to a different pairing, or that
     * cannot be resumed because no live credentials remain. Called when the
     * pairing screen settles on a session.
     */
    fun reconcileSession(sessionId: String) {
        if (_active.value) return
        reconcileUploadSession(sessionId)
        reconcileDownloadSession(sessionId)
    }

    /** Drops stale records once the app is known to have no paired session. */
    fun reconcileWithoutSession() {
        if (_active.value) return
        val credentials = try {
            storage.loadCredentials()
        } catch (e: Exception) {
            null
        }
        if (credentials != null) return
        val upload = queueStore.load()
        if (upload != null) {
            deleteSpool(upload)
            queueStore.clear()
            preparer.cleanupOrphanSpools(null)
            _snapshot.value = TransferSnapshot()
        }
        val download = downloadRecoveryStore.load()
        if (download != null) {
            deleteDownloadStage(download)
            downloadRecoveryStore.clear()
            downloadStager?.cleanupOrphans(null)
            _downloadSnapshot.value = DownloadSnapshot()
        }
    }

    /**
     * Stops any active operation before the caller clears pairing credentials.
     * This keeps signed cancels authenticated and makes sure no staging copy
     * outlives the pairing.
     */
    suspend fun shutdownForUnpair() {
        coordinator.requestCancel()
        downloadCoordinator?.requestCancel()
        val job = synchronized(lock) { operationJob }
        if (job != null) {
            val joined = withTimeoutOrNull(unpairJoinTimeoutMs) { job.join() }
            if (joined == null) {
                job.cancel()
                withTimeoutOrNull(5_000L) { job.join() }
            }
        }
        if (queueStore.load() != null) {
            withTimeoutOrNull(unpairCancelTimeoutMs) { coordinator.cancelNow() }
        }
        val leftoverUpload = queueStore.load()
        if (leftoverUpload != null) {
            // Last-resort local cleanup. The desktop expires the transfer or
            // deletes it when its session is revoked.
            deleteSpool(leftoverUpload)
            queueStore.clear()
        }
        val leftoverDownload = downloadRecoveryStore.load()
        if (leftoverDownload != null && downloadCoordinator != null) {
            withTimeoutOrNull(unpairCancelTimeoutMs) { downloadCoordinator.cancelNow() }
        }
        val remainingDownload = downloadRecoveryStore.load()
        if (remainingDownload != null) {
            deleteDownloadStage(remainingDownload)
            downloadRecoveryStore.clear()
        }
        downloadStager?.cleanupOrphans(null)
        _snapshot.value = TransferSnapshot()
        _downloadSnapshot.value = DownloadSnapshot()
    }

    // ── Operation lifecycle ────────────────────────────────────────────────

    private fun beginOperation(operation: TransferOperation): Boolean {
        synchronized(lock) {
            if (_active.value) return false
            _active.value = true
            _operation.value = operation
        }
        return true
    }

    private fun launchUpload(block: suspend () -> Unit) {
        launchOperation(TransferOperation.UPLOAD, block)
    }

    private fun launchDownload(block: suspend () -> Unit) {
        launchOperation(TransferOperation.DOWNLOAD, block)
    }

    private fun launchOperation(operation: TransferOperation, block: suspend () -> Unit) {
        val job = scope.launch(start = CoroutineStart.LAZY) {
            try {
                block()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                if (operation == TransferOperation.DOWNLOAD) {
                    _downloadSnapshot.value = DownloadSnapshot(
                        phase = DownloadPhase.FAILED,
                        errorCode = "download_failed",
                        message = e.message ?: "The download stopped unexpectedly"
                    )
                } else {
                    _snapshot.value = TransferSnapshot(
                        phase = TransferPhase.FAILED,
                        errorCode = "upload_failed",
                        message = e.message ?: "The upload stopped unexpectedly"
                    )
                }
            } finally {
                finishOperation()
            }
        }
        synchronized(lock) { operationJob = job }
        job.start()
    }

    private fun finishOperation() {
        synchronized(lock) {
            operationJob = null
            _operation.value = TransferOperation.NONE
        }
        _active.value = false
        service.stop()
    }

    // ── Restore and cleanup ────────────────────────────────────────────────

    private fun restorePersistedState() {
        val credentials = try {
            storage.loadCredentials()
        } catch (e: Exception) {
            null
        }
        val upload = try {
            queueStore.load()
        } catch (e: Exception) {
            null
        }
        if (upload != null) {
            // Exactly one operation is legal. A download record cannot have
            // progressed while an upload record was durable, so discard any
            // stale download staging rather than offering two resumes.
            discardDownloadState()
            if (credentials == null || credentials.sessionId != upload.sessionId) {
                deleteSpool(upload)
                queueStore.clear()
                preparer.cleanupOrphanSpools(null)
                return
            }
            preparer.cleanupOrphanSpools(upload.spoolName)
            _snapshot.value = restoredSnapshot(upload)
            return
        }
        preparer.cleanupOrphanSpools(null)
        restoreDownloadState(credentials)
    }

    private fun restoreDownloadState(credentials: SessionCredentials?) {
        if (downloadCoordinator == null || downloadStager == null) return
        val record = try {
            downloadRecoveryStore.load()
        } catch (e: Exception) {
            null
        }
        if (record == null) {
            downloadStager.cleanupOrphans(null)
            return
        }
        downloadStager.cleanupOrphans(record.stageName)
        if (credentials == null || credentials.sessionId != record.sessionId) {
            deleteDownloadStage(record)
            downloadRecoveryStore.clear()
            downloadStager.cleanupOrphans(null)
            return
        }
        _downloadSnapshot.value = downloadCoordinator.restoredSnapshot() ?: DownloadSnapshot()
    }

    private fun reconcileUploadSession(sessionId: String) {
        val record = queueStore.load() ?: return
        if (record.sessionId == sessionId) {
            if (_snapshot.value.phase == TransferPhase.IDLE) {
                _snapshot.value = restoredSnapshot(record)
            }
            return
        }
        deleteSpool(record)
        queueStore.clear()
        preparer.cleanupOrphanSpools(null)
        _snapshot.value = TransferSnapshot()
    }

    private fun reconcileDownloadSession(sessionId: String) {
        val coordinator = downloadCoordinator ?: return
        val stager = downloadStager ?: return
        val record = downloadRecoveryStore.load() ?: run {
            stager.cleanupOrphans(null)
            return
        }
        if (record.sessionId == sessionId) {
            stager.cleanupOrphans(record.stageName)
            if (_downloadSnapshot.value.phase == DownloadPhase.IDLE) {
                _downloadSnapshot.value = coordinator.restoredSnapshot() ?: DownloadSnapshot()
            }
            return
        }
        deleteDownloadStage(record)
        downloadRecoveryStore.clear()
        stager.cleanupOrphans(null)
        _downloadSnapshot.value = DownloadSnapshot()
    }

    private fun pendingDownload(): PersistedDownload? = try {
        downloadRecoveryStore.load()
    } catch (e: Exception) {
        null
    }

    private fun restoredSnapshot(record: PersistedTransfer): TransferSnapshot {
        if (record.initUncertain) {
            return TransferSnapshot(
                phase = TransferPhase.INIT_OUTCOME_UNKNOWN,
                filename = record.filename,
                size = record.size,
                receivedBytes = 0L,
                state = record.state,
                errorCode = "init_outcome_unknown",
                message = "An earlier upload request may or may not have reached the desktop. " +
                    "It will not be retried automatically. Dismiss it and select the file again.",
                resumable = false
            )
        }
        return TransferSnapshot(
            phase = TransferPhase.INTERRUPTED,
            transferId = record.transferId.takeIf { it.isNotEmpty() },
            filename = record.filename,
            size = record.size,
            receivedBytes = record.receivedBytes,
            state = record.state,
            publishedFilename = record.publishedFilename,
            errorCode = record.errorCode,
            message = "An upload was interrupted. Resume to reconcile committed progress " +
                "and continue from the first uncommitted chunk.",
            resumable = true
        )
    }

    private fun deleteSpool(record: PersistedTransfer) {
        record.spoolName?.let { name ->
            try {
                preparer.spoolFileByName(name)?.delete()
            } catch (e: Exception) {
                // Best effort; encrypted state is still discarded.
            }
        }
    }

    private fun deleteDownloadStage(record: PersistedDownload) {
        try {
            downloadStager?.deleteByName(record.stageName)
        } catch (e: Exception) {
            // Best effort; the encrypted record is discarded.
        }
    }

    private fun discardDownloadState() {
        val record = try {
            downloadRecoveryStore.load()
        } catch (e: Exception) {
            null
        }
        if (record != null) deleteDownloadStage(record)
        downloadRecoveryStore.clear()
        downloadStager?.cleanupOrphans(null)
        _downloadSnapshot.value = DownloadSnapshot()
    }
}
