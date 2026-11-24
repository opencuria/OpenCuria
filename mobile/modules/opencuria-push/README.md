# End-to-end encrypted push

Local Expo module and extension sources that let the app read push messages
nobody else can: not the push proxy, not Apple, not Google. Servers encrypt each
message for one installation (RFC 8291, aes128gcm); the app decrypts on the
device.

- **Keys:** `getKeys(id)` creates an installation's P-256 key pair and 16-byte
  auth secret once and returns only the public half, which the app registers
  with the server. `deleteKeys(id)` runs on sign-out.
  - iOS keeps them in the keychain of the app group (`ExpoWidgetsAppGroupIdentifier`),
    readable after first unlock, never synced or restored to another device.
  - Android encrypts them with a non-exportable Android Keystore key.
- **iOS alerts:** `extension/NotificationService.swift` is a notification service
  extension (`plugins/withNotificationServiceExtension.js` adds the target and
  its EAS entry). It replaces the proxy's placeholder text with the decrypted
  title and body and puts the data where expo-notifications reads it
  (`userInfo.body`). An unreadable message keeps the placeholder.
- **iOS Live Activities:** the content state's `props` arrive encrypted. The
  plugin compiles `ios/Shared/OpenCuriaPushCrypto.swift` into expo-widgets'
  extension, and `patches/expo-widgets+57.0.27.patch` decodes `props` through
  its `OpenCuriaPushPayloadDecoder` before rendering. Props the app sets
  locally are plain JSON and pass through unchanged. Re-create the patch when
  upgrading expo-widgets.
- **Android:** `PushMessagingService` replaces expo-notifications' FCM service
  (manifest priority 1 against -1). It decrypts the data message and hands
  expo-notifications a normal notification, so taps reach the JavaScript
  listeners as before.

Each device tries every stored key, one per signed-in account, so messages
carry no key ID. Tests: `RFC 8291` vectors in
`android/src/test` (`./gradlew :opencuria-push:testDebugUnitTest`) and in the
backend (`apps/notifications/tests/test_proxy.py`).
