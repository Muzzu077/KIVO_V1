package com.kivo.companion

import com.kivo.companion.pairing.QrParser
import com.kivo.companion.pairing.QrParsingException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.net.URLEncoder

class QrParserTest {

    private fun makeValidUri(
        addr: String = "192.168.1.7:8443",
        fp: String = "E1CE2A45791117A9124F26201FF40CA4C13956CCF61D0D81EAF1A3DF6B047B21",
        t: String = "AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA",
        id: String = "sess_0123456789abcdef",
        sn: String = "oKGio6SlpqeoqaqrrK2ur7CxsrO0tba3uLm6u7y9vr8",
        exp: Long = (System.currentTimeMillis() / 1000) + 300
    ): String {
        return "kivo-pair://v1?addr=${URLEncoder.encode(addr, "UTF-8")}&fp=$fp&t=$t&id=$id&sn=$sn&exp=$exp"
    }

    @Test
    fun testValidQrUriParsesSuccessfully() {
        val uri = makeValidUri()
        val offer = QrParser.parse(uri)

        assertEquals("v1", offer.version)
        assertEquals("192.168.1.7", offer.hostIp)
        assertEquals(8443, offer.port)
        assertEquals("E1CE2A45791117A9124F26201FF40CA4C13956CCF61D0D81EAF1A3DF6B047B21", offer.certFingerprint)
        assertEquals("sess_0123456789abcdef", offer.sessionId)
        assertEquals(32, offer.pairingSecret.size)
        assertEquals(32, offer.serverNonce.size)
    }

    @Test
    fun testRejectsInvalidScheme() {
        val uri = makeValidUri().replace("kivo-pair://", "https://")
        try {
            QrParser.parse(uri)
            fail("Expected QrParsingException")
        } catch (e: QrParsingException) {
            assertEquals("invalid_scheme", e.code)
        }
    }

    @Test
    fun testRejectsUnsupportedVersion() {
        val uri = makeValidUri().replace("kivo-pair://v1", "kivo-pair://v2")
        try {
            QrParser.parse(uri)
            fail("Expected QrParsingException")
        } catch (e: QrParsingException) {
            assertEquals("unsupported_version", e.code)
        }
    }

    @Test
    fun testRejectsPublicIp() {
        val uri = makeValidUri(addr = "8.8.8.8:8443")
        try {
            QrParser.parse(uri)
            fail("Expected QrParsingException")
        } catch (e: QrParsingException) {
            assertEquals("public_ip_rejected", e.code)
        }
    }

    @Test
    fun testRejectsLoopbackIp() {
        val uri = makeValidUri(addr = "127.0.0.1:8443")
        try {
            QrParser.parse(uri)
            fail("Expected QrParsingException")
        } catch (e: QrParsingException) {
            assertEquals("loopback_rejected", e.code)
        }
    }

    @Test
    fun testRejectsInvalidPort() {
        val uri = makeValidUri(addr = "192.168.1.7:80")
        try {
            QrParser.parse(uri)
            fail("Expected QrParsingException")
        } catch (e: QrParsingException) {
            assertEquals("invalid_port", e.code)
        }
    }

    @Test
    fun testRejectsInvalidFingerprintLength() {
        val uri = makeValidUri(fp = "AABBCC")
        try {
            QrParser.parse(uri)
            fail("Expected QrParsingException")
        } catch (e: QrParsingException) {
            assertEquals("invalid_fingerprint", e.code)
        }
    }

    @Test
    fun testRejectsExpiredOffer() {
        val pastTime = (System.currentTimeMillis() / 1000) - 60
        val uri = makeValidUri(exp = pastTime)
        try {
            QrParser.parse(uri)
            fail("Expected QrParsingException")
        } catch (e: QrParsingException) {
            assertEquals("offer_expired", e.code)
        }
    }

    @Test
    fun testRejectsDuplicateParameters() {
        val uri = makeValidUri() + "&addr=192.168.1.8:8443"
        try {
            QrParser.parse(uri)
            fail("Expected QrParsingException")
        } catch (e: QrParsingException) {
            assertEquals("duplicate_param", e.code)
        }
    }
}
