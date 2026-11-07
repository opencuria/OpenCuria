// Copyright OpenCuria contributors. AGPL-3.0-only.

import ExpoModulesCore
import Foundation

/**
 Storage of one installed app, separate from OpenCuria and from other apps,
 as Expo Go scopes each experience: documents, cache and SecureStore entries.
 */
final class AppSandbox {
  let launch: AppLaunch
  let cacheDir: URL

  init(launch: AppLaunch) {
    self.launch = launch
    self.cacheDir = AppSandbox.cacheDirectory(appId: launch.appId)
  }

  static func cacheDirectory(appId: String) -> URL {
    FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
      .appendingPathComponent("opencuria-apps", isDirectory: true)
      .appendingPathComponent(appId, isDirectory: true)
  }

  /// Prefix of the app's Keychain services, see `SandboxSecureStoreModule`.
  static func keychainPrefix(appId: String) -> String {
    "opencuria-app-\(appId):"
  }

  /// Remove cache and SecureStore entries of an uninstalled app.
  static func clear(appId: String) throws {
    guard AppLaunch.isValidId(appId) else {
      throw AppLaunchError.invalid("Invalid app id")
    }
    try? FileManager.default.removeItem(at: cacheDirectory(appId: appId))
    SandboxSecureStoreModule.deleteAll(prefix: keychainPrefix(appId: appId))
  }

  /// The Expo context of one React instance of this app.
  func makeAppContext(reload: @escaping () -> Void) -> AppContext {
    let fileManager = FileManager.default
    try? fileManager.createDirectory(at: launch.dataDir, withIntermediateDirectories: true)
    try? fileManager.createDirectory(at: cacheDir, withIntermediateDirectories: true)

    let appContext = AppContext(
      config: AppContextConfig(
        documentDirectory: launch.dataDir,
        cacheDirectory: cacheDir,
        appGroups: nil
      )
    )
    appContext.fileSystem = FileSystemManager(
      documentDirectory: launch.dataDir.path,
      cachesDirectory: cacheDir.path,
      applicationSupportDirectory: launch.dataDir
        .appendingPathComponent("Application Support", isDirectory: true).path
    )
    appContext.constants = SandboxConstants(base: appContext.constants, launch: launch)
    // Registered first and protected, so the generated provider cannot replace them.
    appContext.moduleRegistry.register(
      module: AppUpdatesModule(appContext: appContext, launch: launch, reload: reload),
      name: nil,
      preventModuleOverriding: true
    )
    appContext.moduleRegistry.register(
      module: SandboxSecureStoreModule(
        appContext: appContext,
        servicePrefix: AppSandbox.keychainPrefix(appId: launch.appId)
      ),
      name: nil,
      preventModuleOverriding: true
    )
    return appContext
  }
}

/// `Constants.manifest` reports the app's own Expo config.
final class SandboxConstants: NSObject, EXConstantsInterface {
  private let base: EXConstantsInterface?
  private let launch: AppLaunch

  init(base: EXConstantsInterface?, launch: AppLaunch) {
    self.base = base
    self.launch = launch
  }

  func constants() -> [AnyHashable: Any] {
    var values = base?.constants() ?? [:]
    values["manifest"] = launch.expoConfig
    values["appOwnership"] = NSNull()
    return values
  }
}
