package com.kivo.companion.transfer

import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.util.Locale

class DownloadTicketException(
    val code: String,
    message: String
) : IllegalArgumentException(message)

/**
 * Validated offer from a `kivo-download://v1` QR handoff.
 *
 * The ticket is only a reference. It never authorizes anything by itself: the
 * desktop still requires the paired bearer token, a valid `K_up` request MAC,
 * a fresh sequence/nonce, and the ticket's bound session/device.
 */
data class DownloadOffer(
    val ticketId: String,
    val deviceId: String,
    val filename: String,
    val size: Long,
    val sha256Hex: String
)

/**
 * Strict parser for the Stage 13.3.3.4 handoff. Mirrors
 * `kivo/bridge/download_handoff.py` byte-for-byte and never accepts a bare
 * ticket ID, a filesystem path, a duplicate parameter, or an unknown version.
 */
object DownloadTicketParser {
    const val SCHEME = "kivo-download"
    const val VERSION = "v1"
    const val PREFIX = "$SCHEME://$VERSION?"
    const val MAX_PAYLOAD_BYTES = 2048
    const val MAX_DOWNLOAD_BYTES = 2L * 1024L * 1024L * 1024L

    private val TICKET = Regex("^tkt_[0-9a-f]{64}$")
    private val DEVICE = Regex("^client_[A-Za-z0-9_.-]{1,57}$")
    private val SHA256 = Regex("^[0-9a-f]{64}$")
    private val INTEGER = Regex("^(0|[1-9][0-9]*)$")
    private val REQUIRED = setOf("ticket", "device", "name", "size", "sha256")
    private val UNRESERVED =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~".toSet()

    fun parse(raw: String, expectedDeviceId: String? = null): DownloadOffer {
        if (raw.isBlank()) fail("invalid_handoff", "The download handoff is missing")
        if (raw != raw.trim()) fail("invalid_handoff", "The download handoff has surrounding whitespace")
        if (raw.toByteArray(Charsets.UTF_8).size > MAX_PAYLOAD_BYTES) {
            fail("handoff_too_large", "The download handoff is too large")
        }
        if (raw.startsWith("$SCHEME://") && !raw.startsWith(PREFIX)) {
            fail("unsupported_version", "The download handoff version is not supported")
        }
        if (!raw.startsWith(PREFIX)) fail("invalid_handoff", "The download handoff scheme is invalid")
        if (raw.contains('#')) fail("invalid_handoff", "The download handoff must not contain a fragment")

        val query = raw.substring(PREFIX.length)
        if (query.isEmpty()) fail("invalid_handoff", "The download handoff has no parameters")

        val parsed = LinkedHashMap<String, String>()
        for (part in query.split('&')) {
            if (part.isEmpty() || part.count { it == '=' } != 1) {
                fail("invalid_handoff", "The download handoff is malformed")
            }
            val separator = part.indexOf('=')
            val rawKey = part.substring(0, separator)
            val rawValue = part.substring(separator + 1)
            if (rawKey.isEmpty() || rawValue.isEmpty()) {
                fail("invalid_handoff", "The download handoff is malformed")
            }
            val key = decodeComponent(rawKey)
            val value = decodeComponent(rawValue)
            if (parsed.put(key, value) != null) {
                fail("duplicate_parameter", "The download handoff contains a duplicate parameter")
            }
        }
        if (parsed.keys != REQUIRED) {
            fail("invalid_handoff", "The download handoff parameters are invalid")
        }

        val ticket = parsed.getValue("ticket")
        if (!TICKET.matches(ticket)) fail("invalid_ticket", "The download ticket is malformed")
        val device = parsed.getValue("device")
        if (!DEVICE.matches(device)) fail("invalid_device", "The target device identifier is malformed")
        val filename = try {
            TransferValidation.validateDisplayFilename(parsed.getValue("name"))
        } catch (e: TransferValidationException) {
            fail("invalid_filename", "The display filename is invalid")
        }
        val sizeText = parsed.getValue("size")
        if (!INTEGER.matches(sizeText)) fail("invalid_size", "The file size is invalid")
        val size = sizeText.toLongOrNull()
            ?: fail("invalid_size", "The file size is invalid")
        if (size !in 0L..MAX_DOWNLOAD_BYTES) fail("invalid_size", "The file size is out of range")
        val sha = parsed.getValue("sha256")
        if (!SHA256.matches(sha)) fail("invalid_sha256", "The file digest is malformed")

        if (expectedDeviceId != null) {
            if (!DEVICE.matches(expectedDeviceId)) {
                fail("invalid_device", "The local device identifier is malformed")
            }
            if (device != expectedDeviceId) {
                fail("handoff_device_mismatch", "The download was authorized for a different paired device")
            }
        }
        return DownloadOffer(
            ticketId = ticket,
            deviceId = device,
            filename = filename,
            size = size,
            sha256Hex = sha.lowercase(Locale.ROOT)
        )
    }

    private fun decodeComponent(value: String): String {
        val out = ByteArrayOutputStream(value.length)
        var index = 0
        while (index < value.length) {
            val char = value[index]
            if (char == '%') {
                if (index + 2 >= value.length) {
                    fail("invalid_handoff", "The download handoff encoding is invalid")
                }
                val high = hexDigit(value[index + 1])
                val low = hexDigit(value[index + 2])
                if (high < 0 || low < 0) {
                    fail("invalid_handoff", "The download handoff encoding is invalid")
                }
                out.write((high shl 4) or low)
                index += 3
                continue
            }
            if (char !in UNRESERVED) {
                fail("invalid_handoff", "The download handoff encoding is invalid")
            }
            out.write(char.code)
            index += 1
        }
        return try {
            Charsets.UTF_8.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(out.toByteArray()))
                .toString()
        } catch (e: Exception) {
            fail("invalid_handoff", "The download handoff is not valid UTF-8")
        }
    }

    private fun hexDigit(value: Char): Int = when (value) {
        in '0'..'9' -> value - '0'
        in 'a'..'f' -> value - 'a' + 10
        in 'A'..'F' -> value - 'A' + 10
        else -> -1
    }

    private fun fail(code: String, message: String): Nothing =
        throw DownloadTicketException(code, message)
}
