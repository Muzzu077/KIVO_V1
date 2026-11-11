package com.kivo.companion.transfer

import com.kivo.companion.model.SessionCredentials
import com.kivo.companion.storage.KeystoreSecretStorage
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import org.json.JSONObject
import java.io.Closeable
import java.io.IOException
import java.io.InputStream
import java.security.SecureRandom
import kotlin.math.ceil
import kotlin.math.min

/**
 * Serialized upload engine.
 *
 * One authenticated request is in flight per session. Every request reserves a
 * durable sequence before transmission, verifies its signed response, and only
 * then updates user-visible progress. A transfer is reported as successful only
 * after a signed `committed` status, which is Gate B on the desktop.
 */
class UploadCoordinator(
    private val storage: KeystoreSecretStorage,
    private val resolver: UploadSourceResolver,
    private val preparer: UploadPreparer,
    private val transportFactory: TransferTransportFactory,
    private val onSnapshot: (TransferSnapshot) -> Unit,
    private val credentialsLoader: () -> SessionCredentials? = { storage.loadCredentials() },
    private val sequenceManager: TransferSequenceManager = TransferSequenceManager(storage),
    private val queueStore: TransferQueueStore = TransferQueueStore(storage),
    private val nowSeconds: () -> Long = { System.currentTimeMillis() / 1000L },
    private val clockMillis: () -> Long = { System.currentTimeMillis() },
    private val delayFn: suspend (Long) -> Unit = { delay(it) },
    private val pollIntervalMs: Long = TransferProtocolLimits.STATUS_POLL_INTERVAL_MS,
    private val maxConsecutiveNetworkFailures: Int = TransferProtocolLimits.MAX_NETWORK_RETRIES,
    private val secureRandom: SecureRandom = SecureRandom(),
    private val requestGate: TransferRequestGate? = null
) {
    companion object {
        private val TRANSFER_ID_PATTERN = Regex("^tr_[0-9a-f]{24}$")

        private val KNOWN_STATES = setOf(
            "created",
            "awaiting_desktop_approval",
            "receiving",
            "verifying",
            "awaiting_final_acceptance",
            "committed",
            "rejected",
            "cancelled",
            "expired",
            "failed",
            "discarded"
        )

        private val TERMINAL_STATES = setOf(
            "committed", "rejected", "cancelled", "expired", "failed", "discarded"
        )

        private const val BACKOFF_BASE_MS = 500L
        private const val BACKOFF_MAX_MS = 8_000L
    }

    @Volatile
    private var cancelRequested = false

    private val activeLock = Any()
    private var activeClient: TransferHttpClient? = null
    private var activePrepared: PreparedUpload? = null

    // ── Public lifecycle ───────────────────────────────────────────────────

    /**
     * Uploads each selected document sequentially. The caller must have already
     * shown metadata, destination identity, and obtained explicit confirmation.
     * Selection never starts an upload by itself.
     */
    suspend fun startUploads(uris: List<String>) {
        cancelRequested = false
        if (uris.isEmpty()) return

        val pending = queueStore.load()
        if (pending != null) {
            onSnapshot(snapshotFor(pending, "Finish or dismiss the interrupted upload first"))
            return
        }

        for (uri in uris) {
            if (cancelRequested) break
            val outcome = uploadOne(uri)
            if (outcome != FlowOutcome.COMPLETED) break
        }
    }

    /** Resumes the durable interrupted transfer, if one exists. Never starts a new transfer. */
    suspend fun resume() {
        cancelRequested = false
        val record = queueStore.load()
        if (record == null) {
            onSnapshot(TransferSnapshot(phase = TransferPhase.IDLE))
            return
        }
        if (record.initUncertain) {
            emitInitUnknown(record)
            return
        }
        if (record.state !in KNOWN_STATES) {
            failLocal(record.copy(errorCode = "invalid_state"), "invalid_transfer_state", "Stored transfer state is invalid")
            return
        }

        val credentials = credentialsLoader()
        if (credentials == null || credentials.sessionId != record.sessionId ||
            credentials.isExpired(nowSeconds()) || !credentials.hasEndpoint()
        ) {
            failLocal(record, "session_expired", "The paired session is no longer valid. Select the files again after pairing.")
            return
        }

        if (record.transferId.isEmpty() || !TRANSFER_ID_PATTERN.matches(record.transferId)) {
            failLocal(record, "invalid_transfer_id", "The stored transfer identity is invalid")
            return
        }

        val prepared = try {
            val source = resolver.resolve(record.uri)
            preparer.restore(
                source = source,
                filename = record.filename,
                mimeType = record.mimeType,
                size = record.size,
                sha256Hex = record.sha256Hex,
                strategy = record.strategy,
                spoolName = record.spoolName
            )
        } catch (e: TransferException) {
            failLocal(record, e.code, e.message ?: "The selected document is no longer available")
            return
        } catch (e: Exception) {
            failLocal(record, "source_unavailable", e.message ?: "The selected document is no longer available")
            return
        }

        onSnapshot(snapshotFor(record, "Checking transfer status with the desktop"))
        val client = try {
            createClient(credentials)
        } catch (e: Exception) {
            prepared.cleanup()
            failLocal(record, "transport_unavailable", e.message ?: "Could not create a transfer connection")
            return
        }
        synchronized(activeLock) {
            activeClient = client
            activePrepared = prepared
        }

        try {
            when (val status = queryStatus(client, record)) {
                is StatusResult.Verified -> {
                    val updated = status.record
                    if (!persist(updated)) {
                        interrupt(updated, prepared, "storage_unavailable", "Could not persist resumed transfer state")
                        return
                    }
                    onSnapshot(snapshotFor(updated, "Resumed"))
                    val outcome = drive(client, updated, prepared)
                    if (outcome == FlowOutcome.INTERRUPTED) {
                        // The durable record and staging copy are retained.
                    }
                }
                is StatusResult.SignedError -> {
                    handleSignedError(status.response, record, prepared)
                }
                is StatusResult.NetworkFailure -> {
                    interrupt(record, prepared, status.error.code, status.error.message ?: "Network unavailable")
                }
                is StatusResult.ProtocolFailure -> {
                    failProtocol(record, prepared, status.error)
                }
            }
        } finally {
            synchronized(activeLock) {
                activeClient = null
                activePrepared = null
            }
            client.close()
        }
    }

    /** Requests cancellation of the running upload; the engine performs signed cancel and cleanup. */
    fun requestCancel() {
        cancelRequested = true
        synchronized(activeLock) {
            activeClient?.cancelActiveCall()
        }
    }

    /**
     * Cancels an interrupted transfer while no upload coroutine is running.
     * Local staging is always removed; the signed desktop cancel is best effort.
     */
    suspend fun cancelNow() {
        cancelRequested = true
        synchronized(activeLock) {
            activeClient?.cancelActiveCall()
        }
        val record = queueStore.load()
        if (record == null) {
            onSnapshot(TransferSnapshot(phase = TransferPhase.IDLE))
            return
        }
        cleanupLocal(record)
        onSnapshot(cancelledSnapshot(record))
        bestEffortCancel(record)
    }

    /** Dismisses an outcome-unknown record and removes any private staging copy. */
    suspend fun dismiss() {
        val record = queueStore.load()
        if (record != null) cleanupLocal(record) else queueStore.clear()
        onSnapshot(TransferSnapshot(phase = TransferPhase.IDLE))
    }

    // ── Upload initiation ──────────────────────────────────────────────────

    private suspend fun uploadOne(uri: String): FlowOutcome {
        onSnapshot(
            TransferSnapshot(
                phase = TransferPhase.PREPARING,
                message = "Validating and hashing the selected document"
            )
        )

        val credentials = credentialsLoader()
        if (credentials == null || credentials.isExpired(nowSeconds()) || !credentials.hasEndpoint()) {
            onSnapshot(
                TransferSnapshot(
                    phase = TransferPhase.EXPIRED,
                    errorCode = "session_expired",
                    message = "The paired session is no longer valid. Pair again before uploading."
                )
            )
            return FlowOutcome.STOPPED
        }

        val prepared = try {
            preparer.prepare(resolver.resolve(uri)) { cancelRequested }
        } catch (e: TransferCancelledException) {
            return cancelledBeforeInit()
        } catch (e: TransferValidationException) {
            return failWithoutRecord(e.code, e.message ?: "The selected document is not supported")
        } catch (e: TransferException) {
            return failWithoutRecord(e.code, e.message ?: "The selected document could not be prepared")
        } catch (e: Exception) {
            return failWithoutRecord("source_unavailable", e.message ?: "The selected document could not be read")
        }

        if (cancelRequested) {
            prepared.cleanup()
            return cancelledBeforeInit()
        }

        synchronized(activeLock) { activePrepared = prepared }
        onSnapshot(
            TransferSnapshot(
                phase = TransferPhase.PREPARING,
                filename = prepared.filename,
                size = prepared.size,
                receivedBytes = 0L,
                message = "Ready to send"
            )
        )

        val now = nowSeconds()
        var record = PersistedTransfer(
            sessionId = credentials.sessionId,
            transferId = "",
            uri = prepared.uri,
            filename = prepared.filename,
            mimeType = prepared.mimeType,
            size = prepared.size,
            sha256Hex = prepared.sha256Hex,
            strategy = prepared.strategy,
            spoolName = prepared.spoolName,
            nextChunkIndex = 0L,
            receivedBytes = 0L,
            state = "initiating",
            initUncertain = true,
            createdAt = now,
            updatedAt = now,
            expiresAt = now + TransferProtocolLimits.APPROVAL_WAIT_SECONDS
        )

        // The pre-init record is durable before any bytes leave the device. If
        // the init response is lost, a restart can distinguish "never created"
        // from "created but unknown" and will not silently retry.
        if (!queueStore.save(record)) {
            prepared.cleanup()
            onSnapshot(
                TransferSnapshot(
                    phase = TransferPhase.FAILED,
                    filename = prepared.filename,
                    size = prepared.size,
                    errorCode = "storage_unavailable",
                    message = "Could not persist transfer state before uploading"
                )
            )
            return FlowOutcome.STOPPED
        }

        val client = try {
            createClient(credentials)
        } catch (e: Exception) {
            prepared.cleanup()
            queueStore.clear()
            onSnapshot(
                TransferSnapshot(
                    phase = TransferPhase.FAILED,
                    filename = prepared.filename,
                    size = prepared.size,
                    errorCode = "transport_unavailable",
                    message = e.message ?: "Could not create a transfer connection"
                )
            )
            return FlowOutcome.STOPPED
        }
        synchronized(activeLock) { activeClient = client }

        var outcome = FlowOutcome.STOPPED
        try {
            val init = try {
                client.init(prepared.filename, prepared.mimeType, prepared.size, prepared.sha256Hex)
            } catch (e: CancellationException) {
                throw e
            } catch (e: TransferNetworkException) {
                if (cancelRequested) return cancelPath(record, prepared, client)
                emitInitUnknown(record.copy(filename = prepared.filename, size = prepared.size))
                return FlowOutcome.INTERRUPTED
            } catch (e: IOException) {
                if (cancelRequested) return cancelPath(record, prepared, client)
                emitInitUnknown(record.copy(filename = prepared.filename, size = prepared.size))
                return FlowOutcome.INTERRUPTED
            } catch (e: TransferProtocolException) {
                return failProtocol(record, prepared, e)
            } catch (e: TransferException) {
                return failProtocol(
                    record,
                    prepared,
                    TransferProtocolException(e.code, e.message ?: "Upload initialization failed")
                )
            } catch (e: Exception) {
                if (cancelRequested) return cancelPath(record, prepared, client)
                return failProtocol(
                    record,
                    prepared,
                    TransferProtocolException("init_failed", e.message ?: "Upload initialization failed")
                )
            }

            if (!init.ok) {
                return failProtocol(
                    record,
                    prepared,
                    TransferProtocolException(
                        init.errorCode ?: "init_rejected",
                        init.errorMessage ?: "The desktop refused the upload"
                    )
                )
            }
            if (init.state != "awaiting_desktop_approval" && init.state != "receiving") {
                return failProtocol(
                    record,
                    prepared,
                    TransferProtocolException(
                        "unexpected_init_state",
                        "The desktop returned an unexpected initial state"
                    )
                )
            }
            if (!TRANSFER_ID_PATTERN.matches(init.transferId)) {
                return failProtocol(
                    record,
                    prepared,
                    TransferProtocolException("invalid_transfer_id", "The desktop returned an invalid transfer id")
                )
            }

            record = record.copy(
                transferId = init.transferId,
                initUncertain = false,
                state = init.state
            )
            val applied = applyTransferPayload(record, init)
                ?: return failProtocol(
                    record,
                    prepared,
                    TransferProtocolException(
                        "init_identity_mismatch",
                        "The desktop response did not match the selected document"
                    )
                )
            record = applied
            if (!persist(record)) {
                return interrupt(
                    record,
                    prepared,
                    "storage_unavailable",
                    "Could not persist the initialized transfer"
                )
            }
            onSnapshot(snapshotFor(record, "Waiting for permission on the desktop"))

            outcome = drive(client, record, prepared)
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            if (record.transferId.isNotEmpty()) {
                outcome = interrupt(
                    record,
                    prepared,
                    "upload_interrupted",
                    e.message ?: "The upload was interrupted"
                )
            } else {
                outcome = failProtocol(
                    record,
                    prepared,
                    TransferProtocolException("upload_failed", e.message ?: "The upload failed")
                )
            }
        } finally {
            synchronized(activeLock) {
                activeClient = null
                activePrepared = null
            }
            client.close()
        }
        return outcome
    }

    // ── Phase driver ───────────────────────────────────────────────────────

    private suspend fun drive(
        client: TransferHttpClient,
        initial: PersistedTransfer,
        prepared: PreparedUpload
    ): FlowOutcome {
        var record = initial
        while (true) {
            currentCoroutineContext().ensureActive()
            if (cancelRequested) return cancelPath(record, prepared, client)

            val step = try {
                when (record.state) {
                    "created", "awaiting_desktop_approval" ->
                        waitForApproval(client, record, prepared)
                    "receiving" ->
                        receiveChunks(client, record, prepared)
                    "verifying", "awaiting_final_acceptance" ->
                        waitForFinal(client, record, prepared)
                    "committed" ->
                        Step.Done(complete(record, prepared))
                    "rejected", "discarded" ->
                        Step.Done(terminal(record, prepared, TransferPhase.REJECTED, "The desktop rejected the upload"))
                    "cancelled" ->
                        Step.Done(terminal(record, prepared, TransferPhase.CANCELLED, "The upload was cancelled"))
                    "expired" ->
                        Step.Done(terminal(record, prepared, TransferPhase.EXPIRED, "The upload expired before completion"))
                    "failed" ->
                        Step.Done(terminal(record, prepared, TransferPhase.FAILED, "The upload failed on the desktop"))
                    else ->
                        Step.Done(
                            failProtocol(
                                record,
                                prepared,
                                TransferProtocolException("invalid_transfer_state", "Unknown transfer state")
                            )
                        )
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: IOException) {
                Step.Done(interrupt(record, prepared, "network_error", e.message ?: "Network connection lost"))
            } catch (e: TransferException) {
                Step.Done(failProtocol(record, prepared, e))
            } catch (e: Exception) {
                Step.Done(
                    failProtocol(
                        record,
                        prepared,
                        TransferProtocolException("upload_failed", e.message ?: "Upload failed")
                    )
                )
            }

            when (step) {
                is Step.Continue -> record = step.record
                is Step.Done -> return step.outcome
            }
        }
    }

    private suspend fun waitForApproval(
        client: TransferHttpClient,
        initial: PersistedTransfer,
        prepared: PreparedUpload
    ): Step {
        var record = initial
        var failures = 0
        while (true) {
            currentCoroutineContext().ensureActive()
            if (cancelRequested) return Step.Done(cancelPath(record, prepared, client))
            if (record.state != "created" && record.state != "awaiting_desktop_approval") {
                return Step.Continue(record)
            }
            if (nowSeconds() >= record.expiresAt) {
                return Step.Done(terminal(record, prepared, TransferPhase.EXPIRED, "Desktop permission expired"))
            }

            when (val result = queryStatus(client, record)) {
                is StatusResult.Verified -> {
                    failures = 0
                    record = result.record
                    if (!persist(record)) {
                        return Step.Done(
                            interrupt(record, prepared, "storage_unavailable", "Could not persist transfer state")
                        )
                    }
                    onSnapshot(snapshotFor(record, approvalMessage(record)))
                    if (record.state !in setOf("created", "awaiting_desktop_approval")) {
                        return Step.Continue(record)
                    }
                }
                is StatusResult.SignedError -> {
                    return Step.Done(handleSignedError(result.response, record, prepared))
                }
                is StatusResult.NetworkFailure -> {
                    failures += 1
                    if (failures > maxConsecutiveNetworkFailures) {
                        return Step.Done(
                            interrupt(
                                record,
                                prepared,
                                result.error.code,
                                result.error.message ?: "Lost contact with the desktop"
                            )
                        )
                    }
                }
                is StatusResult.ProtocolFailure -> {
                    return Step.Done(failProtocol(record, prepared, result.error))
                }
            }
            delayFn(pollIntervalMs)
        }
    }

    private suspend fun receiveChunks(
        client: TransferHttpClient,
        initial: PersistedTransfer,
        prepared: PreparedUpload
    ): Step {
        var record = initial
        val totalChunks = TransferValidation.totalChunks(record.size)

        if (record.nextChunkIndex >= totalChunks) {
            return queryStatusStep(client, record, prepared)
        }

        var index = record.nextChunkIndex
        var cursor: ChunkCursor? = ChunkCursor(prepared, index)
        var pendingBuffer: ByteArray? = null
        var failures = 0

        try {
            while (index < totalChunks) {
                currentCoroutineContext().ensureActive()
                if (cancelRequested) return Step.Done(cancelPath(record, prepared, client))

                val expectedLength = TransferValidation.expectedChunkLength(record.size, index)
                val buffer = pendingBuffer ?: try {
                    cursor!!.readChunk(index, expectedLength)
                } catch (e: TransferSourceException) {
                    return Step.Done(failProtocol(record, prepared, e))
                } catch (e: Exception) {
                    return Step.Done(
                        failProtocol(
                            record,
                            prepared,
                            TransferSourceException(
                                "source_changed",
                                "The selected document could not be read at the expected position",
                                e
                            )
                        )
                    )
                }
                pendingBuffer = buffer

                val response = try {
                    client.sendChunk(record.transferId, index, buffer)
                } catch (e: CancellationException) {
                    throw e
                } catch (e: TransferNetworkException) {
                    failures += 1
                    if (cancelRequested) return Step.Done(cancelPath(record, prepared, client))
                    if (failures > maxConsecutiveNetworkFailures) {
                        return Step.Done(
                            interrupt(
                                record,
                                prepared,
                                e.code,
                                e.message ?: "Network connection lost while sending a chunk"
                            )
                        )
                    }
                    when (val status = queryStatus(client, record)) {
                        is StatusResult.Verified -> {
                            record = status.record
                            if (!persist(record)) {
                                return Step.Done(
                                    interrupt(record, prepared, "storage_unavailable", "Could not persist transfer state")
                                )
                            }
                            if (record.state != "receiving") {
                                return Step.Continue(record)
                            }
                            if (record.nextChunkIndex > index) {
                                // The desktop committed the chunk but the ack was lost.
                                pendingBuffer = null
                                index = record.nextChunkIndex
                                cursor?.close()
                                cursor = if (index < totalChunks) ChunkCursor(prepared, index) else null
                            } else {
                                // Not committed: resend the identical bytes with a fresh sequence.
                                cursor?.close()
                                cursor = if (index + 1 < totalChunks) ChunkCursor(prepared, index + 1) else null
                            }
                        }
                        is StatusResult.SignedError -> {
                            return Step.Done(handleSignedError(status.response, record, prepared))
                        }
                        is StatusResult.NetworkFailure -> {
                            failures += 1
                            if (failures > maxConsecutiveNetworkFailures) {
                                return Step.Done(
                                    interrupt(
                                        record,
                                        prepared,
                                        status.error.code,
                                        status.error.message ?: "Lost contact with the desktop"
                                    )
                                )
                            }
                        }
                        is StatusResult.ProtocolFailure -> {
                            return Step.Done(failProtocol(record, prepared, status.error))
                        }
                    }
                    delayFn(backoffMs(failures))
                    continue
                } catch (e: TransferProtocolException) {
                    return Step.Done(failProtocol(record, prepared, e))
                } catch (e: TransferException) {
                    return Step.Done(
                        failProtocol(record, prepared, TransferProtocolException(e.code, e.message ?: "Chunk failed"))
                    )
                }

                failures = 0
                val applied = applyTransferPayload(record, response)
                    ?: return Step.Done(
                        failProtocol(
                            record,
                            prepared,
                            TransferProtocolException(
                                "chunk_response_mismatch",
                                "The desktop chunk response did not match the transfer"
                            )
                        )
                    )
                record = applied

                if (!response.ok) {
                    val code = response.errorCode ?: "chunk_rejected"
                    when (code) {
                        "chunk_out_of_order" -> {
                            pendingBuffer = null
                            index = record.nextChunkIndex
                            cursor?.close()
                            cursor = if (index < totalChunks) ChunkCursor(prepared, index) else null
                            if (!persist(record)) {
                                return Step.Done(
                                    interrupt(record, prepared, "storage_unavailable", "Could not persist transfer state")
                                )
                            }
                            continue
                        }
                        "replay_sequence_invalid", "replay_nonce_detected" -> {
                            delayFn(backoffMs(1))
                            continue
                        }
                        else -> {
                            return Step.Done(
                                failProtocol(
                                    record,
                                    prepared,
                                    TransferProtocolException(
                                        code,
                                        response.errorMessage ?: "The desktop rejected the chunk"
                                    )
                                )
                            )
                        }
                    }
                }

                if (!persist(record)) {
                    return Step.Done(
                        interrupt(record, prepared, "storage_unavailable", "Could not persist verified progress")
                    )
                }
                onSnapshot(snapshotFor(record, "Uploading verified chunks"))
                pendingBuffer = null

                if (record.state != "receiving") {
                    return Step.Continue(record)
                }

                index = record.nextChunkIndex
                if (cursor?.nextIndex != index) {
                    cursor?.close()
                    cursor = if (index < totalChunks) ChunkCursor(prepared, index) else null
                }
            }
        } finally {
            cursor?.close()
        }

        // All chunks were acknowledged. Ask for the authoritative verification
        // state rather than assuming success from the last ack.
        return queryStatusStep(client, record, prepared)
    }

    private suspend fun waitForFinal(
        client: TransferHttpClient,
        initial: PersistedTransfer,
        prepared: PreparedUpload
    ): Step {
        var record = initial
        var failures = 0
        while (true) {
            currentCoroutineContext().ensureActive()
            if (cancelRequested) return Step.Done(cancelPath(record, prepared, client))
            if (record.state in TERMINAL_STATES) return Step.Continue(record)
            if (record.state != "verifying" && record.state != "awaiting_final_acceptance") {
                return Step.Continue(record)
            }
            if (nowSeconds() >= record.expiresAt) {
                return Step.Done(
                    terminal(record, prepared, TransferPhase.EXPIRED, "Final acceptance expired on the desktop")
                )
            }

            onSnapshot(
                snapshotFor(
                    record,
                    if (record.state == "verifying") {
                        "Desktop is verifying the file"
                    } else {
                        "Verified. Waiting for final acceptance on the desktop"
                    }
                )
            )

            when (val result = queryStatus(client, record)) {
                is StatusResult.Verified -> {
                    failures = 0
                    record = result.record
                    if (!persist(record)) {
                        return Step.Done(
                            interrupt(record, prepared, "storage_unavailable", "Could not persist transfer state")
                        )
                    }
                    onSnapshot(snapshotFor(record, acceptanceMessage(record)))
                }
                is StatusResult.SignedError -> {
                    return Step.Done(handleSignedError(result.response, record, prepared))
                }
                is StatusResult.NetworkFailure -> {
                    failures += 1
                    if (failures > maxConsecutiveNetworkFailures) {
                        return Step.Done(
                            interrupt(
                                record,
                                prepared,
                                result.error.code,
                                result.error.message ?: "Lost contact with the desktop"
                            )
                        )
                    }
                }
                is StatusResult.ProtocolFailure -> {
                    return Step.Done(failProtocol(record, prepared, result.error))
                }
            }
            delayFn(pollIntervalMs)
        }
    }

    private suspend fun queryStatusStep(
        client: TransferHttpClient,
        record: PersistedTransfer,
        prepared: PreparedUpload
    ): Step {
        var failures = 0
        while (true) {
            if (cancelRequested) return Step.Done(cancelPath(record, prepared, client))
            when (val result = queryStatus(client, record)) {
                is StatusResult.Verified -> {
                    if (!persist(result.record)) {
                        return Step.Done(
                            interrupt(result.record, prepared, "storage_unavailable", "Could not persist transfer state")
                        )
                    }
                    if (result.record.state == "receiving" &&
                        result.record.nextChunkIndex >= TransferValidation.totalChunks(result.record.size)
                    ) {
                        return Step.Done(
                            failProtocol(
                                result.record,
                                prepared,
                                TransferProtocolException(
                                    "verification_not_started",
                                    "The desktop did not begin verification after the final chunk"
                                )
                            )
                        )
                    }
                    return Step.Continue(result.record)
                }
                is StatusResult.SignedError ->
                    return Step.Done(handleSignedError(result.response, record, prepared))
                is StatusResult.NetworkFailure -> {
                    failures += 1
                    if (cancelRequested) return Step.Done(cancelPath(record, prepared, client))
                    if (failures > maxConsecutiveNetworkFailures) {
                        return Step.Done(
                            interrupt(
                                record,
                                prepared,
                                result.error.code,
                                result.error.message ?: "Lost contact with the desktop"
                            )
                        )
                    }
                    delayFn(backoffMs(failures))
                }
                is StatusResult.ProtocolFailure ->
                    return Step.Done(failProtocol(record, prepared, result.error))
            }
        }
    }

    // ── Status and payload validation ──────────────────────────────────────

    private suspend fun queryStatus(
        client: TransferHttpClient,
        record: PersistedTransfer
    ): StatusResult {
        return try {
            val response = client.status(record.transferId)
            if (!response.ok && response.state == "error") {
                StatusResult.SignedError(response, record)
            } else {
                val updated = applyTransferPayload(record, response)
                if (updated == null) {
                    StatusResult.ProtocolFailure(
                        TransferProtocolException(
                            "status_identity_mismatch",
                            "The desktop status did not match the stored transfer identity"
                        )
                    )
                } else {
                    StatusResult.Verified(response, updated)
                }
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: TransferNetworkException) {
            StatusResult.NetworkFailure(e)
        } catch (e: TransferProtocolException) {
            StatusResult.ProtocolFailure(e)
        } catch (e: IOException) {
            StatusResult.NetworkFailure(
                TransferNetworkException("network_error", e.message ?: "Network connection lost", e)
            )
        } catch (e: TransferException) {
            StatusResult.ProtocolFailure(
                TransferProtocolException(e.code, e.message ?: "Transfer status failed")
            )
        }
    }

    /**
     * Validates every authenticated identity and geometry field before progress
     * is updated. Returns null when the response does not belong to this upload.
     */
    private fun applyTransferPayload(
        record: PersistedTransfer,
        response: VerifiedTransferResponse
    ): PersistedTransfer? {
        val payload = response.payload
        val transferId = response.transferId
        if (transferId != record.transferId || !TRANSFER_ID_PATTERN.matches(transferId)) return null

        if (payload.has("filename")) {
            if (payload.optString("filename", "") != record.filename) return null
        }
        if (payload.has("size")) {
            if (payload.optLong("size", -1L) != record.size) return null
        }
        if (payload.has("sha256")) {
            val sha = payload.optString("sha256", "")
            if (!sha.equals(record.sha256Hex, ignoreCase = true)) return null
        }

        val state = response.state
        if (state !in KNOWN_STATES) return null

        val totalChunks = TransferValidation.totalChunks(record.size)
        val nextChunkIndex = payload.optLong("next_chunk_index", record.nextChunkIndex)
        if (nextChunkIndex !in 0L..totalChunks) return null

        val receivedBytes = payload.optLong("received_bytes", record.receivedBytes)
        if (receivedBytes != expectedBytesAt(nextChunkIndex, record.size)) return null

        val expiresRaw = payload.optDouble("expires_at", 0.0)
        val expiresAt = if (expiresRaw.isFinite() && expiresRaw > 0.0) {
            ceil(expiresRaw).toLong()
        } else {
            record.expiresAt
        }

        val errorCode = payload.optString("error_code", "")
            .takeIf { it.isNotEmpty() && it != "null" && it.length <= 64 }
        val publishedFilename = payload.optString("published_filename", "")
            .takeIf { it.isNotEmpty() && it != "null" }
            ?.takeIf {
                it.length <= 255 &&
                    !it.contains('/') &&
                    !it.contains('\\') &&
                    it.none { ch -> ch.code < 0x20 || ch.code == 0x7F }
            }

        return record.copy(
            state = state,
            nextChunkIndex = nextChunkIndex,
            receivedBytes = receivedBytes,
            expiresAt = expiresAt,
            errorCode = errorCode ?: record.errorCode,
            publishedFilename = publishedFilename ?: record.publishedFilename,
            updatedAt = nowSeconds()
        )
    }

    private fun expectedBytesAt(nextChunkIndex: Long, size: Long): Long {
        val totalChunks = TransferValidation.totalChunks(size)
        if (nextChunkIndex >= totalChunks) return size
        return nextChunkIndex * TransferValidation.CHUNK_BYTES
    }

    // ── Outcomes and cleanup ───────────────────────────────────────────────

    private fun complete(record: PersistedTransfer, prepared: PreparedUpload): FlowOutcome {
        if (record.receivedBytes != record.size) {
            return failProtocol(
                record,
                prepared,
                TransferProtocolException(
                    "committed_size_mismatch",
                    "The desktop committed a transfer with an unexpected byte count"
                )
            )
        }
        prepared.cleanup()
        queueStore.clear()
        onSnapshot(
            TransferSnapshot(
                phase = TransferPhase.COMMITTED,
                transferId = record.transferId,
                filename = record.filename,
                size = record.size,
                receivedBytes = record.size,
                state = "committed",
                publishedFilename = record.publishedFilename,
                message = "Accepted by the desktop and saved to Downloads"
            )
        )
        return FlowOutcome.COMPLETED
    }

    private fun terminal(
        record: PersistedTransfer,
        prepared: PreparedUpload,
        phase: TransferPhase,
        message: String
    ): FlowOutcome {
        cleanupLocal(record)
        onSnapshot(
            TransferSnapshot(
                phase = phase,
                transferId = record.transferId.takeIf { it.isNotEmpty() },
                filename = record.filename,
                size = record.size,
                receivedBytes = record.receivedBytes,
                state = record.state,
                errorCode = record.errorCode,
                message = message
            )
        )
        return FlowOutcome.STOPPED
    }

    private fun interrupt(
        record: PersistedTransfer,
        prepared: PreparedUpload,
        code: String,
        message: String
    ): FlowOutcome {
        queueStore.save(record)
        onSnapshot(
            TransferSnapshot(
                phase = TransferPhase.INTERRUPTED,
                transferId = record.transferId.takeIf { it.isNotEmpty() },
                filename = record.filename,
                size = record.size,
                receivedBytes = record.receivedBytes,
                state = record.state,
                errorCode = code,
                message = message,
                resumable = record.transferId.isNotEmpty() && !record.initUncertain
            )
        )
        return FlowOutcome.INTERRUPTED
    }

    private fun failProtocol(
        record: PersistedTransfer,
        prepared: PreparedUpload,
        error: TransferException
    ): FlowOutcome {
        cleanupLocal(record)
        onSnapshot(
            TransferSnapshot(
                phase = TransferPhase.FAILED,
                transferId = record.transferId.takeIf { it.isNotEmpty() },
                filename = record.filename,
                size = record.size,
                receivedBytes = record.receivedBytes,
                state = record.state,
                errorCode = error.code,
                message = error.message ?: "Upload failed"
            )
        )
        return FlowOutcome.STOPPED
    }

    private fun failLocal(record: PersistedTransfer, code: String, message: String): FlowOutcome {
        cleanupLocal(record)
        onSnapshot(
            TransferSnapshot(
                phase = TransferPhase.FAILED,
                transferId = record.transferId.takeIf { it.isNotEmpty() },
                filename = record.filename,
                size = record.size,
                receivedBytes = record.receivedBytes,
                state = record.state,
                errorCode = code,
                message = message
            )
        )
        return FlowOutcome.STOPPED
    }

    private fun failWithoutRecord(code: String, message: String): FlowOutcome {
        onSnapshot(
            TransferSnapshot(
                phase = TransferPhase.FAILED,
                errorCode = code,
                message = message
            )
        )
        return FlowOutcome.STOPPED
    }

    private suspend fun cancelPath(
        record: PersistedTransfer,
        prepared: PreparedUpload,
        client: TransferHttpClient
    ): FlowOutcome {
        cleanupLocal(record)
        onSnapshot(cancelledSnapshot(record))
        bestEffortCancel(record, client)
        return FlowOutcome.STOPPED
    }

    private fun handleSignedError(
        response: VerifiedTransferResponse,
        record: PersistedTransfer,
        prepared: PreparedUpload
    ): FlowOutcome {
        val code = response.errorCode ?: "transfer_refused"
        val updated = record.copy(errorCode = code)
        val phase = when (code) {
            "session_invalid", "session_expired", "transfer_not_found" -> TransferPhase.EXPIRED
            else -> TransferPhase.FAILED
        }
        val message = when (code) {
            "transfer_not_found" -> "The desktop no longer has this transfer"
            "session_invalid", "session_expired" -> "The paired session is no longer valid"
            else -> response.errorMessage ?: "The desktop refused the transfer request"
        }
        return terminal(updated, prepared, phase, message)
    }

    private fun cleanupLocal(record: PersistedTransfer) {
        val prepared = synchronized(activeLock) { activePrepared }
        if (prepared != null) {
            prepared.cleanup()
        } else {
            record.spoolName?.let { name ->
                preparer.spoolFileByName(name)?.delete()
            }
        }
        queueStore.clear()
    }

    private suspend fun bestEffortCancel(
        record: PersistedTransfer,
        existingClient: TransferHttpClient? = null
    ) {
        if (record.initUncertain || record.transferId.isEmpty()) return
        val credentials = credentialsLoader() ?: return
        if (credentials.sessionId != record.sessionId || credentials.isExpired(nowSeconds()) ||
            !credentials.hasEndpoint()
        ) {
            return
        }
        var client = existingClient
        var owned = false
        try {
            if (client == null) {
                client = createClient(credentials)
                owned = true
            }
            try {
                client.cancel(record.transferId)
            } catch (e: Exception) {
                // Local cancellation is authoritative; the desktop will expire
                // or stall-timeout the transfer if the signed cancel is lost.
            }
        } finally {
            if (owned) client?.close()
        }
    }

    private fun emitInitUnknown(record: PersistedTransfer) {
        onSnapshot(
            TransferSnapshot(
                phase = TransferPhase.INIT_OUTCOME_UNKNOWN,
                filename = record.filename,
                size = record.size,
                receivedBytes = 0L,
                state = record.state,
                errorCode = "init_outcome_unknown",
                message = "The desktop may have received the upload request, but no response arrived. " +
                    "For safety the app will not retry automatically. Dismiss this item and select the file again.",
                resumable = false
            )
        )
    }

    private fun persist(record: PersistedTransfer): Boolean = queueStore.save(record)

    private fun snapshotFor(record: PersistedTransfer, message: String): TransferSnapshot {
        return TransferSnapshot(
            phase = phaseFor(record),
            transferId = record.transferId.takeIf { it.isNotEmpty() },
            filename = record.filename,
            size = record.size,
            receivedBytes = record.receivedBytes,
            state = record.state,
            publishedFilename = record.publishedFilename,
            errorCode = record.errorCode,
            message = message,
            resumable = record.state in setOf("receiving", "verifying", "awaiting_final_acceptance")
        )
    }

    private fun phaseFor(record: PersistedTransfer): TransferPhase = when (record.state) {
        "created", "awaiting_desktop_approval" -> TransferPhase.WAITING_DESKTOP_APPROVAL
        "receiving" -> TransferPhase.RECEIVING
        "verifying" -> TransferPhase.VERIFYING
        "awaiting_final_acceptance" -> TransferPhase.AWAITING_FINAL_ACCEPTANCE
        "committed" -> TransferPhase.COMMITTED
        "rejected", "discarded" -> TransferPhase.REJECTED
        "cancelled" -> TransferPhase.CANCELLED
        "expired" -> TransferPhase.EXPIRED
        "failed" -> TransferPhase.FAILED
        else -> if (record.initUncertain) TransferPhase.INIT_OUTCOME_UNKNOWN else TransferPhase.PREPARING
    }

    private fun cancelledBeforeInit(): FlowOutcome {
        onSnapshot(
            TransferSnapshot(
                phase = TransferPhase.CANCELLED,
                message = "Upload cancelled before any request was sent. No file data left this device.",
                errorCode = "cancelled"
            )
        )
        return FlowOutcome.STOPPED
    }

    private fun cancelledSnapshot(record: PersistedTransfer): TransferSnapshot =
        TransferSnapshot(
            phase = TransferPhase.CANCELLED,
            transferId = record.transferId.takeIf { it.isNotEmpty() },
            filename = record.filename,
            size = record.size,
            receivedBytes = record.receivedBytes,
            state = "cancelled",
            message = "Upload cancelled. Any bytes already received remain quarantined on the desktop until it deletes them."
        )

    private fun approvalMessage(record: PersistedTransfer): String =
        if (record.state == "created") {
            "Upload request created. Waiting for desktop permission"
        } else {
            "Waiting for permission on the desktop"
        }

    private fun acceptanceMessage(record: PersistedTransfer): String = when (record.state) {
        "verifying" -> "Desktop is verifying the file"
        "awaiting_final_acceptance" -> "Verified. Waiting for final acceptance on the desktop"
        "committed" -> "Accepted by the desktop and saved to Downloads"
        else -> "Checking transfer status"
    }

    private fun backoffMs(failures: Int): Long {
        var value = BACKOFF_BASE_MS
        repeat((failures - 1).coerceIn(0, 5)) { value = min(value * 2, BACKOFF_MAX_MS) }
        return value
    }

    private fun createClient(credentials: SessionCredentials): TransferHttpClient {
        val transport = transportFactory.create(credentials)
        return TransferHttpClient(
            credentials = credentials,
            sequences = sequenceManager,
            transport = transport,
            nonceSource = { ByteArray(16).also(secureRandom::nextBytes) },
            clockMillis = clockMillis,
            requestGate = requestGate
        )
    }

    // ── Internal protocol types ────────────────────────────────────────────

    private enum class FlowOutcome { COMPLETED, STOPPED, INTERRUPTED }

    private sealed class Step {
        data class Continue(val record: PersistedTransfer) : Step()
        data class Done(val outcome: FlowOutcome) : Step()
    }

    private sealed class StatusResult {
        data class Verified(
            val response: VerifiedTransferResponse,
            val record: PersistedTransfer
        ) : StatusResult()

        data class SignedError(
            val response: VerifiedTransferResponse,
            val record: PersistedTransfer
        ) : StatusResult()

        data class NetworkFailure(val error: TransferNetworkException) : StatusResult()
        data class ProtocolFailure(val error: TransferProtocolException) : StatusResult()
    }

    private class ChunkCursor(
        private val prepared: PreparedUpload,
        startIndex: Long
    ) : Closeable {
        private var stream: InputStream = prepared.openChunkStream(startIndex)
        var nextIndex: Long = startIndex
            private set

        fun readChunk(index: Long, expectedLength: Int): ByteArray {
            require(index == nextIndex) { "chunk cursor out of position" }
            val data = ChunkReader.readExactly(stream, expectedLength)
            nextIndex = index + 1L
            return data
        }

        override fun close() {
            try {
                stream.close()
            } catch (e: Exception) {
                // Stream closure is best effort; the source read error is preserved.
            }
        }
    }
}
