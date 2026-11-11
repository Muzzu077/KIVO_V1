package com.kivo.companion

import com.kivo.companion.model.SessionCredentials
import com.kivo.companion.storage.KeystoreSecretStorage
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.file.Files
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey

class KeystoreStorageTest {

    private lateinit var tempDir: File
    private lateinit var testKey: SecretKey
    private lateinit var storage: KeystoreSecretStorage

    @Before
    fun setUp() {
        tempDir = Files.createTempDirectory("keystore_test_").toFile()

        val keyGen = KeyGenerator.getInstance("AES")
        keyGen.init(256)
        testKey = keyGen.generateKey()

        storage = KeystoreSecretStorage(
            storageDirectory = tempDir,
            customKeyProvider = { testKey }
        )
    }

    @After
    fun tearDown() {
        tempDir.deleteRecursively()
    }

    private fun createSampleCredentials(
        sessionId: String = "sess_12345678",
        expiresAt: Long = (System.currentTimeMillis() / 1000) + 3600
    ): SessionCredentials {
        return SessionCredentials(
            sessionId = sessionId,
            deviceId = "dev_abc",
            bearerToken = "sess_tok_sample_bearer_token_value",
            upstreamKey = ByteArray(32) { 0x01 },
            downstreamKey = ByteArray(32) { 0x02 },
            expiresAt = expiresAt
        )
    }

    @Test
    fun testEncryptDecryptRoundTrip() {
        val creds = createSampleCredentials()
        assertTrue(storage.saveCredentials(creds))

        val loaded = storage.loadCredentials()
        assertNotNull(loaded)
        assertEquals(creds.sessionId, loaded?.sessionId)
        assertEquals(creds.deviceId, loaded?.deviceId)
        assertEquals(creds.bearerToken, loaded?.bearerToken)
        assertArrayEquals(creds.upstreamKey, loaded?.upstreamKey)
        assertArrayEquals(creds.downstreamKey, loaded?.downstreamKey)
        assertEquals(creds.expiresAt, loaded?.expiresAt)
    }

    @Test
    fun testDecryptionFailsWithWrongKey() {
        val creds = createSampleCredentials()
        assertTrue(storage.saveCredentials(creds))

        // Recreate storage with a different AES key
        val wrongKey = KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()
        val otherStorage = KeystoreSecretStorage(storageDirectory = tempDir, customKeyProvider = { wrongKey })

        val loaded = otherStorage.loadCredentials()
        assertNull("Decryption with wrong key must fail and return null", loaded)
    }

    @Test
    fun testTamperedCiphertextRejectedByAeadTag() {
        val creds = createSampleCredentials()
        assertTrue(storage.saveCredentials(creds))

        val file = storage.getStorageFile()
        val bytes = file.readBytes()
        // Tamper with last byte (part of GCM tag)
        bytes[bytes.size - 1] = (bytes[bytes.size - 1].toInt() xor 0xFF).toByte()
        file.writeBytes(bytes)

        val loaded = storage.loadCredentials()
        assertNull("Tampered ciphertext must fail GCM tag verification", loaded)
    }

    @Test
    fun testTamperedIvRejected() {
        val creds = createSampleCredentials()
        assertTrue(storage.saveCredentials(creds))

        val file = storage.getStorageFile()
        val bytes = file.readBytes()
        // Envelope: [1 byte ver][2 bytes sessLen][sessLen bytes][12 bytes IV]
        // Tamper with IV byte
        val sessLen = (ByteBuffer.wrap(bytes, 1, 2).order(ByteOrder.BIG_ENDIAN).short.toInt() and 0xFFFF)
        val ivOffset = 1 + 2 + sessLen
        bytes[ivOffset] = (bytes[ivOffset].toInt() xor 0xAA).toByte()
        file.writeBytes(bytes)

        val loaded = storage.loadCredentials()
        assertNull("Tampered IV must fail decryption", loaded)
    }

    @Test
    fun testTamperedSessionIdInAadRejected() {
        val creds = createSampleCredentials("sess_original")
        assertTrue(storage.saveCredentials(creds))

        val file = storage.getStorageFile()
        val bytes = file.readBytes()
        // Tamper with sessionId in header
        val sessLen = (ByteBuffer.wrap(bytes, 1, 2).order(ByteOrder.BIG_ENDIAN).short.toInt() and 0xFFFF)
        bytes[3] = 'x'.code.toByte()
        file.writeBytes(bytes)

        val loaded = storage.loadCredentials()
        assertNull("Tampered AAD session ID must fail authentication", loaded)
    }

    @Test
    fun testCorruptOrTruncatedEnvelopeFailsSafely() {
        val file = storage.getStorageFile()
        file.writeBytes(byteArrayOf(0x01, 0x00)) // Truncated envelope

        val loaded = storage.loadCredentials()
        assertNull("Truncated envelope must fail safely", loaded)
    }

    @Test
    fun testInvalidEnvelopeVersionRejected() {
        val creds = createSampleCredentials()
        assertTrue(storage.saveCredentials(creds))

        val file = storage.getStorageFile()
        val bytes = file.readBytes()
        bytes[0] = 0x99.toByte() // Wrong envelope version
        file.writeBytes(bytes)

        val loaded = storage.loadCredentials()
        assertNull("Invalid envelope version must be rejected", loaded)
    }

    @Test
    fun testExpiredSessionPurgedOnLoad() {
        val expiredTime = (System.currentTimeMillis() / 1000) - 100
        val expiredCreds = createSampleCredentials(expiresAt = expiredTime)
        assertTrue(storage.saveCredentials(expiredCreds))

        val loaded = storage.loadCredentials()
        assertNull("Expired credentials must not be returned", loaded)
        assertFalse("Storage file must be cleared after detecting expiry", storage.getStorageFile().exists())
    }

    @Test
    fun testClearDeletesStorageFile() {
        val creds = createSampleCredentials()
        assertTrue(storage.saveCredentials(creds))
        assertTrue(storage.getStorageFile().exists())

        storage.clear()
        assertFalse(storage.getStorageFile().exists())
        assertNull(storage.loadCredentials())
    }

    @Test
    fun testEndpointAndSequenceRoundTrip() {
        val creds = createSampleCredentials().copy(
            nextSequence = 42L,
            hostIp = "192.168.1.50",
            port = 8443,
            certFingerprint = "A".repeat(64)
        )
        assertTrue(storage.saveCredentials(creds))

        val loaded = storage.loadCredentials()
        assertEquals(42L, loaded?.nextSequence)
        assertEquals("192.168.1.50", loaded?.hostIp)
        assertEquals(8443, loaded?.port)
        assertEquals("A".repeat(64), loaded?.certFingerprint)
    }

    @Test
    fun testReserveTransferSequenceIsDurableBeforeReturn() {
        val creds = createSampleCredentials()
        assertTrue(storage.saveCredentials(creds))

        assertEquals(1L, storage.reserveTransferSequence(creds.sessionId))
        val afterFirst = KeystoreSecretStorage(
            storageDirectory = tempDir,
            customKeyProvider = { testKey }
        ).loadCredentials()
        assertEquals("Advancement must be durable immediately", 2L, afterFirst?.nextSequence)

        assertEquals(2L, storage.reserveTransferSequence(creds.sessionId))
        assertEquals(3L, storage.reserveTransferSequence(creds.sessionId))
    }

    @Test
    fun testReserveTransferSequenceRejectsWrongSessionAndExpiredCredentials() {
        val creds = createSampleCredentials()
        assertTrue(storage.saveCredentials(creds))
        assertNull(storage.reserveTransferSequence("sess_other"))

        val expired = createSampleCredentials(expiresAt = (System.currentTimeMillis() / 1000) - 1)
        assertTrue(storage.saveCredentials(expired))
        assertNull(storage.reserveTransferSequence(expired.sessionId))
    }

    @Test
    fun testConcurrentSequenceReservationIsUniqueAndMonotonic() {
        val creds = createSampleCredentials()
        assertTrue(storage.saveCredentials(creds))

        val values = java.util.Collections.synchronizedSet(mutableSetOf<Long>())
        val threads = (1..8).map {
            Thread {
                repeat(25) {
                    storage.reserveTransferSequence(creds.sessionId)?.let(values::add)
                }
            }
        }
        threads.forEach(Thread::start)
        threads.forEach(Thread::join)

        assertEquals(200, values.size)
        assertEquals((1L..200L).toSet(), values)
        assertEquals(201L, storage.loadCredentials()?.nextSequence)
    }

    @Test
    fun testRecoveryAndTransferRecordsAreEncryptedAndIndependentlyClearable() {
        val recovery = org.json.JSONObject()
            .put("record_type", "pairing_recovery")
            .put("kPollB64", "c2VjcmV0")
        val transfer = org.json.JSONObject()
            .put("record_type", "transfer_state")
            .put("transferId", "tr_0123456789abcdef01234567")

        assertTrue(storage.savePairingRecovery(recovery))
        assertTrue(storage.saveTransferState(transfer))

        val rawRecovery = storage.getPairingRecoveryFile().readBytes().toString(Charsets.ISO_8859_1)
        assertFalse(rawRecovery.contains("c2VjcmV0"))
        assertFalse(rawRecovery.contains("pairing_recovery"))

        assertEquals("pairing_recovery", storage.loadPairingRecovery()?.optString("record_type"))
        assertEquals("transfer_state", storage.loadTransferState()?.optString("record_type"))

        storage.clearPairingRecovery()
        assertNull(storage.loadPairingRecovery())
        assertNotNull(storage.loadTransferState())

        storage.clearTransferState()
        assertNull(storage.loadTransferState())
    }

    @Test
    fun testCorruptRecoveryRecordFailsClosed() {
        val recovery = org.json.JSONObject().put("record_type", "pairing_recovery")
        assertTrue(storage.savePairingRecovery(recovery))
        val file = storage.getPairingRecoveryFile()
        val bytes = file.readBytes()
        bytes[bytes.size - 1] = (bytes[bytes.size - 1].toInt() xor 0x01).toByte()
        file.writeBytes(bytes)

        assertNull(storage.loadPairingRecovery())
        assertFalse("Corrupt record must be removed", file.exists())
    }
}
