package com.kivo.companion.pairing

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.kivo.companion.model.PairingState
import com.kivo.companion.model.SessionCredentials
import com.kivo.companion.network.PairingNetworkException
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

class PairingViewModel(
    private val repository: PairingRepository
) : ViewModel() {

    private val _uiState = MutableStateFlow<PairingState>(PairingState.Idle)
    val uiState: StateFlow<PairingState> = _uiState.asStateFlow()

    private var pairingJob: Job? = null

    init {
        checkPersistedSession()
    }

    fun checkPersistedSession() {
        val existing = repository.loadPersistedSession()
        if (existing != null && !existing.isExpired()) {
            _uiState.value = existing.toPairedState()
            return
        }

        val pending = repository.loadPendingRecovery()
        if (pending != null) {
            _uiState.value = PairingState.Recovering(pending.sasCode)
            resumeRecovery()
        } else {
            _uiState.value = PairingState.Idle
        }
    }

    fun startScanning() {
        _uiState.value = PairingState.Scanning
    }

    fun onQrScanned(rawUri: String) {
        if (_uiState.value !is PairingState.Scanning) return

        try {
            val offer = QrParser.parse(rawUri)
            _uiState.value = PairingState.Connecting("${offer.hostIp}:${offer.port}")

            pairingJob?.cancel()
            pairingJob = viewModelScope.launch {
                try {
                    val credentials = repository.executePairing(
                        offer = offer,
                        onSasDerived = { sasCode ->
                            _uiState.value = PairingState.AwaitingApproval(sasCode)
                        },
                        onRecovering = { attempt ->
                            val sas = (_uiState.value as? PairingState.AwaitingApproval)?.sasCode
                                ?: (_uiState.value as? PairingState.Recovering)?.sasCode
                                ?: "------"
                            if (attempt > 1) {
                                _uiState.value = PairingState.Recovering(sas)
                            }
                        }
                    )
                    _uiState.value = credentials.toPairedState()
                } catch (e: QrParsingException) {
                    _uiState.value = PairingState.Error(e.code, e.message ?: "Invalid QR Code")
                } catch (e: PairingNetworkException) {
                    _uiState.value = PairingState.Error(e.code, e.message ?: "Pairing failed")
                } catch (e: Exception) {
                    _uiState.value = PairingState.Error(
                        "pairing_failed",
                        e.message ?: "Pairing handshake failed"
                    )
                }
            }
        } catch (e: QrParsingException) {
            _uiState.value = PairingState.Error(e.code, e.message ?: "Invalid QR Code")
        } catch (e: Exception) {
            _uiState.value = PairingState.Error("malformed_qr", e.message ?: "Malformed QR Code")
        }
    }

    fun cancelPairing() {
        pairingJob?.cancel()
        pairingJob = null
        repository.cancelPendingPairing()
        checkPersistedSession()
    }

    fun disconnect() {
        val credentials = repository.loadPersistedSession()
        if (credentials != null) {
            viewModelScope.launch {
                repository.disconnect(
                    credentials.hostIp,
                    credentials.port,
                    credentials.certFingerprint,
                    credentials.bearerToken
                )
                _uiState.value = PairingState.Idle
            }
        } else {
            _uiState.value = PairingState.Idle
        }
    }

    private fun resumeRecovery() {
        pairingJob?.cancel()
        pairingJob = viewModelScope.launch {
            try {
                val credentials = repository.resumePendingPairing(
                    onSasDerived = { sasCode ->
                        _uiState.value = PairingState.Recovering(sasCode)
                    },
                    onRecovering = { attempt ->
                        val sas = (_uiState.value as? PairingState.Recovering)?.sasCode ?: "------"
                        _uiState.value = PairingState.Recovering(sas)
                    }
                )
                if (credentials != null) {
                    _uiState.value = credentials.toPairedState()
                } else {
                    _uiState.value = PairingState.Idle
                }
            } catch (e: PairingNetworkException) {
                _uiState.value = PairingState.Error(e.code, e.message ?: "Pairing recovery failed")
            } catch (e: Exception) {
                _uiState.value = PairingState.Error(
                    "pairing_recovery_failed",
                    e.message ?: "Pairing recovery failed"
                )
            }
        }
    }

    private fun SessionCredentials.toPairedState(): PairingState.Paired = PairingState.Paired(
        sessionId = sessionId,
        hostAddress = if (hostIp.isNotBlank()) "$hostIp:$port" else "Connected Desktop",
        certFingerprintSuffix = certFingerprint.takeLast(8),
        expiresAt = expiresAt
    )
}
