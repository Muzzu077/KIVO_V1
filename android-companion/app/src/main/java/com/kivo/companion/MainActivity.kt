package com.kivo.companion

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.kivo.companion.model.PairingState
import com.kivo.companion.pairing.PairingRepository
import com.kivo.companion.pairing.PairingViewModel
import com.kivo.companion.storage.KeystoreSecretStorage
import com.kivo.companion.transfer.TransferOperation
import com.kivo.companion.transfer.TransferRuntime
import com.kivo.companion.transfer.TransferViewModel
import com.kivo.companion.ui.screens.HomeScreen
import com.kivo.companion.ui.screens.PairedScreen
import com.kivo.companion.ui.screens.QrScannerScreen
import com.kivo.companion.ui.screens.SasScreen
import com.kivo.companion.ui.theme.KivoCompanionTheme

class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val storage = KeystoreSecretStorage(applicationContext)
        val repository = PairingRepository(storage)
        val transferRuntime = TransferRuntime.get(applicationContext)

        setContent {
            KivoCompanionTheme {
                val pairingViewModel: PairingViewModel = viewModel {
                    PairingViewModel(repository)
                }
                val transferViewModel: TransferViewModel = viewModel {
                    TransferViewModel(transferRuntime)
                }
                MainApp(pairingViewModel, transferViewModel)
            }
        }
    }
}

@Composable
fun MainApp(
    viewModel: PairingViewModel,
    transferViewModel: TransferViewModel
) {
    val uiState by viewModel.uiState.collectAsStateWithLifecycle()
    val transferSnapshot by transferViewModel.snapshot.collectAsStateWithLifecycle()
    val transferActive by transferViewModel.active.collectAsStateWithLifecycle()
    val operation by transferViewModel.operation.collectAsStateWithLifecycle()
    val downloadSnapshot by transferViewModel.downloadSnapshot.collectAsStateWithLifecycle()
    val pendingDownloadOffer by transferViewModel.pendingDownloadOffer.collectAsStateWithLifecycle()
    val downloadHandoffError by transferViewModel.downloadHandoffError.collectAsStateWithLifecycle()
    val selection by transferViewModel.selection.collectAsStateWithLifecycle()
    val snackbarHostState = remember { SnackbarHostState() }
    val downloadActive = transferActive && operation == TransferOperation.DOWNLOAD

    LaunchedEffect(uiState) {
        when (val state = uiState) {
            is PairingState.Paired -> transferViewModel.onPaired(state.sessionId)
            is PairingState.Idle -> transferViewModel.onUnpaired()
            is PairingState.Error -> {
                snackbarHostState.showSnackbar("Error: ${state.message}")
            }
            else -> Unit
        }
    }

    Scaffold(
        snackbarHost = { SnackbarHost(hostState = snackbarHostState) }
    ) { innerPadding ->
        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(innerPadding)
        ) {
            when (val state = uiState) {
                is PairingState.Idle -> {
                    HomeScreen(onScanClicked = { viewModel.startScanning() })
                }
                is PairingState.Scanning -> {
                    QrScannerScreen(
                        onQrScanned = { uri -> viewModel.onQrScanned(uri) },
                        onCancel = { viewModel.cancelPairing() }
                    )
                }
                is PairingState.Connecting -> {
                    SasScreen(
                        sasCode = "Connecting…",
                        onCancel = { viewModel.cancelPairing() }
                    )
                }
                is PairingState.VerifyingSas -> {
                    SasScreen(
                        sasCode = state.sasCode,
                        onCancel = { viewModel.cancelPairing() }
                    )
                }
                is PairingState.AwaitingApproval -> {
                    SasScreen(
                        sasCode = state.sasCode,
                        onCancel = { viewModel.cancelPairing() }
                    )
                }
                is PairingState.Recovering -> {
                    SasScreen(
                        sasCode = state.sasCode,
                        onCancel = { viewModel.cancelPairing() }
                    )
                }
                is PairingState.Paired -> {
                    PairedScreen(
                        hostAddress = state.hostAddress,
                        fingerprintSuffix = state.certFingerprintSuffix,
                        transferSnapshot = transferSnapshot,
                        transferActive = transferActive,
                        downloadSnapshot = downloadSnapshot,
                        downloadActive = downloadActive,
                        pendingDownloadOffer = pendingDownloadOffer,
                        downloadHandoffError = downloadHandoffError,
                        selection = selection,
                        onUrisSelected = transferViewModel::addSelectedUris,
                        onRemoveSelected = transferViewModel::removeSelected,
                        onClearSelection = transferViewModel::clearSelection,
                        onConfirmUpload = transferViewModel::confirmUpload,
                        onCancelTransfer = transferViewModel::cancelTransfer,
                        onResumeTransfer = transferViewModel::resumeTransfer,
                        onDismissOutcome = transferViewModel::dismissOutcome,
                        onDownloadQrScanned = transferViewModel::acceptDownloadHandoff,
                        onConfirmDownload = transferViewModel::confirmDownload,
                        onDismissDownloadOffer = {
                            transferViewModel.dismissDownloadOffer()
                            transferViewModel.clearDownloadHandoffError()
                        },
                        onResumeDownload = transferViewModel::resumeDownload,
                        onCancelDownload = transferViewModel::cancelDownload,
                        onPublishDownload = transferViewModel::publishDownload,
                        onDismissDownloadOutcome = transferViewModel::dismissDownloadOutcome,
                        onDisconnect = {
                            transferViewModel.disconnectThen { viewModel.disconnect() }
                        }
                    )
                }
                is PairingState.Error -> {
                    HomeScreen(onScanClicked = { viewModel.startScanning() })
                }
            }
        }
    }
}
