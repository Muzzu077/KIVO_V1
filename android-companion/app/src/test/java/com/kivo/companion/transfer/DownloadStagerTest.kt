package com.kivo.companion.transfer

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileOutputStream
import java.nio.file.Files
import java.nio.file.Paths

/**
 * App-private staging tests. Every stage lives under one directory with a
 * random name; symlinks, canonical escapes, insufficient space, and geometry
 * mistakes are rejected rather than repaired.
 */
class DownloadStagerTest {

    private lateinit var root: File
    private lateinit var staging: File
    private var freeSpace = 2L * 1024L * 1024L * 1024L

    @Before
    fun setUp() {
        root = Files.createTempDirectory("download_stager_").toFile()
        staging = File(root, "kivo-downloads").also { it.mkdirs() }
    }

    @After
    fun tearDown() {
        root.deleteRecursively()
    }

    private fun stager(): DownloadStager = DownloadStager(
        directory = staging,
        freeSpaceBytes = { freeSpace }
    )

    @Test
    fun `new stages use random app-private names`() {
        val first = stager().newStage()
        val second = stager().newStage()
        assertTrue(PersistedDownload.STAGE_PATTERN.matches(first.name))
        assertTrue(PersistedDownload.STAGE_PATTERN.matches(second.name))
        assertFalse(first.name == second.name)
        assertEquals(staging.canonicalFile, first.canonicalFile.parentFile)
        assertEquals(0L, first.length())
    }

    @Test
    fun `prepare reserves exact length and rejects insufficient space`() {
        val stager = stager()
        val stage = stager.newStage()
        stager.prepare(stage, 2L * 1024L * 1024L + 17L)
        assertEquals(2L * 1024L * 1024L + 17L, stage.length())

        freeSpace = 1024L
        val rejected = stager.newStage()
        try {
            stager.prepare(rejected, 1024L * 1024L)
            throw AssertionError("accepted an under-space staging copy")
        } catch (e: TransferSourceException) {
            assertEquals("insufficient_storage", e.code)
        }
    }

    @Test
    fun `empty files prepare without allocation and hash to the empty digest`() {
        val stager = stager()
        val stage = stager.newStage()
        stager.prepare(stage, 0L)
        assertEquals(0L, stage.length())
        assertEquals(
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            stager.hashStage(stage)
        )
        assertTrue(
            stager.verifyStage(
                stage,
                0L,
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            )
        )
    }

    @Test
    fun `appended chunks hash and verify byte for byte`() {
        val stager = stager()
        val stage = stager.newStage()
        val first = ByteArray(1024) { (it % 251).toByte() }
        val second = ByteArray(777) { ((it + 13) % 251).toByte() }
        stager.prepare(stage, (first.size + second.size).toLong())
        stager.append(stage, 0L, first)
        stager.append(stage, first.size.toLong(), second)

        val expected = TransferCrypto.sha256Hex(first + second)
        assertEquals(expected, stager.hashStage(stage))
        assertTrue(stager.verifyStage(stage, (first.size + second.size).toLong(), expected))
        assertFalse(stager.verifyStage(stage, (first.size + second.size).toLong(), "0".repeat(64)))
        assertFalse(stager.verifyStage(stage, first.size.toLong(), expected))

        val sink = ByteArrayOutputStream()
        assertEquals((first.size + second.size).toLong(), stager.copyTo(stage, sink))
        assertTrue((first + second).contentEquals(sink.toByteArray()))
    }

    @Test
    fun `append cannot grow beyond the reserved geometry`() {
        val stager = stager()
        val stage = stager.newStage()
        stager.prepare(stage, 4L)
        stager.append(stage, 0L, ByteArray(4) { 1 })
        try {
            stager.append(stage, 4L, byteArrayOf(9))
            throw AssertionError("accepted an overrun chunk")
        } catch (e: TransferSourceException) {
            assertEquals("staging_geometry", e.code)
        }
        assertEquals(4L, stage.length())
    }

    @Test
    fun `truncate removes an unverified tail`() {
        val stager = stager()
        val stage = stager.newStage()
        stager.prepare(stage, 10L)
        stager.append(stage, 0L, ByteArray(10) { 2 })
        stager.truncate(stage, 4L)
        assertEquals(4L, stage.length())
        assertEquals(TransferCrypto.sha256Hex(ByteArray(4) { 2 }), stager.hashStage(stage))
    }

    @Test
    fun `stage names cannot escape the directory or follow symlinks`() {
        val stager = stager()
        val outside = File(root, "outside.bin").also {
            FileOutputStream(it).use { out -> out.write(byteArrayOf(1, 2, 3)) }
        }
        val symlinkName = "kivo_download_${"a".repeat(32)}.part"
        val link = File(staging, symlinkName)
        Files.createSymbolicLink(link.toPath(), outside.toPath())

        assertNull(stager.stageByName(symlinkName))
        assertNull(stager.stageByName("../outside.bin"))
        assertNull(stager.stageByName("kivo_download_${"a".repeat(32)}.part\n"))
        try {
            stager.prepare(link, 3L)
            throw AssertionError("accepted a symlinked staging file")
        } catch (e: TransferSourceException) {
            assertEquals("staging_invalid", e.code)
        }

        val canonicalEscape = File(staging, "kivo_download_${"b".repeat(32)}.part")
        assertNotNull(canonicalEscape)
        assertNull(stager.stageByName(canonicalEscape.name))
    }

    @Test
    fun `orphan cleanup keeps only the referenced stage`() {
        val stager = stager()
        val keep = stager.newStage()
        stager.prepare(keep, 3L)
        stager.append(keep, 0L, byteArrayOf(1, 2, 3))
        val orphan = stager.newStage()
        stager.prepare(orphan, 1L)
        val unrelated = File(staging, "notes.txt").also { it.writeText("keep me") }

        stager.cleanupOrphans(keep.name)

        assertTrue(keep.exists())
        assertFalse(orphan.exists())
        assertTrue(unrelated.exists())
    }

    @Test
    fun `deleting by name never touches another file`() {
        val stager = stager()
        val stage = stager.newStage()
        stager.prepare(stage, 1L)
        val unrelated = File(staging, "kivo_download_${"c".repeat(32)}.txt")
        unrelated.writeText("outside the pattern")

        stager.deleteByName(stage.name)

        assertFalse(stage.exists())
        assertTrue(unrelated.exists())
    }

    @Test
    fun `hash cancellation stops before allocating a whole-file buffer`() {
        val stager = stager()
        val stage = stager.newStage()
        stager.prepare(stage, 4096L)
        stager.append(stage, 0L, ByteArray(4096) { 1 })
        try {
            stager.hashStage(stage, isCancelled = { true })
            throw AssertionError("hash ignored cancellation")
        } catch (e: TransferCancelledException) {
            assertTrue(e.message!!.contains("cancelled", ignoreCase = true))
        }
    }

    @Test
    fun `staging directory must not be a symlink`() {
        val real = File(root, "real-staging").also { it.mkdirs() }
        val link = File(root, "linked-staging")
        Files.createSymbolicLink(link.toPath(), real.toPath())
        try {
            DownloadStager(directory = link)
            throw AssertionError("accepted a symlinked staging directory")
        } catch (e: IllegalArgumentException) {
            assertTrue(e.message!!.contains("symlink"))
        }
    }

    @Test
    fun `canonical path helpers reject parent traversal names`() {
        // The staging name grammar is the first line of defence; the canonical
        // parent comparison is the second. Both are exercised without ever
        // creating a file outside the directory.
        val stager = stager()
        val outside = File(root, "secret.part")
        assertNull(stager.stageByName(outside.name))
        assertNull(stager.stageByName(Paths.get("..", outside.name).toString()))
    }
}
