package com.kivo.companion.transfer

import com.kivo.companion.storage.KeystoreSecretStorage

/**
 * Durable, concurrency-safe transfer sequence allocation.
 *
 * Sequence 1 is the first authenticated transfer request for a newly paired
 * session. The next value is persisted (with fsync and atomic replacement)
 * before the caller is allowed to transmit. Sequences are never reused or
 * reset for the lifetime of a session.
 */
class TransferSequenceManager(
    private val storage: KeystoreSecretStorage
) {
    @Synchronized
    fun reserve(sessionId: String): Long {
        return storage.reserveTransferSequence(sessionId)
            ?: throw TransferException(
                "sequence_unavailable",
                "Could not durably reserve the next transfer sequence",
                retryable = false,
                terminal = true
            )
    }

    @Synchronized
    fun peek(sessionId: String): Long {
        val credentials = storage.loadCredentials() ?: return 1L
        if (credentials.sessionId != sessionId) return 1L
        return credentials.nextSequence.coerceAtLeast(1L)
    }
}
