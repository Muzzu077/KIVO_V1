package com.kivo.companion.transfer

import com.kivo.companion.crypto.TlsPinnerFactory
import com.kivo.companion.model.SessionCredentials
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.ResponseBody
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.math.BigDecimal
import java.util.concurrent.TimeUnit

/**
 * One authenticated transfer request. The transport only writes headers and
 * bytes; it never decides retry, sequence, or transfer state.
 */
data class TransferTransportRequest(
    val method: String,
    val endpoint: String,
    val body: ByteArray?,
    val contentType: String?,
    val sequence: Long,
    val nonce: ByteArray,
    val timestamp: Double,
    val contentSha256: ByteArray,
    val signature: ByteArray,
    val maxResponseBytes: Int = OkHttpTransferTransport.MAX_RESPONSE_BYTES
) {
    val contentLength: Long get() = body?.size?.toLong() ?: 0L
}

data class RawTransferHttpResponse(
    val httpStatus: Int,
    val bodyText: String
)

interface TransferTransport {
    fun execute(request: TransferTransportRequest): RawTransferHttpResponse

    /** Cancels the in-flight call, if any, so a user cancellation is prompt. */
    fun cancelActiveCall()

    fun close()
}

interface TransferTransportFactory {
    fun create(credentials: SessionCredentials): TransferTransport
}

/**
 * Pinned TLS 1.3 transport. Redirects and automatic retries are disabled so a
 * request can never be silently replayed to a different endpoint or host.
 */
class OkHttpTransferTransport(
    private val hostIp: String,
    private val port: Int,
    private val certFingerprint: String,
    private val sessionId: String,
    private val bearerToken: String,
    private val connectTimeoutSeconds: Long = 10,
    private val writeTimeoutSeconds: Long = 30,
    private val readTimeoutSeconds: Long = 30,
    private val callTimeoutSeconds: Long = 45
) : TransferTransport {
    private val baseUrl = "https://$hostIp:$port"

    private val client: OkHttpClient = run {
        val (socketFactory, trustManager, hostnameVerifier) =
            TlsPinnerFactory.createTlsComponents(certFingerprint, hostIp)
        OkHttpClient.Builder()
            .sslSocketFactory(socketFactory, trustManager)
            .hostnameVerifier(hostnameVerifier)
            .connectTimeout(connectTimeoutSeconds, TimeUnit.SECONDS)
            .writeTimeout(writeTimeoutSeconds, TimeUnit.SECONDS)
            .readTimeout(readTimeoutSeconds, TimeUnit.SECONDS)
            .callTimeout(callTimeoutSeconds, TimeUnit.SECONDS)
            .followRedirects(false)
            .followSslRedirects(false)
            .retryOnConnectionFailure(false)
            .build()
    }

    @Volatile
    private var activeCall: okhttp3.Call? = null

    override fun execute(request: TransferTransportRequest): RawTransferHttpResponse {
        val body = when {
            request.body != null ->
                request.body.toRequestBody(request.contentType?.toMediaType())
            request.method == "POST" || request.method == "PUT" ->
                ByteArray(0).toRequestBody(null)
            else -> null
        }

        val builder = Request.Builder()
            .url(baseUrl + request.endpoint)
            .header("Authorization", "Bearer $bearerToken")
            .header("X-Kivo-Protocol", TransferCrypto.PROTOCOL_VERSION)
            .header("X-Kivo-Session", sessionId)
            .header("X-Kivo-Sequence", request.sequence.toString())
            .header("X-Kivo-Nonce", TransferCrypto.encodeNonceB64(request.nonce))
            .header("X-Kivo-Timestamp", formatTimestamp(request.timestamp))
            .header("X-Kivo-Content-SHA256", TransferCrypto.toHex(request.contentSha256))
            .header("X-Kivo-Signature", TransferCrypto.toHex(request.signature))
            .header("Accept", "application/json")

        if (body == null) {
            builder.get()
        } else {
            builder.method(request.method, body)
        }

        val call = client.newCall(builder.build())
        activeCall = call
        try {
            call.execute().use { response ->
                val text = readBounded(response.body, request.maxResponseBytes)
                return RawTransferHttpResponse(response.code, text)
            }
        } finally {
            activeCall = null
        }
    }

    override fun cancelActiveCall() {
        try {
            activeCall?.cancel()
        } catch (e: Exception) {
            // Best effort; the coordinator also observes its cancellation flag.
        }
    }

    override fun close() {
        cancelActiveCall()
        try {
            client.dispatcher.executorService.shutdown()
        } catch (e: Exception) {
            // Nothing to release.
        }
        try {
            client.connectionPool.evictAll()
        } catch (e: Exception) {
            // Nothing to release.
        }
    }

    private fun readBounded(body: ResponseBody?, limit: Int): String {
        if (body == null) return ""
        require(limit > 0) { "response limit must be positive" }
        val out = ByteArrayOutputStream()
        val buffer = ByteArray(8192)
        body.source().use { source ->
            while (true) {
                val read = source.read(buffer)
                if (read < 0) break
                out.write(buffer, 0, read)
                if (out.size() > limit) {
                    throw TransferNetworkException(
                        "response_too_large",
                        "Desktop response exceeded the bounded response size"
                    )
                }
            }
        }
        return out.toString(Charsets.UTF_8.name())
    }

    companion object {
        const val MAX_RESPONSE_BYTES = 64 * 1024

        fun formatTimestamp(value: Double): String =
            BigDecimal.valueOf(value).stripTrailingZeros().toPlainString()
    }
}

class OkHttpTransferTransportFactory(
    private val connectTimeoutSeconds: Long = 10,
    private val readTimeoutSeconds: Long = 30
) : TransferTransportFactory {
    override fun create(credentials: SessionCredentials): TransferTransport {
        return OkHttpTransferTransport(
            hostIp = credentials.hostIp,
            port = credentials.port,
            certFingerprint = credentials.certFingerprint,
            sessionId = credentials.sessionId,
            bearerToken = credentials.bearerToken,
            connectTimeoutSeconds = connectTimeoutSeconds,
            readTimeoutSeconds = readTimeoutSeconds
        )
    }
}

/**
 * Serializes authenticated requests for one paired session. The desktop keeps
 * one replay window shared by uploads and downloads, so an upload chunk and a
 * download chunk must never be in flight concurrently.
 */
class TransferRequestGate {
    private val lock = java.util.concurrent.locks.ReentrantLock()

    fun <T> withPermit(block: () -> T): T {
        lock.lock()
        try {
            return block()
        } finally {
            lock.unlock()
        }
    }
}

/**
 * Authenticated transfer client. Every call reserves and persists a fresh
 * monotonic sequence before transmitting, uses a fresh 16-byte nonce, and
 * verifies the complete signed response envelope with `K_down`.
 *
 * Signed error responses are returned to the caller (they are authenticated);
 * unsigned bodies, malformed envelopes, and transport failures raise.
 */
class TransferHttpClient(
    private val credentials: SessionCredentials,
    private val sequences: TransferSequenceManager,
    private val transport: TransferTransport,
    private val nonceSource: () -> ByteArray = { ByteArray(16).also(SecureRandomHolder.random::nextBytes) },
    private val clockMillis: () -> Long = { System.currentTimeMillis() },
    private val requestGate: TransferRequestGate? = null
) {
    companion object {
        private val EMPTY_BODY = ByteArray(0)
        private val TRANSFER_ID_PATTERN = Regex("^tr_[0-9a-f]{24}$")
        const val MAX_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
        const val MAX_DOWNLOAD_PAYLOAD_BYTES = 1_572_864
        const val MAX_DOWNLOAD_RESPONSE_BYTES = 2 * 1024 * 1024
    }

    fun init(filename: String, mime: String?, size: Long, sha256Hex: String): VerifiedTransferResponse {
        val body = TransferCrypto.buildInitBody(filename, mime, size, sha256Hex)
        return send(
            context = "upload_init",
            method = "POST",
            endpoint = "/api/v1/transfers/uploads/init",
            body = body,
            contentType = "application/json; charset=utf-8"
        )
    }

    fun status(transferId: String): VerifiedTransferResponse {
        require(TRANSFER_ID_PATTERN.matches(transferId)) { "invalid transfer id" }
        return send(
            context = "upload_status",
            method = "GET",
            endpoint = "/api/v1/transfers/uploads/$transferId",
            body = null,
            contentType = null
        )
    }

    fun sendChunk(transferId: String, chunkIndex: Long, data: ByteArray): VerifiedTransferResponse {
        require(TRANSFER_ID_PATTERN.matches(transferId)) { "invalid transfer id" }
        require(chunkIndex >= 0L) { "invalid chunk index" }
        require(data.size <= TransferValidation.CHUNK_BYTES.toInt()) { "chunk too large" }
        return send(
            context = "upload_chunk",
            method = "PUT",
            endpoint = "/api/v1/transfers/uploads/$transferId/chunks/$chunkIndex",
            body = data,
            contentType = "application/octet-stream"
        )
    }

    fun cancel(transferId: String): VerifiedTransferResponse {
        require(TRANSFER_ID_PATTERN.matches(transferId)) { "invalid transfer id" }
        return send(
            context = "upload_cancel",
            method = "POST",
            endpoint = "/api/v1/transfers/uploads/$transferId/cancel",
            body = EMPTY_BODY,
            contentType = null
        )
    }

    fun downloadInit(ticketId: String): VerifiedTransferResponse {
        require(TransferCrypto.TICKET_ID_PATTERN.matches(ticketId)) { "invalid download ticket" }
        return send(
            context = "download_init",
            method = "POST",
            endpoint = "/api/v1/transfers/downloads/init",
            body = TransferCrypto.buildDownloadInitBody(ticketId),
            contentType = "application/json; charset=utf-8"
        )
    }

    fun downloadStatus(ticketId: String): VerifiedTransferResponse = sendDownload(
        ticketId = ticketId,
        context = "download_status",
        method = "GET",
        suffix = ""
    )

    fun downloadChunk(ticketId: String, chunkIndex: Long): VerifiedTransferResponse {
        require(TransferCrypto.TICKET_ID_PATTERN.matches(ticketId)) { "invalid download ticket" }
        require(chunkIndex in 0L..Integer.MAX_VALUE.toLong()) { "invalid chunk index" }
        return send(
            context = "download_chunk",
            method = "GET",
            endpoint = "/api/v1/transfers/downloads/$ticketId/chunks/$chunkIndex",
            body = null,
            contentType = null,
            maxResponseBytes = MAX_DOWNLOAD_RESPONSE_BYTES,
            maxPayloadBytes = MAX_DOWNLOAD_PAYLOAD_BYTES
        )
    }

    fun downloadComplete(ticketId: String): VerifiedTransferResponse = sendDownload(
        ticketId = ticketId,
        context = "download_complete",
        method = "POST",
        suffix = "/complete"
    )

    fun downloadCancel(ticketId: String): VerifiedTransferResponse = sendDownload(
        ticketId = ticketId,
        context = "download_cancel",
        method = "POST",
        suffix = "/cancel"
    )

    private fun sendDownload(
        ticketId: String,
        context: String,
        method: String,
        suffix: String
    ): VerifiedTransferResponse {
        require(TransferCrypto.TICKET_ID_PATTERN.matches(ticketId)) { "invalid download ticket" }
        return send(
            context = context,
            method = method,
            endpoint = "/api/v1/transfers/downloads/$ticketId$suffix",
            body = EMPTY_BODY,
            contentType = null
        )
    }

    fun close() {
        transport.close()
    }

    fun cancelActiveCall() {
        transport.cancelActiveCall()
    }

    private fun send(
        context: String,
        method: String,
        endpoint: String,
        body: ByteArray?,
        contentType: String?,
        maxResponseBytes: Int = OkHttpTransferTransport.MAX_RESPONSE_BYTES,
        maxPayloadBytes: Int = TransferResponseVerifier.MAX_PAYLOAD_BYTES
    ): VerifiedTransferResponse {
        val gate = requestGate
        return if (gate == null) {
            sendLocked(context, method, endpoint, body, contentType, maxResponseBytes, maxPayloadBytes)
        } else {
            gate.withPermit {
                sendLocked(context, method, endpoint, body, contentType, maxResponseBytes, maxPayloadBytes)
            }
        }
    }

    private fun sendLocked(
        context: String,
        method: String,
        endpoint: String,
        body: ByteArray?,
        contentType: String?,
        maxResponseBytes: Int,
        maxPayloadBytes: Int
    ): VerifiedTransferResponse {
        val sequence = sequences.reserve(credentials.sessionId)
        val nonce = nonceSource()
        require(nonce.size == TransferCrypto.NONCE_BYTES) { "nonce must be 16 bytes" }
        val timestamp = clockMillis().toDouble() / 1000.0
        val bytes = body ?: EMPTY_BODY
        val contentSha256 = TransferCrypto.sha256(bytes)

        val canonical = TransferCrypto.buildCanonicalRequest(
            version = TransferCrypto.PROTOCOL_VERSION,
            sessionId = credentials.sessionId,
            method = method,
            endpoint = endpoint,
            sequence = sequence,
            nonce = nonce,
            timestamp = timestamp,
            contentLength = bytes.size.toLong(),
            contentSha256 = contentSha256
        )
        val signature = TransferCrypto.computeRequestSignature(credentials.upstreamKey, canonical)

        val raw = try {
            transport.execute(
                TransferTransportRequest(
                    method = method,
                    endpoint = endpoint,
                    body = body,
                    contentType = contentType,
                    sequence = sequence,
                    nonce = nonce,
                    timestamp = timestamp,
                    contentSha256 = contentSha256,
                    signature = signature,
                    maxResponseBytes = maxResponseBytes
                )
            )
        } catch (e: IOException) {
            throw TransferNetworkException("network_error", e.message ?: "Network connection lost", e)
        }

        val envelope = try {
            JSONObject(raw.bodyText)
        } catch (e: Exception) {
            throw TransferNetworkException(
                "invalid_response",
                "Desktop returned a response that is not a JSON object",
                e
            )
        }

        if (!envelope.has("sig")) {
            val code = sanitizeCode(envelope.optString("error_code", ""))
            val message = sanitizeMessage(envelope.optString("error_message", ""))
            if (raw.httpStatus >= 500) {
                throw TransferNetworkException(
                    "unsigned_server_error",
                    "Desktop returned HTTP ${raw.httpStatus}"
                )
            }
            throw TransferProtocolException(
                code.ifEmpty { "unsigned_response" },
                message.ifEmpty { "Desktop returned an unsigned transfer response" },
                retryable = false,
                terminal = true
            )
        }

        return TransferResponseVerifier.verify(
            envelope = envelope,
            downstreamKey = credentials.downstreamKey,
            expectedContext = context,
            sessionId = credentials.sessionId,
            sequence = sequence,
            requestNonce = nonce,
            actualHttpStatus = raw.httpStatus,
            maxPayloadBytes = maxPayloadBytes
        )
    }

    private fun sanitizeCode(value: String): String =
        if (value.length in 1..64 && value.all { it.isLetterOrDigit() || it == '_' || it == '-' }) {
            value
        } else {
            ""
        }

    private fun sanitizeMessage(value: String): String =
        value.take(256).filter { it == '\t' || it >= ' ' }

    private object SecureRandomHolder {
        val random = java.security.SecureRandom()
    }
}
