package com.kivo.companion.transfer

import com.kivo.companion.model.SessionCredentials
import com.kivo.companion.storage.KeystoreSecretStorage
import kotlinx.coroutines.delay
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import java.util.Locale

/**
 * Serialized desktop-to-Android download engine.
 *
 * Ordering is deliberate:
 * 1. claim the opaque ticket with the existing authenticated init request;
 * 2. stream only in-order 1 MiB chunks, verifying every signed chunk digest;
 * 3. re-hash the app-private staged file against the signed whole-file SHA-256;
 * 4. ask the desktop to mark the transfer complete;
 * 5. let the user choose a SAF destination and publish a verified copy;
 * 6. delete staging only after the destination write has been flushed.
 *
 * A destination is never chosen or written before the desktop has acknowledged
 * completion, so a failed publication can never duplicate a partial remote
 * transfer. Publication is never automatic after a process restart: an
 * interrupted publication returns to the picker with the verified staged copy.
 */
class DownloadCoordinator(
    private val storage: KeystoreSecretStorage,
    private val stager: DownloadStager,
    private val destinationResolver: DownloadDestinationResolver,
    private val publisher: DownloadPublisher,
    private val transportFactory: TransferTransportFactory,
    private val onSnapshot: (DownloadSnapshot) -> Unit,
    private val credentialsLoader: () -> SessionCredentials? = { storage.loadCredentials() },
    private val sequenceManager: TransferSequenceManager = TransferSequenceManager(storage),
    private val recoveryStore: DownloadRecoveryStore = DownloadRecoveryStore(storage),
    private val nowSeconds: () -> Long = { System.currentTimeMillis() / 1000L },
    private val clockMillis: () -> Long = { System.currentTimeMillis() },
    private val delayFn: suspend (Long) -> Unit = { delay(it) },
    private val maxNetworkFailures: Int = TransferProtocolLimits.MAX_NETWORK_RETRIES,
    private val secureRandom: SecureRandom = SecureRandom(),
    private val requestGate: TransferRequestGate? = null
) {
    companion object {
        private const val BACKOFF_BASE_MS = 500L
        private const val BACKOFF_MAX_MS = 8_000L
        private const val MAX_CHUNK_BASE64_CHARS = 1_500_000
        private val TICKET = Regex("^tkt_[0-9a-f]{64}$")
        private val SHA = Regex("^[0-9a-f]{64}$")
        private val BASE64 = Regex("^[A-Za-z0-9+/]+={0,2}$")
        private val ACTIVE_STATES = setOf(
            "preparing",
            "claimed",
            "downloading",
            "verified"
        )
        private val READY_STATES = setOf("ready_to_publish", "publishing")
        private val COMPLETED_CODES = setOf("download_completed")
        private val EXPIRED_CODES = setOf(
            "download_expired",
            "download_not_found",
            "download_not_claimed",
            "download_idle_timeout",
            "session_expired",
            "session_invalid"
        )
        private val FAILED_CODES = setOf(
            "download_forbidden",
            "download_source_unavailable",
            "download_source_changed",
            "download_integrity_failed",
            "download_unavailable",
            "concurrent_download_limit"
        )
    }

    private val activeLock = Any()
    private var activeClient: TransferHttpClient? = null

    @Volatile
    private var cancelRequested = false

    // ── User-confirmed start and resume ─────────────────────────────────────

    /**
     * Claims and downloads the offered ticket. The caller must have already
     * shown the desktop identity, filename, size, storage warning, and obtained
     * explicit confirmation. A QR scan alone never starts a request.
     */
    suspend fun start(offer: DownloadOffer) {
        cancelRequested = false
        val credentials = credentialsLoader()
        if (!usableCredentials(credentials)) {
            emit(
                DownloadSnapshot(
                    phase = DownloadPhase.EXPIRED,
                    filename = offer.filename,
                    size = offer.size,
                    sha256Hex = offer.sha256Hex,
                    desktopLabel = "Paired desktop",
                    errorCode = "session_expired",
                    message = "The paired session is no longer valid. Pair again before downloading."
                )
            )
            return
        }
        if (credentials!!.deviceId != offer.deviceId) {
            emit(
                DownloadSnapshot(
                    phase = DownloadPhase.FAILED,
                    filename = offer.filename,
                    size = offer.size,
                    errorCode = "handoff_device_mismatch",
                    message = "This download was authorized for a different paired device."
                )
            )
            return
        }
        if (!TICKET.matches(offer.ticketId)) {
            emit(
                DownloadSnapshot(
                    phase = DownloadPhase.FAILED,
                    filename = offer.filename,
                    size = offer.size,
                    errorCode = "invalid_ticket",
                    message = "The download ticket is malformed."
                )
            )
            return
        }

        val existing = recoveryStore.load()
        if (existing != null) {
            if (existing.ticketId == offer.ticketId) {
                resumeRecord(existing, credentials)
                return
            }
            if (existing.state in ACTIVE_STATES || existing.state in READY_STATES) {
                emit(
                    downloadSnapshot(
                        existing,
                        DownloadPhase.FAILED,
                        "Another download is already pending. Resume or cancel it first.",
                        credentials
                    )
                )
                return
            }
            cleanupLocal(existing)
        }

        val stage = try {
            stager.newStage().also { stager.prepare(it, offer.size) }
        } catch (e: TransferSourceException) {
            emit(
                DownloadSnapshot(
                    phase = DownloadPhase.FAILED,
                    filename = offer.filename,
                    size = offer.size,
                    errorCode = e.code,
                    message = e.message ?: "Could not prepare app-private download staging"
                )
            )
            return
        } catch (e: Exception) {
            emit(
                DownloadSnapshot(
                    phase = DownloadPhase.FAILED,
                    filename = offer.filename,
                    size = offer.size,
                    errorCode = "staging_unavailable",
                    message = e.message ?: "Could not prepare app-private download staging"
                )
            )
            return
        }

        val now = nowSeconds()
        val record = PersistedDownload(
            sessionId = credentials.sessionId,
            ticketId = offer.ticketId,
            deviceId = offer.deviceId,
            filename = offer.filename,
            fileType = null,
            size = offer.size,
            sha256Hex = offer.sha256Hex,
            nextChunkIndex = 0L,
            receivedBytes = 0L,
            stageName = stage.name,
            state = "preparing",
            completeAcknowledged = false,
            createdAt = now,
            updatedAt = now,
            leaseExpiresAt = 0L
        )
        if (!persist(record)) {
            stager.delete(stage)
            emit(
                DownloadSnapshot(
                    phase = DownloadPhase.FAILED,
                    filename = offer.filename,
                    size = offer.size,
                    errorCode = "storage_unavailable",
                    message = "Could not persist the encrypted download record"
                )
            )
            return
        }
        emit(preparingSnapshot(record, credentials))

        val client = try {
            createClient(credentials)
        } catch (e: Exception) {
            interrupt(
                record,
                "transport_unavailable",
                e.message ?: "Could not create a transfer connection"
            )
            return
        }
        synchronized(activeLock) { activeClient = client }
        try {
            drive(client, record, credentials)
        } finally {
            synchronized(activeLock) { activeClient = null }
            client.close()
        }
    }

    /** Resumes the durable download after explicit user action. Never starts a new ticket. */
    suspend fun resume() {
        cancelRequested = false
        val record = recoveryStore.load()
        if (record == null) {
            emit(DownloadSnapshot())
            return
        }
        val credentials = credentialsLoader()
        if (!usableCredentials(credentials) || credentials!!.sessionId != record.sessionId) {
            failLocal(
                record,
                "session_expired",
                "The paired session is no longer valid. Pair again before resuming."
            )
            return
        }
        resumeRecord(record, credentials)
    }

    private suspend fun resumeRecord(record: PersistedDownload, credentials: SessionCredentials) {
        if (record.deviceId != credentials.deviceId) {
            failLocal(
                record,
                "handoff_device_mismatch",
                "The stored download belongs to a different paired device."
            )
            return
        }
        if (record.ticketId.isEmpty() || !TICKET.matches(record.ticketId)) {
            failLocal(record, "invalid_ticket", "The stored download ticket is malformed.")
            return
        }
        val stage = stager.stageByName(record.stageName) ?: run {
            failLocal(record, "staging_missing", "The private staging copy is no longer available.")
            return
        }

        if (record.state == "completed") {
            emit(downloadSnapshot(record, DownloadPhase.COMPLETED, "Saved to the selected destination", credentials))
            cleanupRecordOnly()
            return
        }
        if (record.state in READY_STATES) {
            val verified = try {
                verifyStagedCopy(record, stage)
            } catch (e: TransferCancelledException) {
                cancellationAfterComplete(record)
                return
            }
            if (!verified) {
                failLocal(
                    record,
                    "staging_modified",
                    "The verified staging copy no longer matches the download."
                )
                return
            }
            if (record.state == "publishing") {
                val updated = record.copy(state = "ready_to_publish", updatedAt = nowSeconds())
                if (!persist(updated)) {
                    failLocal(
                        record,
                        "storage_unavailable",
                        "Could not persist the interrupted publication state."
                    )
                    return
                }
                emit(
                    downloadSnapshot(
                        updated,
                        DownloadPhase.READY_TO_PUBLISH,
                        "Publication was interrupted. Choose the destination again; nothing is overwritten automatically.",
                        credentials
                    )
                )
            } else {
                emit(
                    downloadSnapshot(
                        record,
                        DownloadPhase.READY_TO_PUBLISH,
                        "Download verified. Choose where to save it.",
                        credentials
                    )
                )
            }
            return
        }
        if (record.state !in ACTIVE_STATES) {
            failLocal(record, "invalid_state", "The stored download state is invalid.")
            return
        }
        if (record.size > 0L && stage.length() != record.size) {
            try {
                stager.prepare(stage, record.size)
            } catch (e: TransferSourceException) {
                failLocal(record, e.code, e.message ?: "Could not prepare download staging")
                return
            }
        }

        emit(preparingSnapshot(record, credentials))
        val client = try {
            createClient(credentials)
        } catch (e: Exception) {
            interrupt(
                record,
                "transport_unavailable",
                e.message ?: "Could not create a transfer connection"
            )
            return
        }
        synchronized(activeLock) { activeClient = client }
        try {
            drive(client, record, credentials)
        } finally {
            synchronized(activeLock) { activeClient = null }
            client.close()
        }
    }

    // ── Cancellation and dismissal ─────────────────────────────────────────

    fun requestCancel() {
        cancelRequested = true
        synchronized(activeLock) { activeClient?.cancelActiveCall() }
    }

    /** Cancels while no coroutine is running; local data is always removed. */
    suspend fun cancelNow() {
        cancelRequested = true
        synchronized(activeLock) { activeClient?.cancelActiveCall() }
        val record = recoveryStore.load()
        if (record == null) {
            emit(DownloadSnapshot(phase = DownloadPhase.CANCELLED, message = "Download cancelled"))
            return
        }
        cleanupLocal(record)
        emit(
            DownloadSnapshot(
                phase = DownloadPhase.CANCELLED,
                ticketId = record.ticketId,
                filename = record.filename,
                size = record.size,
                receivedBytes = record.receivedBytes,
                desktopLabel = desktopLabel(credentialsLoader()),
                errorCode = "cancelled",
                message = "Download cancelled. The app-private staging copy was removed."
            )
        )
        bestEffortCancel(record, null)
    }

    suspend fun dismiss() {
        val record = recoveryStore.load()
        if (record != null) cleanupLocal(record) else recoveryStore.clear()
        emit(DownloadSnapshot())
    }

    // ── Publication ────────────────────────────────────────────────────────

    /**
     * Publishes the verified staged copy to a URI returned by
     * `ACTION_CREATE_DOCUMENT`. The URI is used immediately and never stored.
     */
    suspend fun publish(uri: String) {
        cancelRequested = false
        val record = recoveryStore.load()
        if (record == null) {
            emit(
                DownloadSnapshot(
                    phase = DownloadPhase.FAILED,
                    errorCode = "no_download",
                    message = "There is no verified download to publish."
                )
            )
            return
        }
        if (!record.completeAcknowledged || record.state !in READY_STATES ||
            record.nextChunkIndex != totalDownloadChunks(record.size) ||
            record.receivedBytes != record.size
        ) {
            emit(
                downloadSnapshot(
                    record,
                    DownloadPhase.FAILED,
                    "The download is not ready for publication.",
                    credentialsLoader()
                )
            )
            return
        }
        val stage = stager.stageByName(record.stageName)
        if (stage == null) {
            failLocal(record, "staging_missing", "The verified staging copy is no longer available.")
            return
        }
        val credentials = credentialsLoader()
        emit(
            downloadSnapshot(
                record,
                DownloadPhase.PUBLISHING,
                "Saving the verified download to the selected destination",
                credentials
            )
        )
        val stagedVerified = try {
            verifyStagedCopy(record, stage)
        } catch (e: TransferCancelledException) {
            cancellationAfterComplete(record)
            return
        }
        if (!stagedVerified) {
            if (cancelRequested) {
                cancellationAfterComplete(record)
            } else {
                failLocal(
                    record,
                    "staging_modified",
                    "The verified staging copy no longer matches the download."
                )
            }
            return
        }

        val publishing = record.copy(state = "publishing", errorCode = null, updatedAt = nowSeconds())
        if (!persist(publishing)) {
            failLocal(
                record,
                "storage_unavailable",
                "Could not persist the publication state; the staging copy was removed."
            )
            return
        }

        val destination = try {
            destinationResolver.resolve(uri)
        } catch (e: DownloadPublicationException) {
            publicationRetry(publishing, e.code, e.message ?: "The destination is not supported")
            return
        } catch (e: Exception) {
            publicationRetry(
                publishing,
                "destination_unavailable",
                e.message ?: "The destination could not be opened"
            )
            return
        }

        val result = try {
            publisher.publish(
                stage = stage,
                expectedSize = record.size,
                expectedSha256Hex = record.sha256Hex,
                destination = destination,
                isCancelled = { cancelRequested }
            )
        } catch (e: TransferCancelledException) {
            cancellationAfterComplete(record)
            return
        } catch (e: DownloadPublicationException) {
            if (cancelRequested) cancellationAfterComplete(record)
            else publicationRetry(publishing, e.code, e.message ?: "Publication failed")
            return
        } catch (e: Exception) {
            if (cancelRequested) cancellationAfterComplete(record)
            else publicationRetry(
                publishing,
                "publication_failed",
                e.message ?: "Publication failed"
            )
            return
        }

        val savedName = result.displayName?.takeIf { it.isNotBlank() } ?: record.filename
        val completed = publishing.copy(
            state = "completed",
            completeAcknowledged = true,
            publishedName = savedName,
            errorCode = null,
            updatedAt = nowSeconds()
        )
        val persisted = persist(completed)
        // Staging is removed only after the destination was written, flushed,
        // closed, and (where supported) reopened and re-hashed.
        stager.delete(stage)
        recoveryStore.clear()
        emit(
            DownloadSnapshot(
                phase = DownloadPhase.COMPLETED,
                ticketId = record.ticketId,
                filename = record.filename,
                size = record.size,
                receivedBytes = record.size,
                desktopLabel = desktopLabel(credentials),
                sha256Hex = record.sha256Hex,
                fileType = record.fileType,
                publishedName = savedName,
                destinationVerified = result.destinationVerified,
                message = if (result.destinationVerified) {
                    "Saved and verified at the selected destination."
                } else {
                    "Saved to the selected destination. The provider did not allow a verification read."
                }
            )
        )
        if (!persisted) {
            // The destination already contains the only copy. Nothing remains
            // that could be published twice.
            recoveryStore.clear()
        }
    }

    // ── Restore projection ─────────────────────────────────────────────────

    /**
     * Returns a truthful snapshot for a durable record without any network or
     * hashing work. A caller must still obtain explicit resume or publish
     * action; nothing is retried or written automatically.
     */
    fun restoredSnapshot(): DownloadSnapshot? {
        val record = try {
            recoveryStore.load()
        } catch (e: Exception) {
            null
        } ?: return null
        val credentials = try {
            credentialsLoader()
        } catch (e: Exception) {
            null
        }
        return when (record.state) {
            "preparing", "claimed", "downloading", "verified" -> downloadSnapshot(
                record,
                DownloadPhase.INTERRUPTED,
                "A download from ${desktopLabel(credentials)} was interrupted. Resume to reconcile signed progress.",
                credentials,
                resumable = true
            )
            "ready_to_publish" -> downloadSnapshot(
                record,
                DownloadPhase.READY_TO_PUBLISH,
                "Download verified. Choose where to save it.",
                credentials
            )
            "publishing" -> downloadSnapshot(
                record,
                DownloadPhase.READY_TO_PUBLISH,
                "Publication was interrupted. Choose the destination again; nothing is overwritten automatically.",
                credentials
            )
            "completed" -> downloadSnapshot(
                record,
                DownloadPhase.COMPLETED,
                "Saved to the selected destination",
                credentials
            )
            "cancelled" -> downloadSnapshot(record, DownloadPhase.CANCELLED, "Download cancelled", credentials)
            "expired" -> downloadSnapshot(record, DownloadPhase.EXPIRED, "The download ticket expired", credentials)
            "failed" -> downloadSnapshot(
                record,
                DownloadPhase.FAILED,
                record.errorCode ?: "The download failed",
                credentials
            )
            else -> null
        }
    }

    // ── Engine ─────────────────────────────────────────────────────────────

    private suspend fun drive(
        client: TransferHttpClient,
        initial: PersistedDownload,
        credentials: SessionCredentials
    ): FlowOutcome {
        var record = initial
        var failures = 0
        var next = record.nextChunkIndex
        var resync = true

        while (true) {
            if (cancelRequested) return cancelPath(record, client)
            if (resync) {
                resync = false
                val initResponse = try {
                    client.downloadInit(record.ticketId)
                } catch (e: TransferNetworkException) {
                    failures += 1
                    if (failures > maxNetworkFailures) {
                        return interrupt(
                            record,
                            e.code,
                            "Could not reach the desktop to claim the download. Resume to retry."
                        )
                    }
                    emit(
                        downloadSnapshot(
                            record,
                            DownloadPhase.RECONNECTING,
                            "Reconnecting to the desktop before claiming the download",
                            credentials,
                            attempt = failures
                        )
                    )
                    delayFn(backoffMs(failures))
                    resync = true
                    continue
                } catch (e: TransferException) {
                    return failLocal(record, e.code, e.message ?: "The desktop refused the download")
                } catch (e: Exception) {
                    return failLocal(
                        record,
                        "download_failed",
                        e.message ?: "The download request failed"
                    )
                }

                if (!initResponse.ok) {
                    val terminal = handleSignedError(initResponse, record, credentials)
                    if (terminal != null) return terminal
                    // `download_completed` is the only non-terminal signed
                    // refusal. Verify the staged copy before offering it.
                    return reconcileCompleted(record, credentials)
                }

                val status = try {
                    parseStatus(initResponse, record, credentials)
                } catch (e: TransferProtocolException) {
                    return failLocal(
                        record,
                        e.code,
                        e.message ?: "The desktop returned an invalid download response."
                    )
                }
                record = record.copy(
                    filename = status.filename,
                    fileType = status.fileType,
                    size = status.size,
                    sha256Hex = status.sha256Hex,
                    leaseExpiresAt = status.leaseExpiresAt,
                    updatedAt = nowSeconds()
                )
                if (status.state == "completed") {
                    return reconcileCompleted(record, credentials)
                }
                if (status.state !in setOf("claimed", "streaming")) {
                    return failLocal(
                        record,
                        "invalid_download_state",
                        "The desktop reported an unsupported download state."
                    )
                }
                next = minOf(record.nextChunkIndex, status.nextChunkIndex)
                record = record.copy(
                    nextChunkIndex = next,
                    receivedBytes = expectedDownloadBytes(next, record.size),
                    state = if (next == 0L) "claimed" else "downloading",
                    errorCode = null,
                    updatedAt = nowSeconds()
                )
                val stage = stager.stageByName(record.stageName)
                if (stage == null) {
                    return failLocal(
                        record,
                        "staging_missing",
                        "The private staging copy is no longer available."
                    )
                }
                if (record.size > 0L && stage.length() != record.size) {
                    try {
                        stager.prepare(stage, record.size)
                    } catch (e: TransferSourceException) {
                        return failLocal(record, e.code, e.message ?: "Could not prepare download staging")
                    }
                }
                if (!persist(record)) {
                    return failLocal(
                        record,
                        "storage_unavailable",
                        "Could not persist download progress; staging was removed to avoid an unsafe resume."
                    )
                }
                emit(
                    downloadSnapshot(
                        record,
                        DownloadPhase.DOWNLOADING,
                        if (next == 0L) "Claimed. Starting the verified download."
                        else "Resuming from the first chunk the desktop has not committed.",
                        credentials
                    )
                )
            }

            val total = totalDownloadChunks(record.size)
            var resyncRequested = false
            while (next < total) {
                if (cancelRequested) return cancelPath(record, client)
                val response = try {
                    client.downloadChunk(record.ticketId, next)
                } catch (e: TransferNetworkException) {
                    failures += 1
                    if (failures > maxNetworkFailures) {
                        return interrupt(
                            record,
                            e.code,
                            "The connection to the desktop was lost. Resume to continue from verified progress."
                        )
                    }
                    emit(
                        downloadSnapshot(
                            record,
                            DownloadPhase.RECONNECTING,
                            "Connection lost. Checking signed progress with the desktop.",
                            credentials,
                            attempt = failures
                        )
                    )
                    delayFn(backoffMs(failures))
                    resyncRequested = true
                    break
                } catch (e: TransferException) {
                    return failLocal(record, e.code, e.message ?: "The download chunk failed")
                } catch (e: Exception) {
                    return failLocal(
                        record,
                        "download_failed",
                        e.message ?: "The download chunk failed"
                    )
                }

                if (!response.ok) {
                    if (response.errorCode == "chunk_out_of_order") {
                        failures += 1
                        if (failures > maxNetworkFailures) {
                            return failLocal(
                                record,
                                "chunk_out_of_order",
                                "The desktop rejected repeated chunk ordering after bounded retries."
                            )
                        }
                        emit(
                            downloadSnapshot(
                                record,
                                DownloadPhase.RECONNECTING,
                                "Reconciling chunk ordering with the desktop",
                                credentials,
                                attempt = failures
                            )
                        )
                        resyncRequested = true
                        break
                    }
                    val terminal = handleSignedError(response, record, credentials)
                    if (terminal != null) return terminal
                    resyncRequested = true
                    break
                }

                val data = try {
                    parseChunk(response, next, record, credentials)
                } catch (e: TransferProtocolException) {
                    return failLocal(
                        record,
                        e.code,
                        e.message ?: "The desktop returned an invalid download chunk."
                    )
                }
                val offset = next * DOWNLOAD_CHUNK_BYTES
                val stage = stager.stageByName(record.stageName)
                if (stage == null) {
                    return failLocal(
                        record,
                        "staging_missing",
                        "The private staging copy is no longer available."
                    )
                }
                try {
                    stager.append(stage, offset, data)
                } catch (e: TransferSourceException) {
                    return failLocal(record, e.code, e.message ?: "Could not write the download chunk")
                } catch (e: Exception) {
                    return failLocal(
                        record,
                        "staging_write_failed",
                        e.message ?: "Could not write the download chunk"
                    )
                }
                failures = 0
                next += 1L
                record = record.copy(
                    nextChunkIndex = next,
                    receivedBytes = expectedDownloadBytes(next, record.size),
                    state = "downloading",
                    errorCode = null,
                    updatedAt = nowSeconds()
                )
                if (!persist(record)) {
                    return failLocal(
                        record,
                        "storage_unavailable",
                        "Could not persist download progress; staging was removed to avoid an unsafe resume."
                    )
                }
                emit(
                    downloadSnapshot(
                        record,
                        DownloadPhase.DOWNLOADING,
                        "Downloading verified chunks from ${desktopLabel(credentials)}",
                        credentials
                    )
                )
            }
            if (resyncRequested) {
                resync = true
                continue
            }

            // ── Whole-file verification ────────────────────────────────────
            if (cancelRequested) return cancelPath(record, client)
            emit(
                downloadSnapshot(
                    record,
                    DownloadPhase.VERIFYING,
                    "All chunks received. Verifying the whole-file SHA-256 before completing.",
                    credentials
                )
            )
            val stage = stager.stageByName(record.stageName)
            if (stage == null) {
                return failLocal(
                    record,
                    "staging_missing",
                    "The private staging copy is no longer available."
                )
            }
            val verified = try {
                stager.verifyStage(
                    stage = stage,
                    size = record.size,
                    sha256Hex = record.sha256Hex,
                    isCancelled = { cancelRequested }
                )
            } catch (e: TransferCancelledException) {
                return cancelPath(record, client)
            } catch (e: Exception) {
                return failLocal(
                    record,
                    "integrity_check_failed",
                    e.message ?: "Could not verify the downloaded file"
                )
            }
            if (!verified) {
                return failLocal(
                    record,
                    "download_integrity_failed",
                    "The downloaded bytes did not match the signed whole-file SHA-256. Nothing was completed or published."
                )
            }
            record = record.copy(
                state = "verified",
                nextChunkIndex = total,
                receivedBytes = record.size,
                errorCode = null,
                updatedAt = nowSeconds()
            )
            if (!persist(record)) {
                return failLocal(
                    record,
                    "storage_unavailable",
                    "Could not persist verified download state."
                )
            }

            // ── Signed desktop completion (before any destination picker) ──
            return completeLoop(client, record, credentials)
        }
    }

    private suspend fun completeLoop(
        client: TransferHttpClient,
        starting: PersistedDownload,
        credentials: SessionCredentials
    ): FlowOutcome {
        var record = starting
        var failures = 0
        while (true) {
            if (cancelRequested) return cancelPath(record, client)
            val response = try {
                client.downloadComplete(record.ticketId)
            } catch (e: TransferNetworkException) {
                failures += 1
                if (failures > maxNetworkFailures) {
                    return interrupt(
                        record,
                        e.code,
                        "The download is verified. Reconnect and resume to confirm completion with the desktop."
                    )
                }
                emit(
                    downloadSnapshot(
                        record,
                        DownloadPhase.RECONNECTING,
                        "Verified. Retrying the signed completion request.",
                        credentials,
                        attempt = failures
                    )
                )
                delayFn(backoffMs(failures))
                continue
            } catch (e: TransferException) {
                return failLocal(record, e.code, e.message ?: "The desktop refused completion")
            } catch (e: Exception) {
                return failLocal(
                    record,
                    "download_failed",
                    e.message ?: "The completion request failed"
                )
            }

            if (!response.ok) {
                if (response.errorCode == "download_incomplete") {
                    // The desktop's signed progress is authoritative. Return to
                    // init/status reconciliation and re-stream only the missing
                    // chunk range; local data is never published until the
                    // whole-file digest and completion both succeed.
                    failures += 1
                    if (failures > maxNetworkFailures) {
                        return failLocal(
                            record,
                            "download_incomplete",
                            "The desktop still reports missing chunks after bounded reconciliation."
                        )
                    }
                    emit(
                        downloadSnapshot(
                            record,
                            DownloadPhase.RECONNECTING,
                            "The desktop reports missing chunks. Reconciling signed progress.",
                            credentials,
                            attempt = failures
                        )
                    )
                    return drive(client, record, credentials)
                }
                val terminal = handleSignedError(response, record, credentials)
                if (terminal != null) return terminal
                return reconcileCompleted(record, credentials)
            }

            val status = try {
                parseStatus(response, record, credentials)
            } catch (e: TransferProtocolException) {
                return failLocal(
                    record,
                    e.code,
                    e.message ?: "The desktop returned an invalid completion response."
                )
            }
            if (status.state != "completed") {
                return failLocal(
                    record,
                    "invalid_download_state",
                    "The desktop did not confirm the download as complete."
                )
            }
            record = record.copy(
                state = "ready_to_publish",
                completeAcknowledged = true,
                nextChunkIndex = totalDownloadChunks(record.size),
                receivedBytes = record.size,
                leaseExpiresAt = status.leaseExpiresAt,
                errorCode = null,
                updatedAt = nowSeconds()
            )
            if (!persist(record)) {
                return failLocal(
                    record,
                    "storage_unavailable",
                    "The desktop completed the download but local completion state could not be persisted."
                )
            }
            emit(
                downloadSnapshot(
                    record,
                    DownloadPhase.READY_TO_PUBLISH,
                    "Download complete and verified. Choose where to save it.",
                    credentials
                )
            )
            return FlowOutcome.READY
        }
    }

    // ── Signed payload parsing ─────────────────────────────────────────────

    private data class ServerDownloadStatus(
        val filename: String,
        val fileType: String?,
        val size: Long,
        val sha256Hex: String,
        val state: String,
        val nextChunkIndex: Long,
        val receivedBytes: Long,
        val leaseExpiresAt: Long
    )

    private fun parseStatus(
        response: VerifiedTransferResponse,
        record: PersistedDownload,
        credentials: SessionCredentials
    ): ServerDownloadStatus {
        val payload = response.payload
        if (response.transferId != record.ticketId) {
            protocolFailure("The signed response names a different download.")
        }
        val downloadId = stringField(payload, "download_id")
        if (downloadId != record.ticketId) {
            protocolFailure("The signed download identity does not match the ticket.")
        }
        val deviceId = stringField(payload, "device_id")
        if (deviceId != credentials.deviceId || deviceId != record.deviceId) {
            protocolFailure("The download is bound to a different paired device.")
        }
        val filename = stringField(payload, "filename")
        if (filename != record.filename) {
            protocolFailure("The desktop filename does not match the scanned handoff.")
        }
        val size = integerField(payload, "size", 0L, DownloadTicketParser.MAX_DOWNLOAD_BYTES)
        if (size != record.size) {
            protocolFailure("The desktop file size does not match the scanned handoff.")
        }
        val sha = stringField(payload, "sha256")
        if (!SHA.matches(sha) || sha != record.sha256Hex.lowercase(Locale.ROOT)) {
            protocolFailure("The desktop file digest does not match the scanned handoff.")
        }
        val state = stringField(payload, "state").lowercase(Locale.ROOT)
        if (state != response.state.lowercase(Locale.ROOT)) {
            protocolFailure("The signed response state is inconsistent.")
        }
        val totalChunks = integerField(
            payload,
            "total_chunks",
            0L,
            Int.MAX_VALUE.toLong()
        )
        if (totalChunks != totalDownloadChunks(size)) {
            protocolFailure("The desktop chunk geometry is inconsistent.")
        }
        val chunkSize = integerField(payload, "chunk_size", 0L, DOWNLOAD_CHUNK_BYTES)
        if (chunkSize != DOWNLOAD_CHUNK_BYTES) {
            protocolFailure("The desktop chunk size is not supported.")
        }
        val next = integerField(payload, "next_chunk_index", 0L, totalChunks)
        val received = integerField(payload, "bytes_sent", 0L, size)
        if (received != expectedDownloadBytes(next, size)) {
            protocolFailure("The desktop byte progress is inconsistent with its chunk index.")
        }
        if (state == "completed" && (next != totalChunks || received != size)) {
            protocolFailure("The desktop reported completion with incomplete bytes.")
        }
        val expires = epochField(payload, "expires_at")
        val activeExpires = optionalEpochField(payload, "active_expires_at")
        val lease = if (activeExpires != null && activeExpires > 0.0) activeExpires else expires
        if (lease <= 0.0) protocolFailure("The download lease is missing.")
        val fileType = optionalStringField(payload, "file_type")
        if (fileType != null && (fileType.length > 64 || fileType.any { it.code < 0x20 })) {
            protocolFailure("The desktop file type hint is invalid.")
        }
        return ServerDownloadStatus(
            filename = filename,
            fileType = fileType,
            size = size,
            sha256Hex = sha,
            state = state,
            nextChunkIndex = next,
            receivedBytes = received,
            leaseExpiresAt = lease.toLong()
        )
    }

    private fun parseChunk(
        response: VerifiedTransferResponse,
        expectedIndex: Long,
        record: PersistedDownload,
        credentials: SessionCredentials
    ): ByteArray {
        val status = parseStatus(response, record, credentials)
        if (status.state != "streaming") {
            protocolFailure("The desktop returned a chunk outside the streaming state.")
        }
        val payload = response.payload
        val index = integerField(payload, "chunk_index", 0L, Int.MAX_VALUE.toLong())
        if (index != expectedIndex) {
            protocolFailure("The desktop returned an unexpected chunk index.")
        }
        val expectedLength = expectedDownloadChunkLength(record.size, index)
        if (expectedLength < 0L) {
            protocolFailure("The desktop returned a chunk outside the file geometry.")
        }
        val offset = integerField(
            payload,
            "offset",
            0L,
            DownloadTicketParser.MAX_DOWNLOAD_BYTES
        )
        if (offset != index * DOWNLOAD_CHUNK_BYTES) {
            protocolFailure("The desktop returned an unexpected chunk offset.")
        }
        val length = integerField(payload, "length", 0L, DOWNLOAD_CHUNK_BYTES)
        if (length != expectedLength) {
            protocolFailure("The desktop returned a chunk with an invalid length.")
        }
        val chunkSha = stringField(payload, "chunk_sha256")
        if (!SHA.matches(chunkSha)) {
            protocolFailure("The desktop chunk digest is malformed.")
        }
        val encoded = stringField(payload, "chunk_b64")
        if (encoded.length > MAX_CHUNK_BASE64_CHARS || encoded.length % 4 != 0 ||
            !BASE64.matches(encoded)
        ) {
            protocolFailure("The desktop chunk payload is not valid base64.")
        }
        val data = try {
            Base64.getDecoder().decode(encoded)
        } catch (e: IllegalArgumentException) {
            protocolFailure("The desktop chunk payload could not be decoded.")
        }
        if (data.size.toLong() != length) {
            protocolFailure("The decoded chunk length does not match the signed length.")
        }
        val actual = TransferCrypto.sha256Hex(data)
        if (!MessageDigest.isEqual(
                actual.toByteArray(Charsets.US_ASCII),
                chunkSha.toByteArray(Charsets.US_ASCII)
            )
        ) {
            protocolFailure("The chunk bytes do not match the signed chunk digest.")
        }
        return data
    }

    // ── Outcome helpers ────────────────────────────────────────────────────

    private suspend fun reconcileCompleted(
        record: PersistedDownload,
        credentials: SessionCredentials
    ): FlowOutcome {
        val stage = stager.stageByName(record.stageName)
        if (stage == null) {
            return failLocal(
                record,
                "staging_missing",
                "The desktop reports this download complete, but the private staging copy is missing."
            )
        }
        emit(
            downloadSnapshot(
                record,
                DownloadPhase.VERIFYING,
                "The desktop reports completion. Verifying the staged copy before publication.",
                credentials
            )
        )
        val verified = try {
            verifyStagedCopy(record, stage)
        } catch (e: TransferCancelledException) {
            cancellationAfterComplete(record)
            return FlowOutcome.STOPPED
        }
        if (!verified) {
            return failLocal(
                record,
                "download_integrity_failed",
                "The desktop reports completion, but the staged copy did not match its signed digest."
            )
        }
        val ready = record.copy(
            state = "ready_to_publish",
            completeAcknowledged = true,
            nextChunkIndex = totalDownloadChunks(record.size),
            receivedBytes = record.size,
            errorCode = null,
            updatedAt = nowSeconds()
        )
        if (!persist(ready)) {
            return failLocal(
                ready,
                "storage_unavailable",
                "Could not persist the verified download state."
            )
        }
        emit(
            downloadSnapshot(
                ready,
                DownloadPhase.READY_TO_PUBLISH,
                "Download complete and verified. Choose where to save it.",
                credentials
            )
        )
        return FlowOutcome.READY
    }

    private fun verifyStagedCopy(record: PersistedDownload, stage: File): Boolean {
        return try {
            stager.verifyStage(
                stage = stage,
                size = record.size,
                sha256Hex = record.sha256Hex,
                isCancelled = { cancelRequested }
            )
        } catch (e: TransferCancelledException) {
            throw e
        } catch (e: Exception) {
            false
        }
    }

    /**
     * Returns a terminal outcome for signed refusals, or null when the caller
     * must reconcile a completed desktop ticket locally.
     */
    private fun handleSignedError(
        response: VerifiedTransferResponse,
        record: PersistedDownload,
        credentials: SessionCredentials
    ): FlowOutcome? {
        val code = response.errorCode ?: "download_refused"
        if (code in COMPLETED_CODES) return null
        val message = response.errorMessage ?: when (code) {
            "session_expired", "session_invalid" -> "The paired session is no longer valid."
            "download_expired", "download_idle_timeout" -> "The desktop download ticket is no longer active."
            "download_forbidden" -> "This download belongs to a different paired device."
            "download_source_changed" -> "The desktop file changed and can no longer be downloaded."
            "download_integrity_failed" -> "The desktop detected an integrity failure."
            else -> "The desktop refused the download request."
        }
        val phase = when (code) {
            in EXPIRED_CODES -> DownloadPhase.EXPIRED
            else -> DownloadPhase.FAILED
        }
        cleanupLocal(record)
        emit(
            DownloadSnapshot(
                phase = phase,
                ticketId = record.ticketId,
                filename = record.filename,
                size = record.size,
                receivedBytes = record.receivedBytes,
                desktopLabel = desktopLabel(credentials),
                errorCode = code,
                message = message,
                resumable = false
            )
        )
        return FlowOutcome.STOPPED
    }

    private fun protocolFailure(message: String): Nothing =
        throw TransferProtocolException("invalid_download_response", message)

    private fun stringField(payload: JSONObject, key: String): String {
        val value = payload.opt(key)
        if (value !is String || value.isEmpty()) protocolFailure("The signed field $key is missing.")
        return value
    }

    private fun optionalStringField(payload: JSONObject, key: String): String? {
        val value = payload.opt(key) ?: return null
        if (value == JSONObject.NULL) return null
        if (value !is String) protocolFailure("The signed field $key is malformed.")
        return value.ifEmpty { null }
    }

    private fun integerField(
        payload: JSONObject,
        key: String,
        minimum: Long,
        maximum: Long
    ): Long {
        val value = payload.opt(key)
        if (value !is Number) protocolFailure("The signed field $key is missing.")
        val double = value.toDouble()
        if (!double.isFinite() || double % 1.0 != 0.0) {
            protocolFailure("The signed field $key is not an integer.")
        }
        val number = double.toLong()
        if (number < minimum || number > maximum) {
            protocolFailure("The signed field $key is out of range.")
        }
        return number
    }

    private fun epochField(payload: JSONObject, key: String): Double {
        val value = payload.opt(key)
        if (value !is Number) protocolFailure("The signed field $key is missing.")
        val number = value.toDouble()
        if (!number.isFinite() || number <= 0.0 || number > 4_102_444_800.0) {
            protocolFailure("The signed field $key is invalid.")
        }
        return number
    }

    private fun optionalEpochField(payload: JSONObject, key: String): Double? {
        val value = payload.opt(key) ?: return null
        if (value == JSONObject.NULL) return null
        if (value !is Number) protocolFailure("The signed field $key is malformed.")
        val number = value.toDouble()
        if (!number.isFinite() || number < 0.0 || number > 4_102_444_800.0) {
            protocolFailure("The signed field $key is invalid.")
        }
        return number
    }

    // ── Snapshot and state helpers ─────────────────────────────────────────

    private fun preparingSnapshot(
        record: PersistedDownload,
        credentials: SessionCredentials
    ): DownloadSnapshot = downloadSnapshot(
        record,
        DownloadPhase.PREPARING,
        "Claiming the desktop ticket and checking signed progress",
        credentials
    )

    private fun downloadSnapshot(
        record: PersistedDownload,
        phase: DownloadPhase,
        message: String,
        credentials: SessionCredentials?,
        attempt: Int = 0,
        resumable: Boolean = false
    ): DownloadSnapshot = DownloadSnapshot(
        phase = phase,
        ticketId = record.ticketId,
        filename = record.filename,
        size = record.size,
        receivedBytes = record.receivedBytes,
        desktopLabel = desktopLabel(credentials),
        sha256Hex = record.sha256Hex,
        fileType = record.fileType,
        errorCode = record.errorCode,
        message = message,
        attempt = attempt,
        resumable = resumable,
        publishedName = record.publishedName
    )

    private fun desktopLabel(credentials: SessionCredentials?): String {
        val host = credentials?.hostIp?.trim().orEmpty()
        return if (host.isEmpty()) "Paired KIVO desktop" else "KIVO desktop at $host"
    }

    private fun emit(snapshot: DownloadSnapshot) {
        try {
            onSnapshot(snapshot)
        } catch (e: Exception) {
            // A UI observation failure must not corrupt protocol state.
        }
    }

    private fun persist(record: PersistedDownload): Boolean {
        return try {
            recoveryStore.save(record)
        } catch (e: Exception) {
            false
        }
    }

    private fun interrupt(
        record: PersistedDownload,
        code: String,
        message: String
    ): FlowOutcome {
        val updated = record.copy(errorCode = code, updatedAt = nowSeconds())
        persist(updated)
        emit(
            DownloadSnapshot(
                phase = DownloadPhase.INTERRUPTED,
                ticketId = updated.ticketId,
                filename = updated.filename,
                size = updated.size,
                receivedBytes = updated.receivedBytes,
                desktopLabel = desktopLabel(credentialsLoader()),
                sha256Hex = updated.sha256Hex,
                fileType = updated.fileType,
                errorCode = code,
                message = message,
                resumable = true
            )
        )
        return FlowOutcome.INTERRUPTED
    }

    private fun failLocal(
        record: PersistedDownload,
        code: String,
        message: String
    ): FlowOutcome {
        cleanupLocal(record)
        emit(
            DownloadSnapshot(
                phase = DownloadPhase.FAILED,
                ticketId = record.ticketId,
                filename = record.filename,
                size = record.size,
                receivedBytes = record.receivedBytes,
                desktopLabel = desktopLabel(credentialsLoader()),
                sha256Hex = record.sha256Hex,
                errorCode = code,
                message = message
            )
        )
        return FlowOutcome.STOPPED
    }

    private suspend fun cancelPath(
        record: PersistedDownload,
        client: TransferHttpClient?
    ): FlowOutcome {
        cleanupLocal(record)
        emit(
            DownloadSnapshot(
                phase = DownloadPhase.CANCELLED,
                ticketId = record.ticketId,
                filename = record.filename,
                size = record.size,
                receivedBytes = record.receivedBytes,
                desktopLabel = desktopLabel(credentialsLoader()),
                errorCode = "cancelled",
                message = "Download cancelled. The app-private staging copy was removed."
            )
        )
        bestEffortCancel(record, client)
        return FlowOutcome.STOPPED
    }

    private fun cancellationAfterComplete(record: PersistedDownload) {
        cleanupLocal(record)
        emit(
            DownloadSnapshot(
                phase = DownloadPhase.CANCELLED,
                ticketId = record.ticketId,
                filename = record.filename,
                size = record.size,
                receivedBytes = record.size,
                desktopLabel = desktopLabel(credentialsLoader()),
                errorCode = "cancelled",
                message = "Publication cancelled. The selected destination was removed where the provider allowed it."
            )
        )
    }

    private fun publicationRetry(
        record: PersistedDownload,
        code: String,
        message: String
    ) {
        val ready = record.copy(
            state = "ready_to_publish",
            errorCode = code,
            updatedAt = nowSeconds()
        )
        persist(ready)
        emit(
            DownloadSnapshot(
                phase = DownloadPhase.READY_TO_PUBLISH,
                ticketId = ready.ticketId,
                filename = ready.filename,
                size = ready.size,
                receivedBytes = ready.size,
                desktopLabel = desktopLabel(credentialsLoader()),
                sha256Hex = ready.sha256Hex,
                fileType = ready.fileType,
                errorCode = code,
                message = "$message. The verified staging copy is still available; choose a destination again."
            )
        )
    }

    private fun cleanupLocal(record: PersistedDownload) {
        stager.deleteByName(record.stageName)
        recoveryStore.clear()
    }

    private fun cleanupRecordOnly() {
        recoveryStore.clear()
    }

    private suspend fun bestEffortCancel(
        record: PersistedDownload,
        existingClient: TransferHttpClient?
    ) {
        if (record.completeAcknowledged) return
        val credentials = try {
            credentialsLoader()
        } catch (e: Exception) {
            null
        } ?: return
        if (credentials.sessionId != record.sessionId || !usableCredentials(credentials)) return
        var client = existingClient
        var owned = false
        try {
            if (client == null) {
                client = createClient(credentials)
                owned = true
            }
            try {
                client.downloadCancel(record.ticketId)
            } catch (e: Exception) {
                // Local cancellation is authoritative. The desktop will expire
                // or idle-timeout the ticket if the signed cancel is lost.
            }
        } catch (e: Exception) {
            // Best effort only.
        } finally {
            if (owned) {
                try {
                    client?.close()
                } catch (e: Exception) {
                    // Nothing to release.
                }
            }
        }
    }

    private fun usableCredentials(credentials: SessionCredentials?): Boolean {
        return credentials != null &&
            !credentials.isExpired(nowSeconds()) &&
            credentials.hasEndpoint() &&
            credentials.sessionId.isNotEmpty() &&
            credentials.deviceId.isNotEmpty()
    }

    private fun backoffMs(failures: Int): Long {
        var value = BACKOFF_BASE_MS
        repeat((failures - 1).coerceIn(0, 5)) { value = minOf(value * 2, BACKOFF_MAX_MS) }
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

    private enum class FlowOutcome { READY, STOPPED, INTERRUPTED }
}
