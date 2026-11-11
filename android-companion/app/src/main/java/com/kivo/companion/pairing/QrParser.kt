package com.kivo.companion.pairing

import java.net.Inet4Address
import java.net.InetAddress
import java.net.URI
import java.net.URLDecoder
import java.util.Base64
import com.kivo.companion.model.PairingOffer

class QrParsingException(val code: String, message: String) : Exception(message)

/**
 * Validates and decodes the kivo-pair:// URI payload scanned from the desktop QR code.
 */
object QrParser {
    private const val EXPECTED_SCHEME = "kivo-pair"
    private const val EXPECTED_VERSION = "v1"
    private const val MAX_URI_LENGTH = 1024
    private val FINGERPRINT_REGEX = Regex("^[0-9A-Fa-f]{64}$")
    private val SESSION_ID_REGEX = Regex("^[A-Za-z0-9_.-]{1,64}$")

    fun parse(rawUriString: String): PairingOffer {
        if (rawUriString.length > MAX_URI_LENGTH) {
            throw QrParsingException("uri_too_long", "QR payload exceeds maximum length of $MAX_URI_LENGTH")
        }

        val uri = try {
            URI(rawUriString)
        } catch (e: Exception) {
            throw QrParsingException("malformed_uri", "Failed to parse URI: ${e.message}")
        }

        if (uri.scheme != EXPECTED_SCHEME) {
            throw QrParsingException(
                "invalid_scheme",
                "Unsupported URI scheme '${uri.scheme}'; expected '$EXPECTED_SCHEME'"
            )
        }

        // Host must equal expected version (e.g. "v1")
        val hostVersion = uri.host ?: ""
        if (hostVersion != EXPECTED_VERSION) {
            throw QrParsingException(
                "unsupported_version",
                "Unsupported protocol version '$hostVersion'; expected '$EXPECTED_VERSION'"
            )
        }

        val rawQuery = uri.rawQuery ?: ""
        val queryParams = parseQueryParams(rawQuery)

        val requiredParams = setOf("addr", "fp", "t", "id", "sn", "exp")
        val missing = requiredParams - queryParams.keys
        if (missing.isNotEmpty()) {
            throw QrParsingException("missing_params", "Missing required query parameters: $missing")
        }

        // Validate no unknown parameters
        val unknown = queryParams.keys - requiredParams - setOf("v")
        if (unknown.isNotEmpty()) {
            throw QrParsingException("unexpected_params", "Unexpected query parameters: $unknown")
        }

        // 1. Endpoint address (IP:PORT)
        val addrStr = queryParams["addr"] ?: ""
        val (hostIp, port) = parseAndValidateEndpoint(addrStr)

        // 2. Certificate fingerprint (64 hex characters)
        val fpStr = (queryParams["fp"] ?: "").uppercase()
        if (!FINGERPRINT_REGEX.matches(fpStr)) {
            throw QrParsingException("invalid_fingerprint", "Certificate fingerprint must be a 64-hex SHA-256 string")
        }

        // 3. Pairing secret (32 bytes base64url)
        val tStr = queryParams["t"] ?: ""
        val secretBytes = try {
            Base64.getUrlDecoder().decode(tStr + "==".substring(0, (4 - tStr.length % 4) % 4))
        } catch (e: Exception) {
            throw QrParsingException("invalid_secret", "Pairing secret is not valid base64url")
        }
        if (secretBytes.size != 32) {
            throw QrParsingException("invalid_secret_length", "Pairing secret must be exactly 32 bytes")
        }

        // 4. Session ID
        val idStr = queryParams["id"] ?: ""
        if (!SESSION_ID_REGEX.matches(idStr)) {
            throw QrParsingException("invalid_session_id", "Session ID contains invalid characters or length")
        }

        // 5. Server nonce (32 bytes base64url)
        val snStr = queryParams["sn"] ?: ""
        val nonceBytes = try {
            Base64.getUrlDecoder().decode(snStr + "==".substring(0, (4 - snStr.length % 4) % 4))
        } catch (e: Exception) {
            throw QrParsingException("invalid_server_nonce", "Server nonce is not valid base64url")
        }
        if (nonceBytes.size != 32) {
            throw QrParsingException("invalid_nonce_length", "Server nonce must be exactly 32 bytes")
        }

        // 6. Expiry timestamp
        val expStr = queryParams["exp"] ?: ""
        val expiresAt = expStr.toLongOrNull()
            ?: throw QrParsingException("invalid_expiry", "Expiration parameter must be an integer epoch timestamp")

        val nowSeconds = System.currentTimeMillis() / 1000
        if (expiresAt <= nowSeconds) {
            throw QrParsingException("offer_expired", "QR pairing offer has expired")
        }

        return PairingOffer(
            version = EXPECTED_VERSION,
            hostIp = hostIp,
            port = port,
            certFingerprint = fpStr,
            pairingSecret = secretBytes,
            sessionId = idStr,
            serverNonce = nonceBytes,
            expiresAt = expiresAt
        )
    }

    private fun parseQueryParams(query: String): Map<String, String> {
        val map = mutableMapOf<String, String>()
        if (query.isBlank()) return map
        val pairs = query.split("&")
        for (pair in pairs) {
            val idx = pair.indexOf("=")
            if (idx > 0) {
                val key = URLDecoder.decode(pair.substring(0, idx), "UTF-8")
                val value = URLDecoder.decode(pair.substring(idx + 1), "UTF-8")
                if (map.containsKey(key)) {
                    throw QrParsingException("duplicate_param", "Duplicate query parameter: $key")
                }
                map[key] = value
            }
        }
        return map
    }

    private fun parseAndValidateEndpoint(addr: String): Pair<String, Int> {
        val parts = addr.split(":")
        if (parts.size != 2) {
            throw QrParsingException("invalid_endpoint", "Address must be in host:port format")
        }
        val ipStr = parts[0].trim()
        val port = parts[1].trim().toIntOrNull()
            ?: throw QrParsingException("invalid_port", "Port must be an integer")

        if (port !in 1024..65535) {
            throw QrParsingException("invalid_port", "Port must be in non-privileged range 1024..65535")
        }

        // Validate IP is a private IPv4 LAN address
        val inetAddr = try {
            InetAddress.getByName(ipStr)
        } catch (e: Exception) {
            throw QrParsingException("invalid_ip", "Invalid IP address syntax: $ipStr")
        }

        if (inetAddr !is Inet4Address) {
            throw QrParsingException("ipv6_not_supported", "Only IPv4 LAN addresses are supported in this stage")
        }

        if (inetAddr.isLoopbackAddress) {
            throw QrParsingException("loopback_rejected", "Loopback addresses are not permitted")
        }
        if (inetAddr.isLinkLocalAddress) {
            throw QrParsingException("link_local_rejected", "Link-local addresses are not permitted")
        }
        if (inetAddr.isMulticastAddress) {
            throw QrParsingException("multicast_rejected", "Multicast addresses are not permitted")
        }
        if (!inetAddr.isSiteLocalAddress) {
            throw QrParsingException("public_ip_rejected", "Only RFC 1918 private LAN addresses are permitted")
        }

        return Pair(ipStr, port)
    }
}
