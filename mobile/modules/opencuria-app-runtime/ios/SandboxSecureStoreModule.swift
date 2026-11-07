// Copyright OpenCuria contributors. AGPL-3.0-only.

import ExpoModulesCore
import Security

struct SandboxSecureStoreOptions: Record {
  @Field var keychainService: String?
  @Field var keychainAccessible: Int = 5
  @Field var requireAuthentication: Bool = false
}

/**
 `expo-secure-store` for one installed app. Every Keychain item lives under a
 service prefixed with the app's id, so an app can neither read OpenCuria's
 credentials nor another app's secrets. Biometric protection and access
 groups are not available to installed apps.
 */
final class SandboxSecureStoreModule: Module {
  private let servicePrefix: String

  // swiftlint:disable:next unavailable_function
  required init(appContext: AppContext) {
    fatalError("Use init(appContext:servicePrefix:)")
  }

  init(appContext: AppContext, servicePrefix: String) {
    self.servicePrefix = servicePrefix
    super.init(appContext: appContext)
  }

  func definition() -> ModuleDefinition {
    Name("ExpoSecureStore")

    Constant("AFTER_FIRST_UNLOCK") { 0 }
    Constant("AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY") { 1 }
    Constant("ALWAYS") { 2 }
    Constant("WHEN_PASSCODE_SET_THIS_DEVICE_ONLY") { 3 }
    Constant("ALWAYS_THIS_DEVICE_ONLY") { 4 }
    Constant("WHEN_UNLOCKED") { 5 }
    Constant("WHEN_UNLOCKED_THIS_DEVICE_ONLY") { 6 }

    AsyncFunction("getValueWithKeyAsync") { (key: String, options: SandboxSecureStoreOptions) -> String? in
      try self.get(key, options)
    }

    Function("getValueWithKeySync") { (key: String, options: SandboxSecureStoreOptions) -> String? in
      try self.get(key, options)
    }

    AsyncFunction("setValueWithKeyAsync") { (value: String, key: String, options: SandboxSecureStoreOptions) -> Bool in
      try self.set(value, key, options)
    }

    Function("setValueWithKeySync") { (value: String, key: String, options: SandboxSecureStoreOptions) -> Bool in
      try self.set(value, key, options)
    }

    AsyncFunction("deleteValueWithKeyAsync") { (key: String, options: SandboxSecureStoreOptions) in
      SecItemDelete(self.query(key, options) as CFDictionary)
    }

    Function("canUseBiometricAuthentication") { () -> Bool in
      false
    }
  }

  /// Delete every item an app stored, e.g. when it is uninstalled.
  static func deleteAll(prefix: String) {
    let search: [String: Any] = [
      kSecClass as String: kSecClassGenericPassword,
      kSecMatchLimit as String: kSecMatchLimitAll,
      kSecReturnAttributes as String: true
    ]
    var result: AnyObject?
    guard SecItemCopyMatching(search as CFDictionary, &result) == errSecSuccess,
      let items = result as? [[String: Any]]
    else {
      return
    }
    for service in Set(items.compactMap { $0[kSecAttrService as String] as? String })
    where service.hasPrefix(prefix) {
      let delete: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: service
      ]
      SecItemDelete(delete as CFDictionary)
    }
  }

  private func validate(_ key: String) throws -> String {
    let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "._-"))
    guard !key.isEmpty, key.unicodeScalars.allSatisfy(allowed.contains) else {
      throw SandboxSecureStoreException("Invalid key \(key)")
    }
    return key
  }

  private func query(_ key: String, _ options: SandboxSecureStoreOptions) -> [String: Any] {
    let encodedKey = Data(key.utf8)
    return [
      kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: servicePrefix + (options.keychainService ?? "app"),
      kSecAttrGeneric as String: encodedKey,
      kSecAttrAccount as String: encodedKey
    ]
  }

  private func get(_ key: String, _ options: SandboxSecureStoreOptions) throws -> String? {
    var search = query(try validate(key), options)
    search[kSecMatchLimit as String] = kSecMatchLimitOne
    search[kSecReturnData as String] = true
    var result: AnyObject?
    let status = SecItemCopyMatching(search as CFDictionary, &result)
    switch status {
    case errSecSuccess:
      return (result as? Data).flatMap { String(data: $0, encoding: .utf8) }
    case errSecItemNotFound:
      return nil
    default:
      throw SandboxSecureStoreException("Keychain error \(status)")
    }
  }

  private func set(_ value: String, _ key: String, _ options: SandboxSecureStoreOptions) throws -> Bool {
    if options.requireAuthentication {
      throw SandboxSecureStoreException("requireAuthentication is not available to apps in OpenCuria")
    }
    let item = query(try validate(key), options)
    SecItemDelete(item as CFDictionary)
    var add = item
    add[kSecValueData as String] = Data(value.utf8)
    add[kSecAttrAccessible as String] = accessible(options.keychainAccessible)
    let status = SecItemAdd(add as CFDictionary, nil)
    guard status == errSecSuccess else {
      throw SandboxSecureStoreException("Keychain error \(status)")
    }
    return true
  }

  private func accessible(_ value: Int) -> CFString {
    switch value {
    case 0: return kSecAttrAccessibleAfterFirstUnlock
    case 1: return kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
    case 3: return kSecAttrAccessibleWhenPasscodeSetThisDeviceOnly
    case 6: return kSecAttrAccessibleWhenUnlockedThisDeviceOnly
    // ALWAYS* are deprecated by Apple; the nearest supported level applies.
    case 2: return kSecAttrAccessibleAfterFirstUnlock
    case 4: return kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
    default: return kSecAttrAccessibleWhenUnlocked
    }
  }
}

final class SandboxSecureStoreException: GenericException<String>, @unchecked Sendable {
  override var reason: String { param }
}
