package com.kivo.companion.transfer

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Strict handoff parsing against the shared Python `kivo-download://v1` vector.
 * The ticket is opaque metadata only; parsing never contacts the desktop.
 */
class DownloadTicketParserTest {

    private val fixture: JSONObject = loadFixture()
    private val payload = fixture.getString("payload")
    private val deviceId = fixture.getString("device_id")

    @Test
    fun `shared handoff vector parses exactly`() {
        val offer = DownloadTicketParser.parse(payload, deviceId)
        assertEquals(fixture.getString("ticket_id"), offer.ticketId)
        assertEquals(deviceId, offer.deviceId)
        assertEquals(fixture.getString("filename"), offer.filename)
        assertEquals(fixture.getLong("size"), offer.size)
        assertEquals(fixture.getString("sha256"), offer.sha256Hex)
    }

    @Test
    fun `duplicate parameters are rejected`() {
        assertRejected("$payload&ticket=${fixture.getString("ticket_id")}", "duplicate_parameter")
        assertRejected("$payload&device=$deviceId", "duplicate_parameter")
    }

    @Test
    fun `unknown and missing parameters are rejected`() {
        assertRejected("$payload&path=%2Fetc%2Fpasswd", "invalid_handoff")
        assertRejected("$payload&token=secret", "invalid_handoff")
        assertRejected(DownloadTicketParser.PREFIX + "ticket=${fixture.getString("ticket_id")}", "invalid_handoff")
        val withoutDevice = fixture.getJSONObject("params").toMap()
            .filterKeys { it != "device" }
            .entries.joinToString("&") { "${it.key}=${it.value}" }
        assertRejected(DownloadTicketParser.PREFIX + withoutDevice, "invalid_handoff")
    }

    @Test
    fun `unsupported version and foreign schemes are rejected`() {
        assertRejected(payload.replace("://v1?", "://v2?"), "unsupported_version")
        assertRejected(payload.replace("kivo-download://", "kivo-pair://"), "invalid_handoff")
        assertRejected(payload.replace("kivo-download://", "file://"), "invalid_handoff")
        assertRejected("", "invalid_handoff")
        assertRejected(" $payload", "invalid_handoff")
        assertRejected("$payload ", "invalid_handoff")
    }

    @Test
    fun `fragments and malformed component separators are rejected`() {
        assertRejected("$payload#fragment", "invalid_handoff")
        assertRejected("$payload&&ticket=x", "invalid_handoff")
        assertRejected(DownloadTicketParser.PREFIX + "ticket", "invalid_handoff")
        assertRejected(DownloadTicketParser.PREFIX + "ticket=", "invalid_handoff")
        assertRejected(DownloadTicketParser.PREFIX + "ticket=a=b", "invalid_handoff")
    }

    @Test
    fun `malformed ticket syntax is rejected`() {
        for (ticket in listOf(
            "tkt_" + "A".repeat(64),
            "tkt_" + "a".repeat(63),
            "tkt_" + "a".repeat(65),
            "tr_" + "a".repeat(24),
            "tkt_" + "a".repeat(64) + "%00",
            "tkt_" + "a".repeat(32)
        )) {
            assertRejected(payload.replace(fixture.getString("ticket_id"), ticket))
        }
    }

    @Test
    fun `size and digest bounds are enforced`() {
        for (size in listOf("-1", "+1", "3145745.0", "03145745", "0x10", "3145745e2")) {
            assertRejected(withParam("size", size))
        }
        assertRejected(withParam("size", (2L * 1024L * 1024L * 1024L + 1L).toString()))
        for (digest in listOf("0".repeat(63), "0".repeat(65), "G".repeat(64), fixture.getString("sha256").uppercase())) {
            assertRejected(withParam("sha256", digest))
        }
    }

    @Test
    fun `percent encoding is strict and cannot smuggle paths or controls`() {
        val base = DownloadTicketParser.PREFIX + fixture.getJSONObject("params").toMap()
            .entries.joinToString("&") { "${it.key}=${it.value}" }
        for (mutated in listOf(
            base.replace("name=", "name=%2"),
            base.replace("name=", "name=%zz"),
            base.replace("name=", "name=quarterly+report"),
            base.replace("name=", "name=quarterly%20report%00"),
            base.replace("name=", "name=%2Fetc%2Fpasswd"),
            base.replace("name=", "name=..%2F..%2Fsecret"),
            base.replace("name=", "name=%FF"),
            base.replace("name=", "name=%C3")
        )) {
            assertRejected(mutated)
        }
    }

    @Test
    fun `oversized handoff is rejected before parsing`() {
        assertRejected(
            payload + "&padding=" + "a".repeat(DownloadTicketParser.MAX_PAYLOAD_BYTES),
            "handoff_too_large"
        )
    }

    @Test
    fun `device binding is enforced locally`() {
        assertRejected(payload, "handoff_device_mismatch", "client_other_device")
        assertRejected(payload, "invalid_device", "client_bad device")
    }

    @Test
    fun `handoff never carries paths secrets or key material`() {
        for (forbidden in listOf("/home/", "/tmp/", "file://", "bearer", "key_hex", "BEGIN ", "download_id=")) {
            assertFalse(forbidden, payload.contains(forbidden, ignoreCase = true))
        }
        val offer = DownloadTicketParser.parse(payload, deviceId)
        assertTrue(offer.ticketId.startsWith("tkt_"))
        assertEquals(64, offer.sha256Hex.length)
    }

    private fun withParam(key: String, value: String): String {
        val params = fixture.getJSONObject("params").toMap().toMutableMap()
        params[key] = value
        return DownloadTicketParser.PREFIX + params.entries.joinToString("&") { "${it.key}=${it.value}" }
    }

    private fun assertRejected(
        raw: String,
        expectedCode: String? = null,
        expectedDeviceId: String? = null
    ) {
        try {
            DownloadTicketParser.parse(raw, expectedDeviceId)
            throw AssertionError("accepted handoff: $raw")
        } catch (e: DownloadTicketException) {
            if (expectedCode != null) assertEquals(expectedCode, e.code)
        }
    }

    private fun JSONObject.toMap(): Map<String, String> {
        val map = LinkedHashMap<String, String>()
        for (key in keys()) map[key] = getString(key)
        return map
    }

    private fun loadFixture(): JSONObject {
        val stream = javaClass.classLoader!!.getResourceAsStream("download_handoff_vectors.json")
            ?: error("download_handoff_vectors.json missing from test resources")
        return stream.use { JSONObject(it.readBytes().toString(Charsets.UTF_8)) }
    }
}
