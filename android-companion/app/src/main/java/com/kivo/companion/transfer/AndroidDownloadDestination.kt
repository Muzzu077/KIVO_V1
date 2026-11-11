package com.kivo.companion.transfer

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import android.os.ParcelFileDescriptor
import android.provider.DocumentsContract
import android.provider.OpenableColumns
import java.io.InputStream
import java.io.OutputStream

/**
 * SAF-only publication target. Only `content://` URIs returned by
 * `ACTION_CREATE_DOCUMENT` are accepted; no filesystem path is constructed or
 * transmitted. The URI grant is used for the immediate publication and is not
 * persisted in recovery state.
 */
class AndroidDownloadDestinationResolver(
    private val context: Context
) : DownloadDestinationResolver {
    override fun resolve(uri: String): DownloadDestination {
        if (!uri.startsWith("content://")) {
            throw DownloadPublicationException(
                "unsupported_destination",
                "Only destinations chosen through the system document picker are supported"
            )
        }
        return AndroidDownloadDestination(context.applicationContext, Uri.parse(uri))
    }
}

class AndroidDownloadDestination(
    private val context: Context,
    private val destinationUri: Uri
) : DownloadDestination {
    private val resolver: ContentResolver get() = context.contentResolver

    override val uri: String get() = destinationUri.toString()

    override fun displayName(): String? {
        return try {
            resolver.query(destinationUri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)
                ?.use { cursor ->
                    if (!cursor.moveToFirst()) return null
                    val index = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    if (index < 0) null else cursor.getString(index)
                }
        } catch (e: Exception) {
            null
        }
    }

    override fun openSink(): DownloadSink {
        val descriptor = try {
            resolver.openFileDescriptor(destinationUri, "w")
        } catch (e: Exception) {
            null
        }
        if (descriptor != null) {
            return DescriptorSink(descriptor)
        }
        val stream = try {
            resolver.openOutputStream(destinationUri, "w")
        } catch (e: Exception) {
            null
        } ?: throw DownloadPublicationException(
            "destination_unavailable",
            "The selected destination could not be opened for writing"
        )
        return StreamSink(stream)
    }

    override fun reopenInput(): InputStream? {
        return try {
            resolver.openInputStream(destinationUri)
        } catch (e: Exception) {
            throw DownloadPublicationException(
                "destination_unreadable",
                "The destination provider does not allow verification reads",
                e
            )
        }
    }

    override fun delete(): Boolean {
        return try {
            if (DocumentsContract.isDocumentUri(context, destinationUri)) {
                DocumentsContract.deleteDocument(resolver, destinationUri)
            } else {
                resolver.delete(destinationUri, null, null) > 0
            }
        } catch (e: Exception) {
            false
        }
    }

    private class DescriptorSink(
        private val descriptor: ParcelFileDescriptor
    ) : DownloadSink {
        private val stream: OutputStream = ParcelFileDescriptor.AutoCloseOutputStream(descriptor)

        override fun write(buffer: ByteArray, offset: Int, length: Int) {
            stream.write(buffer, offset, length)
        }

        override fun flushAndSync() {
            stream.flush()
            try {
                descriptor.fileDescriptor.sync()
            } catch (e: Exception) {
                // Some providers return a pipe or a remote descriptor without
                // supporting fsync. Flush still completed; publication remains
                // unverified and is reported truthfully.
            }
        }

        override fun close() {
            stream.close()
        }
    }

    private class StreamSink(
        private val stream: OutputStream
    ) : DownloadSink {
        override fun write(buffer: ByteArray, offset: Int, length: Int) {
            stream.write(buffer, offset, length)
        }

        override fun flushAndSync() {
            stream.flush()
        }

        override fun close() {
            stream.close()
        }
    }
}
