package com.kivo.companion.crypto

import java.net.InetAddress
import java.net.Socket
import java.security.MessageDigest
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import javax.net.ssl.HostnameVerifier
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLEngine
import javax.net.ssl.SSLSocket
import javax.net.ssl.SSLSocketFactory
import javax.net.ssl.TrustManager
import javax.net.ssl.X509ExtendedTrustManager

class TlsPinningException(message: String) : CertificateException(message)

/**
 * Strict TLS 1.3 pinning TrustManager validating the ephemeral leaf certificate fingerprint.
 */
class PinnedCertificateTrustManager(
    private val expectedFingerprintHex: String,
    private val expectedLanIp: String
) : X509ExtendedTrustManager() {

    override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        throw CertificateException("Client authentication not supported")
    }

    override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?, socket: Socket?) {
        throw CertificateException("Client authentication not supported")
    }

    override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?, engine: SSLEngine?) {
        throw CertificateException("Client authentication not supported")
    }

    override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        validateServerChain(chain)
    }

    override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?, socket: Socket?) {
        validateServerChain(chain)
    }

    override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?, engine: SSLEngine?) {
        validateServerChain(chain)
    }

    override fun getAcceptedIssuers(): Array<X509Certificate> {
        return emptyArray()
    }

    fun validateServerChain(chain: Array<out X509Certificate>?) {
        if (chain == null || chain.isEmpty()) {
            throw TlsPinningException("Empty server certificate chain")
        }

        val leaf = chain[0]

        // 1. Check validity dates
        try {
            leaf.checkValidity()
        } catch (e: Exception) {
            throw TlsPinningException("Server certificate is expired or not yet valid: ${e.message}")
        }

        // 2. Compute SHA-256 over leaf DER encoding
        val md = MessageDigest.getInstance("SHA-256")
        val derBytes = leaf.encoded
        val actualFpBytes = md.digest(derBytes)
        val actualFpHex = actualFpBytes.joinToString("") { "%02X".format(it) }

        val cleanExpected = expectedFingerprintHex.replace(":", "").replace(" ", "").uppercase()
        val expectedBytes = hexStringToByteArray(cleanExpected)

        // 3. Constant-time fingerprint comparison
        if (!MessageDigest.isEqual(actualFpBytes, expectedBytes)) {
            throw TlsPinningException(
                "Certificate fingerprint mismatch. Expected $cleanExpected, got $actualFpHex"
            )
        }

        // 4. Verify Subject Alternative Name contains expected LAN IP
        val sanEntries = leaf.subjectAlternativeNames
        var ipMatched = false
        if (sanEntries != null) {
            for (entry in sanEntries) {
                // Type 7 is iPAddress in generalName RFC 5280
                val type = entry[0] as? Int
                val value = entry[1] as? String
                if (type == 7 && value != null) {
                    if (value.equals(expectedLanIp, ignoreCase = true)) {
                        ipMatched = true
                        break
                    }
                }
            }
        }

        if (!ipMatched) {
            throw TlsPinningException("Certificate Subject Alternative Name does not match IP $expectedLanIp")
        }

        // 5. Check chain length
        if (chain.size > 2) {
            throw TlsPinningException("Unexpected certificate chain length: ${chain.size}")
        }

        // 6. Check Key Usage if present
        val ku = leaf.keyUsage
        if (ku != null) {
            val hasValidUsage = (ku.isNotEmpty() && ku[0]) || (ku.size > 2 && ku[2]) || (ku.size > 4 && ku[4])
            if (!hasValidUsage) {
                throw TlsPinningException("Certificate key usage does not permit TLS key exchange or signatures")
            }
        }

        // 7. Check Extended Key Usage if present (serverAuth: 1.3.6.1.5.5.7.3.1)
        val eku = leaf.extendedKeyUsage
        if (eku != null) {
            if (!eku.contains("1.3.6.1.5.5.7.3.1") && !eku.contains("2.5.29.37.0")) {
                throw TlsPinningException("Certificate extended key usage does not permit TLS server authentication")
            }
        }
    }

    private fun hexStringToByteArray(s: String): ByteArray {
        val len = s.length
        val data = ByteArray(len / 2)
        var i = 0
        while (i < len) {
            data[i / 2] = ((Character.digit(s[i], 16) shl 4) + Character.digit(s[i + 1], 16)).toByte()
            i += 2
        }
        return data
    }
}

/**
 * SocketFactory enforcing TLS 1.3 only.
 */
class Tls13SocketFactory(
    private val delegate: SSLSocketFactory
) : SSLSocketFactory() {

    override fun getDefaultCipherSuites(): Array<String> = delegate.defaultCipherSuites
    override fun getSupportedCipherSuites(): Array<String> = delegate.supportedCipherSuites

    override fun createSocket(): Socket = patch(delegate.createSocket())
    override fun createSocket(s: Socket?, host: String?, port: Int, autoClose: Boolean): Socket =
        patch(delegate.createSocket(s, host, port, autoClose))
    override fun createSocket(host: String?, port: Int): Socket = patch(delegate.createSocket(host, port))
    override fun createSocket(host: String?, port: Int, localHost: InetAddress?, localPort: Int): Socket =
        patch(delegate.createSocket(host, port, localHost, localPort))
    override fun createSocket(host: InetAddress?, port: Int): Socket = patch(delegate.createSocket(host, port))
    override fun createSocket(address: InetAddress?, port: Int, localAddress: InetAddress?, localPort: Int): Socket =
        patch(delegate.createSocket(address, port, localAddress, localPort))

    private fun patch(socket: Socket): Socket {
        if (socket is SSLSocket) {
            socket.enabledProtocols = arrayOf("TLSv1.3")
        }
        return socket
    }
}

/**
 * HostnameVerifier ensuring the connected IP matches expected IP.
 */
class PinnedHostnameVerifier(
    private val expectedIp: String
) : HostnameVerifier {
    override fun verify(hostname: String?, session: javax.net.ssl.SSLSession?): Boolean {
        if (hostname == null) return false
        return hostname.equals(expectedIp, ignoreCase = true)
    }
}

object TlsPinnerFactory {
    fun createTlsComponents(
        fingerprintHex: String,
        lanIp: String
    ): Triple<SSLSocketFactory, PinnedCertificateTrustManager, HostnameVerifier> {
        val trustManager = PinnedCertificateTrustManager(fingerprintHex, lanIp)
        val sslContext = SSLContext.getInstance("TLSv1.3")
        sslContext.init(null, arrayOf<TrustManager>(trustManager), null)
        val socketFactory = Tls13SocketFactory(sslContext.socketFactory)
        val hostnameVerifier = PinnedHostnameVerifier(lanIp)
        return Triple(socketFactory, trustManager, hostnameVerifier)
    }
}
