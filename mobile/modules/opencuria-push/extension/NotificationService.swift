import UserNotifications

/// Replaces the push proxy's placeholder with the notification the server
/// encrypted for this device. Anything unreadable keeps the placeholder; the
/// ciphertext is never shown and never handed to the app.
final class NotificationService: UNNotificationServiceExtension {
  private var contentHandler: ((UNNotificationContent) -> Void)?
  private var fallback: UNNotificationContent?

  override func didReceive(
    _ request: UNNotificationRequest,
    withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void
  ) {
    self.contentHandler = contentHandler
    guard let content = request.content.mutableCopy() as? UNMutableNotificationContent else {
      return finish(request.content)
    }
    var userInfo = content.userInfo
    let encrypted = userInfo.removeValue(forKey: "enc") as? String
    content.userInfo = userInfo
    fallback = content.copy() as? UNNotificationContent
    guard let encrypted, let alert = OpenCuriaPushCrypto.alert(fromEncrypted: encrypted) else {
      return finish(content)
    }
    content.title = alert.title
    content.body = alert.body
    content.threadIdentifier = alert.thread
    // expo-notifications reads a remote notification's custom data from "body".
    userInfo["body"] = alert.data
    content.userInfo = userInfo
    finish(content)
  }

  override func serviceExtensionTimeWillExpire() {
    if let fallback { finish(fallback) }
  }

  private func finish(_ content: UNNotificationContent) {
    contentHandler?(content)
    contentHandler = nil
  }
}
