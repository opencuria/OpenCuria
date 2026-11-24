package expo.modules.opencuriapush

import android.content.pm.PackageManager
import android.os.Build
import android.util.Base64
import com.google.firebase.messaging.RemoteMessage
import expo.modules.notifications.notifications.enums.NotificationPriority
import expo.modules.notifications.notifications.model.Notification
import expo.modules.notifications.notifications.model.NotificationContent
import expo.modules.notifications.notifications.model.NotificationRequest
import expo.modules.notifications.notifications.model.triggers.FirebaseNotificationTrigger
import expo.modules.notifications.service.ExpoFirebaseMessagingService
import expo.modules.notifications.service.NotificationsService
import org.json.JSONObject
import java.util.Date
import java.util.UUID

/**
 * Decrypts the push proxy's data messages and hands expo-notifications a
 * normal notification, so presentation and tap handling stay unchanged.
 * Unreadable messages show a placeholder, never their ciphertext.
 */
class PushMessagingService : ExpoFirebaseMessagingService() {
  override fun onMessageReceived(remoteMessage: RemoteMessage) {
    val encrypted = remoteMessage.data["enc"] ?: return super.onMessageReceived(remoteMessage)
    val alert = decrypt(encrypted)
    val content = NotificationContent.Builder()
      .setTitle(alert?.optString("title")?.takeIf { it.isNotEmpty() } ?: FALLBACK_TITLE)
      .setText(alert?.optString("body")?.takeIf { it.isNotEmpty() } ?: FALLBACK_BODY)
      .setBody(alert?.optJSONObject("data") ?: JSONObject())
      .setPriority(NotificationPriority.HIGH)
      .useDefaultSound()
      .setAutoDismiss(true)
      .build()
    // The trigger carries only the channel, never the ciphertext, into JavaScript.
    val trigger = FirebaseNotificationTrigger(
      RemoteMessage.Builder("$packageName@push.opencuria")
        .setMessageId(remoteMessage.messageId ?: UUID.randomUUID().toString())
        .setData(defaultChannel()?.let { mapOf("channelId" to it) } ?: emptyMap())
        .build(),
    )
    val identifier = alert?.optString("tag")?.takeIf { it.isNotEmpty() }
      ?: remoteMessage.messageId
      ?: UUID.randomUUID().toString()
    val sent = remoteMessage.sentTime.takeIf { it > 0 } ?: System.currentTimeMillis()
    NotificationsService.receive(
      this,
      Notification(NotificationRequest(identifier, content, trigger), Date(sent)),
    )
  }

  private fun decrypt(encrypted: String): JSONObject? {
    val body = try {
      Base64.decode(encrypted, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)
    } catch (_: IllegalArgumentException) {
      return null
    }
    val plaintext = PushKeyStore(this).all().firstNotNullOfOrNull { entry ->
      WebPushDecryptor.decrypt(body, entry.privateKey, entry.publicKey, entry.authSecret)
    } ?: return null
    return try {
      JSONObject(String(plaintext, Charsets.UTF_8))
    } catch (_: org.json.JSONException) {
      null
    }
  }

  /** The channel the expo-notifications plugin set as Firebase's default. */
  private fun defaultChannel(): String? = try {
    val info = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
      packageManager.getApplicationInfo(
        packageName,
        PackageManager.ApplicationInfoFlags.of(PackageManager.GET_META_DATA.toLong()),
      )
    } else {
      @Suppress("DEPRECATION")
      packageManager.getApplicationInfo(packageName, PackageManager.GET_META_DATA)
    }
    info.metaData?.getString("com.google.firebase.messaging.default_notification_channel_id")
  } catch (_: PackageManager.NameNotFoundException) {
    null
  }

  private companion object {
    const val FALLBACK_TITLE = "OpenCuria"
    const val FALLBACK_BODY = "New agent activity"
  }
}
