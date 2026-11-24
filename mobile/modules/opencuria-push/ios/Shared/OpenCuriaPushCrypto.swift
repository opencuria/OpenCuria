// Shared by the app (OpenCuriaPush module), its notification service extension
// and the Live Activity widget extension; the config plugin copies this file
// into both extension targets. Keep it free of Expo and UIKit imports.

import CryptoKit
import Foundation
import Security

/// Per-account RFC 8291 keys in the app group keychain.
///
/// The app creates them, the extensions read them to decrypt. They are
/// readable after the first unlock (pushes arrive on a locked phone) and never
/// leave this device: no iCloud sync, no backup restore onto another phone.
public enum OpenCuriaPushKeys {
  static let service = "dev.opencuria.push.keys"
  private static let version: UInt8 = 1

  struct Entry {
    let privateKey: P256.KeyAgreement.PrivateKey
    let authSecret: Data
  }

  public enum KeyError: Error {
    case missingAppGroup
    case keychain(OSStatus)
    case random
  }

  /// expo-widgets writes the app group into every target's Info.plist.
  static var accessGroup: String? {
    Bundle.main.object(forInfoDictionaryKey: "ExpoWidgetsAppGroupIdentifier") as? String
  }

  private static func query(_ group: String) -> [String: Any] {
    [
      kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: service,
      kSecAttrAccessGroup as String: group,
    ]
  }

  /// The receiver key (65-byte X9.63 point) and auth secret for `id`, created once.
  public static func publicKeys(for id: String) throws -> (publicKey: Data, authSecret: Data) {
    guard let group = accessGroup else { throw KeyError.missingAppGroup }
    if let entry = read(id: id, group: group) {
      return (entry.privateKey.publicKey.x963Representation, entry.authSecret)
    }
    var auth = Data(count: 16)
    let status = auth.withUnsafeMutableBytes {
      SecRandomCopyBytes(kSecRandomDefault, 16, $0.baseAddress!)
    }
    guard status == errSecSuccess else { throw KeyError.random }
    let privateKey = P256.KeyAgreement.PrivateKey()
    var item = query(group)
    item[kSecAttrAccount as String] = id
    item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
    item[kSecAttrSynchronizable as String] = false
    item[kSecValueData as String] = Data([version]) + privateKey.rawRepresentation + auth
    let added = SecItemAdd(item as CFDictionary, nil)
    if added == errSecDuplicateItem, let entry = read(id: id, group: group) {
      return (entry.privateKey.publicKey.x963Representation, entry.authSecret)
    }
    guard added == errSecSuccess else { throw KeyError.keychain(added) }
    return (privateKey.publicKey.x963Representation, auth)
  }

  /// Forget an account's keys; its pending pushes can no longer be read.
  public static func delete(id: String) throws {
    guard let group = accessGroup else { throw KeyError.missingAppGroup }
    var item = query(group)
    item[kSecAttrAccount as String] = id
    let status = SecItemDelete(item as CFDictionary)
    guard status == errSecSuccess || status == errSecItemNotFound else {
      throw KeyError.keychain(status)
    }
  }

  static func all() -> [Entry] {
    guard let group = accessGroup else { return [] }
    var item = query(group)
    item[kSecMatchLimit as String] = kSecMatchLimitAll
    item[kSecReturnAttributes as String] = true
    item[kSecReturnData as String] = true
    var result: CFTypeRef?
    guard SecItemCopyMatching(item as CFDictionary, &result) == errSecSuccess,
      let rows = result as? [[String: Any]]
    else { return [] }
    return rows.compactMap { ($0[kSecValueData as String] as? Data).flatMap(decode) }
  }

  private static func read(id: String, group: String) -> Entry? {
    var item = query(group)
    item[kSecAttrAccount as String] = id
    item[kSecMatchLimit as String] = kSecMatchLimitOne
    item[kSecReturnData as String] = true
    var result: CFTypeRef?
    guard SecItemCopyMatching(item as CFDictionary, &result) == errSecSuccess,
      let data = result as? Data
    else { return nil }
    return decode(data)
  }

  private static func decode(_ data: Data) -> Entry? {
    let bytes = [UInt8](data)
    guard bytes.count == 49, bytes[0] == version,
      let privateKey = try? P256.KeyAgreement.PrivateKey(rawRepresentation: Data(bytes[1..<33]))
    else { return nil }
    return Entry(privateKey: privateKey, authSecret: Data(bytes[33..<49]))
  }
}

/// Web Push message decryption (RFC 8291 with RFC 8188 aes128gcm).
public enum OpenCuriaPushCrypto {
  /// A decrypted notification as the backend wrote it.
  public struct Alert {
    public let title: String
    public let body: String
    public let thread: String
    public let data: [String: Any]
  }

  /// Decrypt the push proxy's `enc` value with any of this device's account keys.
  public static func decrypt(base64url value: String) -> Data? {
    guard let body = base64url(value) else { return nil }
    for entry in OpenCuriaPushKeys.all() {
      if let plaintext = decrypt(body, privateKey: entry.privateKey, authSecret: entry.authSecret) {
        return plaintext
      }
    }
    return nil
  }

  public static func alert(fromEncrypted value: String) -> Alert? {
    guard let plaintext = decrypt(base64url: value),
      let object = try? JSONSerialization.jsonObject(with: plaintext) as? [String: Any],
      let title = object["title"] as? String,
      let body = object["body"] as? String
    else { return nil }
    return Alert(
      title: title,
      body: body,
      thread: object["thread"] as? String ?? "",
      data: object["data"] as? [String: Any] ?? [:]
    )
  }

  /// One aes128gcm record from a P-256 sender; nil for anything malformed.
  static func decrypt(
    _ body: Data, privateKey: P256.KeyAgreement.PrivateKey, authSecret: Data
  ) -> Data? {
    let bytes = [UInt8](body)
    // Header: salt(16) record size(4) key length(1) sender key(65), then
    // at least the delimiter and the 16-byte tag.
    guard bytes.count >= 86 + 17, bytes[20] == 65, authSecret.count == 16 else { return nil }
    let recordSize =
      UInt32(bytes[16]) << 24 | UInt32(bytes[17]) << 16 | UInt32(bytes[18]) << 8
      | UInt32(bytes[19])
    let salt = Data(bytes[0..<16])
    let senderKey = Data(bytes[21..<86])
    let ciphertext = Data(bytes[86...])
    guard recordSize >= 18, ciphertext.count <= Int(recordSize),
      let sender = try? P256.KeyAgreement.PublicKey(x963Representation: senderKey),
      let shared = try? privateKey.sharedSecretFromKeyAgreement(with: sender)
    else { return nil }
    let keyInfo =
      Data("WebPush: info".utf8) + Data([0]) + privateKey.publicKey.x963Representation
      + senderKey
    let ikm = shared.hkdfDerivedSymmetricKey(
      using: SHA256.self, salt: authSecret, sharedInfo: keyInfo, outputByteCount: 32)
    let key = HKDF<SHA256>.deriveKey(
      inputKeyMaterial: ikm, salt: salt, info: Data("Content-Encoding: aes128gcm".utf8) + Data([0]),
      outputByteCount: 16)
    let nonce = HKDF<SHA256>.deriveKey(
      inputKeyMaterial: ikm, salt: salt, info: Data("Content-Encoding: nonce".utf8) + Data([0]),
      outputByteCount: 12
    ).withUnsafeBytes { Data($0) }
    guard let gcmNonce = try? AES.GCM.Nonce(data: nonce),
      let box = try? AES.GCM.SealedBox(
        nonce: gcmNonce, ciphertext: ciphertext.prefix(ciphertext.count - 16),
        tag: ciphertext.suffix(16)),
      let record = try? AES.GCM.open(box, using: key)
    else { return nil }
    let padded = [UInt8](record)
    guard let end = padded.lastIndex(where: { $0 != 0 }), padded[end] == 2 else { return nil }
    return Data(padded[0..<end])
  }

  static func base64url(_ value: String) -> Data? {
    guard !value.isEmpty,
      value.utf8.allSatisfy({ byte in
        (byte >= 0x30 && byte <= 0x39) || (byte >= 0x41 && byte <= 0x5A)
          || (byte >= 0x61 && byte <= 0x7A) || byte == 0x2D || byte == 0x5F
      })
    else { return nil }
    var base64 = value.replacingOccurrences(of: "-", with: "+")
      .replacingOccurrences(of: "_", with: "/")
    base64 += String(repeating: "=", count: (4 - base64.count % 4) % 4)
    return Data(base64Encoded: base64)
  }
}

/// Found by the patched expo-widgets runtime through the Objective-C runtime:
/// Live Activity pushes carry ciphertext props, local updates carry JSON.
@objc(OpenCuriaPushPayloadDecoder)
public final class OpenCuriaPushPayloadDecoder: NSObject {
  private static let lock = NSLock()
  nonisolated(unsafe) private static var last: (props: String, json: String)?

  @objc(decodeLiveActivityProps:)
  public static func decodeLiveActivityProps(_ props: String) -> String? {
    if props.hasPrefix("{") { return nil }
    lock.lock()
    defer { lock.unlock() }
    if let last, last.props == props { return last.json }
    // An unreadable update renders as an empty activity, never as ciphertext.
    let json =
      OpenCuriaPushCrypto.decrypt(base64url: props).flatMap { String(data: $0, encoding: .utf8) }
      ?? "{}"
    last = (props, json)
    return json
  }
}
