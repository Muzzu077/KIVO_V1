package com.kivo.companion

import com.kivo.companion.crypto.PinnedCertificateTrustManager
import com.kivo.companion.crypto.PinnedHostnameVerifier
import com.kivo.companion.crypto.TlsPinningException
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.File
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate

class TlsPinnerTest {

    private lateinit var certFactory: CertificateFactory
    private lateinit var testCertsJson: JSONObject

    @Before
    fun setUp() {
        certFactory = CertificateFactory.getInstance("X.509")
        // Load test certificate fixtures generated from Python
        val fixtureFile = File("../../tests/fixtures/tls_test_certs.json")
        val jsonStr = fixtureFile.readText(Charsets.UTF_8)
        testCertsJson = JSONObject(jsonStr)
    }

    private fun parseCert(hexKey: String): X509Certificate {
        val hex = testCertsJson.getString(hexKey)
        val der = hexToBytes(hex)
        return certFactory.generateCertificate(ByteArrayInputStream(der)) as X509Certificate
    }

    private fun hexToBytes(hex: String): ByteArray {
        val len = hex.length
        val data = ByteArray(len / 2)
        var i = 0
        while (i < len) {
            data[i / 2] = ((Character.digit(hex[i], 16) shl 4) + Character.digit(hex[i + 1], 16)).toByte()
            i += 2
        }
        return data
    }

    @Test
    fun testValidCertificateAndFingerprintSucceeds() {
        val cert = parseCert("valid_der")
        val expectedFp = testCertsJson.getString("valid_fp")
        val tm = PinnedCertificateTrustManager(expectedFp, "192.168.1.7")

        // Must succeed without throwing
        tm.validateServerChain(arrayOf(cert))
    }

    @Test
    fun testWrongFingerprintThrows() {
        val cert = parseCert("valid_der")
        val wrongFp = "00" * 32
        val tm = PinnedCertificateTrustManager(wrongFp, "192.168.1.7")

        try {
            tm.validateServerChain(arrayOf(cert))
            fail("Expected TlsPinningException on wrong fingerprint")
        } catch (e: TlsPinningException) {
            assertTrue(e.message?.contains("fingerprint mismatch", ignoreCase = true) == true)
        }
    }

    @Test
    fun testExpiredCertificateThrows() {
        val cert = parseCert("expired_der")
        val expectedFp = testCertsJson.getString("expired_fp")
        val tm = PinnedCertificateTrustManager(expectedFp, "192.168.1.7")

        try {
            tm.validateServerChain(arrayOf(cert))
            fail("Expected TlsPinningException on expired certificate")
        } catch (e: TlsPinningException) {
            assertTrue(e.message?.contains("expired", ignoreCase = true) == true)
        }
    }

    @Test
    fun testFutureCertificateThrows() {
        val cert = parseCert("future_der")
        val expectedFp = testCertsJson.getString("future_fp")
        val tm = PinnedCertificateTrustManager(expectedFp, "192.168.1.7")

        try {
            tm.validateServerChain(arrayOf(cert))
            fail("Expected TlsPinningException on not yet valid certificate")
        } catch (e: TlsPinningException) {
            assertTrue(e.message?.contains("not yet valid", ignoreCase = true) == true)
        }
    }

    @Test
    fun testSanMismatchThrows() {
        val cert = parseCert("san_mismatch_der")
        val expectedFp = testCertsJson.getString("san_mismatch_fp")
        // Cert has SAN 192.168.1.99, but we expect 192.168.1.7
        val tm = PinnedCertificateTrustManager(expectedFp, "192.168.1.7")

        try {
            tm.validateServerChain(arrayOf(cert))
            fail("Expected TlsPinningException on SAN IP mismatch")
        } catch (e: TlsPinningException) {
            assertTrue(e.message?.contains("Subject Alternative Name does not match IP", ignoreCase = true) == true)
        }
    }

    @Test
    fun testMissingSanThrows() {
        val cert = parseCert("no_san_der")
        val expectedFp = testCertsJson.getString("no_san_fp")
        val tm = PinnedCertificateTrustManager(expectedFp, "192.168.1.7")

        try {
            tm.validateServerChain(arrayOf(cert))
            fail("Expected TlsPinningException on missing SAN")
        } catch (e: TlsPinningException) {
            assertTrue(e.message?.contains("Subject Alternative Name does not match IP", ignoreCase = true) == true)
        }
    }

    @Test
    fun testEmptyChainThrows() {
        val tm = PinnedCertificateTrustManager("AA".repeat(32), "192.168.1.7")
        try {
            tm.validateServerChain(emptyArray())
            fail("Expected TlsPinningException on empty chain")
        } catch (e: TlsPinningException) {
            assertTrue(e.message?.contains("Empty server certificate chain", ignoreCase = true) == true)
        }
    }

    @Test
    fun testUnexpectedChainLengthThrows() {
        val cert = parseCert("valid_der")
        val expectedFp = testCertsJson.getString("valid_fp")
        val tm = PinnedCertificateTrustManager(expectedFp, "192.168.1.7")

        try {
            tm.validateServerChain(arrayOf(cert, cert, cert))
            fail("Expected TlsPinningException on excessive chain length")
        } catch (e: TlsPinningException) {
            assertTrue(e.message?.contains("chain length", ignoreCase = true) == true)
        }
    }

    @Test
    fun testHostnameVerifierEnforcesLanIp() {
        val verifier = PinnedHostnameVerifier("192.168.1.7")
        assertTrue(verifier.verify("192.168.1.7", null))
        assertFalse(verifier.verify("192.168.1.8", null))
        assertFalse(verifier.verify("evil.com", null))
        assertFalse(verifier.verify(null, null))
    }
}

private operator fun String.times(n: Int): String = this.repeat(n)
