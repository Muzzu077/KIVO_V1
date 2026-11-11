package com.kivo.companion.transfer

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.kivo.companion.MainActivity
import com.kivo.companion.R
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.launch

/**
 * `connectedDevice` foreground service for LAN transfers.
 *
 * A data-sync job is deliberately not used: uploads and downloads are
 * user-initiated local network transfers with explicit desktop authorization,
 * and the protocol can wait on a human for approval and final acceptance. One
 * service observes whichever operation the process-wide runtime owns, exposes
 * progress and a cancel action, and stops as soon as no operation remains.
 */
class TransferService : Service() {

    companion object {
        private const val CHANNEL_ID = "kivo_transfers"
        private const val NOTIFICATION_ID = 4101
        private const val ACTION_CANCEL = "com.kivo.companion.action.CANCEL_TRANSFER"

        fun start(context: Context) {
            ContextCompat.startForegroundService(
                context,
                Intent(context, TransferService::class.java)
            )
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, TransferService::class.java))
        }
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private lateinit var runtime: TransferRuntime
    private var observing = false

    override fun onCreate() {
        super.onCreate()
        runtime = TransferRuntime.get(this)
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_CANCEL) {
            runtime.requestCancel()
            if (!runtime.active.value) stopSelf()
            return START_NOT_STICKY
        }

        startInForeground()
        startObserving()
        if (!runtime.active.value) stopSelf()
        return START_NOT_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        observing = false
        scope.cancel()
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        super.onDestroy()
    }

    private fun startObserving() {
        if (observing) return
        observing = true
        scope.launch {
            combine(
                runtime.snapshot,
                runtime.downloadSnapshot,
                runtime.operation,
                runtime.active
            ) { upload, download, operation, active ->
                TransferServiceState(upload, download, operation, active)
            }.collect { state ->
                if (!state.active) {
                    ServiceCompat.stopForeground(this@TransferService, ServiceCompat.STOP_FOREGROUND_REMOVE)
                    stopSelf()
                } else {
                    updateNotification()
                }
            }
        }
    }

    private fun startInForeground() {
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE
        } else {
            0
        }
        ServiceCompat.startForeground(
            this,
            NOTIFICATION_ID,
            buildNotification(),
            type
        )
    }

    private fun updateNotification() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            return
        }
        try {
            NotificationManagerCompat.from(this)
                .notify(NOTIFICATION_ID, buildNotification())
        } catch (e: SecurityException) {
            // The foreground-service channel is still visible in the task
            // manager; the in-app screen remains authoritative.
        }
    }

    private fun buildNotification(): Notification {
        val content = notificationContent()
        val active = runtime.active.value
        val builder = NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setColor(0xFF6FBF8E.toInt())
            .setContentTitle(content.title)
            .setContentText(content.text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(content.text))
            .setContentIntent(contentIntent())
            .setOngoing(active)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
            .setCategory(NotificationCompat.CATEGORY_PROGRESS)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)

        if (content.showProgress) {
            builder.setProgress(
                100,
                content.progressPercent,
                content.indeterminate
            )
        }
        if (content.showCancel && active) {
            builder.addAction(0, "Cancel", cancelIntent())
        }
        return builder.build()
    }

    private fun notificationContent(): TransferNotificationContent {
        return when (runtime.operation.value) {
            TransferOperation.DOWNLOAD -> DownloadNotificationFactory.from(
                runtime.downloadSnapshot.value,
                runtime.active.value
            )
            else -> TransferNotificationFactory.from(
                runtime.snapshot.value,
                runtime.active.value
            )
        }
    }

    private fun contentIntent(): PendingIntent {
        val intent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        return PendingIntent.getActivity(
            this,
            0,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
    }

    private fun cancelIntent(): PendingIntent {
        val intent = Intent(this, TransferService::class.java).setAction(ACTION_CANCEL)
        return PendingIntent.getService(
            this,
            1,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
    }

    private fun createChannel() {
        val manager = getSystemService(NotificationManager::class.java)
        val channel = NotificationChannel(
            CHANNEL_ID,
            "File transfers",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Upload progress, desktop approval, and cancellation"
            setShowBadge(false)
            lockscreenVisibility = Notification.VISIBILITY_PRIVATE
        }
        manager.createNotificationChannel(channel)
    }

    private data class TransferServiceState(
        val upload: TransferSnapshot,
        val download: DownloadSnapshot,
        val operation: TransferOperation,
        val active: Boolean
    )
}
