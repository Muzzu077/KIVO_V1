package com.kivo.companion.transfer

import android.content.ContentResolver
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.DocumentsContract
import android.provider.OpenableColumns
import java.io.FileNotFoundException
import java.io.InputStream

/**
 * SAF-backed document source. Only `content://` URIs are accepted; no direct
 * filesystem paths are constructed from user input.
 */
class AndroidUploadSource(
    private val context: Context,
    private val uri: Uri,
    override val descriptor: DocumentDescriptor
) : UploadSource {
    override fun openStream(): InputStream {
        return context.contentResolver.openInputStream(uri)
            ?: throw TransferSourceException("source_unavailable", "The document provider did not return a stream")
    }
}

class AndroidUploadSourceResolver(
    private val context: Context
) : UploadSourceResolver {
    override fun resolve(uriString: String): UploadSource {
        val uri = try {
            Uri.parse(uriString)
        } catch (e: Exception) {
            throw TransferSourceException("invalid_source_uri", "The selected document URI is invalid")
        }
        if (uri.scheme != ContentResolver.SCHEME_CONTENT) {
            throw TransferSourceException(
                "unsupported_source_scheme",
                "Only SAF content:// documents can be uploaded"
            )
        }

        val resolver = context.contentResolver
        var displayName: String? = null
        var size: Long? = null
        try {
            resolver.query(
                uri,
                arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE),
                null,
                null,
                null
            )?.use { cursor ->
                if (cursor.moveToFirst()) {
                    val nameIndex = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    if (nameIndex >= 0 && !cursor.isNull(nameIndex)) {
                        displayName = cursor.getString(nameIndex)
                    }
                    val sizeIndex = cursor.getColumnIndex(OpenableColumns.SIZE)
                    if (sizeIndex >= 0 && !cursor.isNull(sizeIndex)) {
                        val value = cursor.getLong(sizeIndex)
                        if (value >= 0L) size = value
                    }
                }
            }
        } catch (e: Exception) {
            // Some providers expose only a stream. Fall through to spooling.
        }

        val mime = try {
            resolver.getType(uri)
        } catch (e: Exception) {
            null
        }
        val isDirectory = mime == DocumentsContract.Document.MIME_TYPE_DIR

        var persistable = false
        try {
            resolver.takePersistableUriPermission(
                uri,
                Intent.FLAG_GRANT_READ_URI_PERMISSION
            )
            persistable = true
        } catch (e: SecurityException) {
            // The grant is transient; spool to survive process death.
        } catch (e: Exception) {
            // Treat unknown provider behavior as non-persistable.
        }

        val descriptor = DocumentDescriptor(
            uri = uriString,
            displayName = displayName,
            sizeBytes = size,
            mimeType = mime,
            isDirectory = isDirectory,
            persistable = persistable,
            reopenable = persistable
        )
        return AndroidUploadSource(context, uri, descriptor)
    }
}
