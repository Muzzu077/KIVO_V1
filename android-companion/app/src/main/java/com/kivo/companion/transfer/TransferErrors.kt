package com.kivo.companion.transfer

/**
 * A structured transfer failure. `retryable` means the same logical operation
 * may be attempted again with a fresh sequence/nonce; it never means a request
 * body may be replayed unchanged.
 */
open class TransferException(
    val code: String,
    message: String,
    val retryable: Boolean = false,
    val terminal: Boolean = false,
    cause: Throwable? = null
) : Exception(message, cause)

class TransferProtocolException(
    code: String,
    message: String,
    retryable: Boolean = false,
    terminal: Boolean = true
) : TransferException(code, message, retryable, terminal)

class TransferNetworkException(
    code: String,
    message: String,
    cause: Throwable? = null
) : TransferException(code, message, retryable = true, terminal = false, cause = cause)

class TransferSourceException(
    code: String,
    message: String,
    cause: Throwable? = null
) : TransferException(code, message, retryable = false, terminal = true, cause = cause)

/**
 * Raised when the user cancels while a source is being hashed or spooled.
 * This is a user decision, never a transfer failure, and the staging copy is
 * removed before it reaches the caller.
 */
class TransferCancelledException(
    message: String = "Upload cancelled by the user"
) : Exception(message)
