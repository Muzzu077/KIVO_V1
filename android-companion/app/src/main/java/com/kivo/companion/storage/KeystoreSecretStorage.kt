package com.kivo.companion.storage

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import com.kivo.companion.model.SessionCredentials
import org.json.JSONObject
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.KeyStore
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

private const val ANDROID_KEYSTORE = "AndroidKeyStore"
private const val MASTER_KEY_ALIAS = "kivo_bridge_master_key"
private const val STORAGE_FILE_NAME = "kivo_session.enc"
private const val PAIRING_RECOVERY_FILE_NAME = "kivo_pairing_recovery.enc"
private const val TRANSFER_STATE_FILE_NAME = "kivo_transfer_state.enc"
private const val DOWNLOAD_STATE_FILE_NAME = "kivo_download_state.enc"
private const val PAIRING_RECOVERY_AAD_ID = "kivo-pairing-recovery-v1"
private const val TRANSFER_STATE_AAD_ID = "kivo-transfer-state-v1"
private const val DOWNLOAD_STATE_AAD_ID = "kivo-download-state-v1"
private const val ENVELOPE_VERSION: Byte = 1
private const val GCM_IV_LENGTH = 12
private const val GCM_TAG_LENGTH = 128
private const val MAX_ENVELOPE_BYTES = 1024 * 1024

/**
 * Native Android Keystore AES-256-GCM envelope storage for session credentials,
 * pairing-recovery state, and the interrupted-transfer record.
 *
 * All records use the same app-private master key but distinct authenticated
 * additional data. Existing `kivo_session.enc` files remain readable: their AAD
 * is still `kivo-envelope-v1:<sessionId>`.
 */
class KeystoreSecretStorage(
    private val context: Context? = null,
    private val keyAlias: String = MASTER_KEY_ALIAS,
    private val customKeyProvider: (() -> SecretKey)? = null,
    storageDirectory: File? = null
) {
    private val storageDir: File = storageDirectory ?: context?.filesDir
        ?: throw IllegalArgumentException("Either context or storageDirectory must be provided")
    private val storageFile = File(storageDir, STORAGE_FILE_NAME)
    private val pairingRecoveryFile = File(storageDir, PAIRING_RECOVERY_FILE_NAME)
    private val transferStateFile = File(storageDir, TRANSFER_STATE_FILE_NAME)
    private val downloadStateFile = File(storageDir, DOWNLOAD_STATE_FILE_NAME)

    init {
        if (!storageDir.exists()) {
            storageDir.mkdirs()
        }
        if (customKeyProvider == null) {
            ensureKeyExists()
        }
    }

    private fun ensureKeyExists() {
        val keyStore = KeyStore.getInstance(ANDROID_KEYSTORE)
        keyStore.load(null)

        if (!keyStore.containsAlias(keyAlias)) {
            val keyGenerator = KeyGenerator.getInstance(
                KeyProperties.KEY_ALGORITHM_AES,
                ANDROID_KEYSTORE
            )
            val spec = KeyGenParameterSpec.Builder(
                keyAlias,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .setRandomizedEncryptionRequired(true)
                .build()

            keyGenerator.init(spec)
            keyGenerator.generateKey()
        }
    }

    private fun getSecretKey(): SecretKey {
        customKeyProvider?.let { return it() }
        val keyStore = KeyStore.getInstance(ANDROID_KEYSTORE)
        keyStore.load(null)
        val entry = keyStore.getEntry(keyAlias, null) as? KeyStore.SecretKeyEntry
            ?: throw IllegalStateException("Keystore entry for $keyAlias not found")
        return entry.secretKey
    }

    /**
     * Encrypts and writes session credentials to app-private storage.
     * Envelope format: [1 byte version][2 bytes AAD id len][AAD id][12 bytes IV][ciphertext + 16 bytes GCM tag]
     */
    @Synchronized
    fun saveCredentials(credentials: SessionCredentials): Boolean {
        val json = JSONObject().apply {
            put("record_type", "session")
            put("sessionId", credentials.sessionId)
            put("deviceId", credentials.deviceId)
            put("bearerToken", credentials.bearerToken)
            put("upstreamKeyB64", Base64.getEncoder().encodeToString(credentials.upstreamKey))
            put("downstreamKeyB64", Base64.getEncoder().encodeToString(credentials.downstreamKey))
            put("expiresAt", credentials.expiresAt)
            put("nextSequence", credentials.nextSequence)
            put("hostIp", credentials.hostIp)
            put("port", credentials.port)
            put("certFingerprint", credentials.certFingerprint)
        }
        return writeEnvelope(storageFile, credentials.sessionId, json)
    }

    /**
     * Reads and decrypts stored session credentials with full AAD verification.
     */
    @Synchronized
    fun loadCredentials(): SessionCredentials? {
        val (headerId, json) = readEnvelope(storageFile) ?: return null
        return try {
            val sessionId = json.optString("sessionId", "")
            if (sessionId.isEmpty() || sessionId != headerId) {
                clearLocked(storageFile)
                return null
            }
            val deviceId = json.optString("deviceId", "")
            val bearerToken = json.optString("bearerToken", "")
            val upKey = decodeBase64(json.optString("upstreamKeyB64", ""))
            val downKey = decodeBase64(json.optString("downstreamKeyB64", ""))
            val expiresAt = json.optLong("expiresAt", 0L)
            if (deviceId.isEmpty() || bearerToken.isEmpty() ||
                upKey?.size != 32 || downKey?.size != 32 || expiresAt <= 0L
            ) {
                clearLocked(storageFile)
                return null
            }

            val nextSequence = json.optLong("nextSequence", 1L).coerceAtLeast(1L)
            val port = json.optInt("port", 0)
            val credentials = SessionCredentials(
                sessionId = sessionId,
                deviceId = deviceId,
                bearerToken = bearerToken,
                upstreamKey = upKey,
                downstreamKey = downKey,
                expiresAt = expiresAt,
                nextSequence = nextSequence,
                hostIp = json.optString("hostIp", ""),
                port = port,
                certFingerprint = json.optString("certFingerprint", "")
            )
            if (credentials.isExpired()) {
                clearLocked(storageFile)
                return null
            }
            credentials
        } catch (e: Exception) {
            clearLocked(storageFile)
            null
        }
    }

    /**
     * Atomically reserves one transfer sequence and persists its advancement
     * before returning. Returns null when credentials are missing, belong to a
     * different session, or cannot be durably updated.
     */
    @Synchronized
    fun reserveTransferSequence(sessionId: String): Long? {
        val current = loadCredentials() ?: return null
        if (current.sessionId != sessionId) return null
        val sequence = current.nextSequence.coerceAtLeast(1L)
        if (sequence >= Long.MAX_VALUE) return null
        val updated = current.copy(nextSequence = sequence + 1L)
        return if (saveCredentials(updated)) sequence else null
    }

    @Synchronized
    fun updateCredentials(credentials: SessionCredentials): Boolean = saveCredentials(credentials)

    @Synchronized
    fun savePairingRecovery(record: JSONObject): Boolean =
        writeEnvelope(pairingRecoveryFile, PAIRING_RECOVERY_AAD_ID, record)

    @Synchronized
    fun loadPairingRecovery(): JSONObject? =
        readEnvelope(pairingRecoveryFile)
            ?.takeIf { it.first == PAIRING_RECOVERY_AAD_ID }
            ?.second

    @Synchronized
    fun clearPairingRecovery() {
        clearLocked(pairingRecoveryFile)
    }

    @Synchronized
    fun saveTransferState(record: JSONObject): Boolean =
        writeEnvelope(transferStateFile, TRANSFER_STATE_AAD_ID, record)

    @Synchronized
    fun loadTransferState(): JSONObject? =
        readEnvelope(transferStateFile)
            ?.takeIf { it.first == TRANSFER_STATE_AAD_ID }
            ?.second

    @Synchronized
    fun clearTransferState() {
        clearLocked(transferStateFile)
    }

    @Synchronized
    fun saveDownloadState(record: JSONObject): Boolean =
        writeEnvelope(downloadStateFile, DOWNLOAD_STATE_AAD_ID, record)

    @Synchronized
    fun loadDownloadState(): JSONObject? =
        readEnvelope(downloadStateFile)
            ?.takeIf { it.first == DOWNLOAD_STATE_AAD_ID }
            ?.second

    @Synchronized
    fun clearDownloadState() {
        clearLocked(downloadStateFile)
    }

    /**
     * Securely deletes stored credentials and all recovery material.
     */
    @Synchronized
    fun clear() {
        clearLocked(storageFile)
        clearLocked(pairingRecoveryFile)
        clearLocked(transferStateFile)
        clearLocked(downloadStateFile)
    }

    @Synchronized
    fun clearCredentialsOnly() {
        clearLocked(storageFile)
    }

    fun getStorageFile(): File = storageFile

    fun getPairingRecoveryFile(): File = pairingRecoveryFile

    fun getTransferStateFile(): File = transferStateFile

    fun getDownloadStateFile(): File = downloadStateFile

    fun getStorageDirectory(): File = storageDir

    private fun writeEnvelope(target: File, aadId: String, json: JSONObject): Boolean {
        return try {
            require(aadId.isNotEmpty() && aadId.toByteArray(Charsets.UTF_8).size <= 255)
            val secretKey = getSecretKey()
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.ENCRYPT_MODE, secretKey)

            val aad = "kivo-envelope-v1:$aadId".toByteArray(Charsets.UTF_8)
            cipher.updateAAD(aad)

            val plaintext = json.toString().toByteArray(Charsets.UTF_8)
            val ciphertext = cipher.doFinal(plaintext)
            val iv = cipher.iv
            val idBytes = aadId.toByteArray(Charsets.UTF_8)

            val headerLen = 1 + 2 + idBytes.size + iv.size
            val envelope = ByteBuffer.allocate(headerLen + ciphertext.size)
                .order(ByteOrder.BIG_ENDIAN)
                .put(ENVELOPE_VERSION)
                .putShort(idBytes.size.toShort())
                .put(idBytes)
                .put(iv)
                .put(ciphertext)
                .array()

            val tempFile = File(storageDir, "${target.name}.tmp")
            val out = java.io.FileOutputStream(tempFile)
            try {
                out.write(envelope)
                out.flush()
                out.fd.sync()
            } finally {
                out.close()
            }
            try {
                java.nio.file.Files.move(
                    tempFile.toPath(),
                    target.toPath(),
                    java.nio.file.StandardCopyOption.ATOMIC_MOVE,
                    java.nio.file.StandardCopyOption.REPLACE_EXISTING
                )
            } catch (atomicFailure: java.nio.file.AtomicMoveNotSupportedException) {
                java.nio.file.Files.move(
                    tempFile.toPath(),
                    target.toPath(),
                    java.nio.file.StandardCopyOption.REPLACE_EXISTING
                )
            }
            true
        } catch (e: Exception) {
            File(storageDir, "${target.name}.tmp").delete()
            false
        }
    }

    private fun readEnvelope(source: File): Pair<String, JSONObject>? {
        if (!source.exists()) return null
        return try {
            if (source.length() <= 0L || source.length() > MAX_ENVELOPE_BYTES) {
                clearLocked(source)
                return null
            }
            val bytes = source.readBytes()
            if (bytes.size < 1 + 2 + GCM_IV_LENGTH + (GCM_TAG_LENGTH / 8)) {
                clearLocked(source)
                return null
            }

            val buffer = ByteBuffer.wrap(bytes).order(ByteOrder.BIG_ENDIAN)
            val version = buffer.get()
            if (version != ENVELOPE_VERSION) {
                clearLocked(source)
                return null
            }

            val idLen = buffer.short.toInt() and 0xFFFF
            if (idLen <= 0 || idLen > 255 || buffer.remaining() < idLen + GCM_IV_LENGTH) {
                clearLocked(source)
                return null
            }

            val idBytes = ByteArray(idLen)
            buffer.get(idBytes)
            val aadId = String(idBytes, Charsets.UTF_8)

            val iv = ByteArray(GCM_IV_LENGTH)
            buffer.get(iv)

            val ciphertext = ByteArray(buffer.remaining())
            buffer.get(ciphertext)

            val secretKey = getSecretKey()
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            val spec = GCMParameterSpec(GCM_TAG_LENGTH, iv)
            cipher.init(Cipher.DECRYPT_MODE, secretKey, spec)

            val aad = "kivo-envelope-v1:$aadId".toByteArray(Charsets.UTF_8)
            cipher.updateAAD(aad)

            val plaintext = cipher.doFinal(ciphertext)
            val json = JSONObject(String(plaintext, Charsets.UTF_8))
            aadId to json
        } catch (e: Exception) {
            clearLocked(source)
            null
        }
    }

    private fun clearLocked(file: File) {
        try {
            if (file.exists()) file.delete()
        } catch (e: Exception) {
            // Best-effort deletion; the file remains encrypted if it survives.
        }
    }

    private fun decodeBase64(value: String): ByteArray? =
        try {
            Base64.getDecoder().decode(value)
        } catch (e: IllegalArgumentException) {
            null
        }
}
