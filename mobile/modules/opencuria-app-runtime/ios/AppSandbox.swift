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
  let files: AppFileAccess

  init(launch: AppLaunch) {
    self.launch = launch
    self.cacheDir = AppSandbox.cacheDirectory(appId: launch.appId)
    try? FileManager.default.createDirectory(at: launch.dataDir, withIntermediateDirectories: true)
    try? FileManager.default.createDirectory(at: cacheDir, withIntermediateDirectories: true)
    self.files = AppFileAccess(dataDir: launch.dataDir, cacheDir: cacheDir, installDir: launch.installDir)
  }

  /**
   Where `mobile/src/apps/appInstaller.ts` installs an app (`appLayout`):
   `opencuria-apps/<appId>` in OpenCuria's documents (`Paths.document`).
   */
  static func installDirectory(appId: String) -> URL {
    FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
      .appendingPathComponent("opencuria-apps", isDirectory: true)
      .appendingPathComponent(appId, isDirectory: true)
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
    let appContext = AppContext(
      config: AppContextConfig(
        documentDirectory: launch.dataDir,
        cacheDirectory: cacheDir,
        appGroups: nil
      )
    )
    let fileSystem = FileSystemManager(
      documentDirectory: launch.dataDir.path,
      cachesDirectory: cacheDir.path,
      applicationSupportDirectory: launch.dataDir
        .appendingPathComponent("Application Support", isDirectory: true).path
    )
    // Every Expo permission check of this context asks the app's policy;
    // without it, paths outside these directories would be readable and
    // writable (see `patches/expo-modules-core+*.patch`).
    let files = self.files
    fileSystem.pathPermissionsOverride = { url in files.permissions(for: url) }
    appContext.fileSystem = fileSystem
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

/**
 The autolinked Expo modules without the ones only OpenCuria itself may
 call: its local modules (`mobile/modules`, Swift modules `OpenCuria*`), the
 development client and the home screen widgets.
 `mobile/scripts/sync-app-runtime.mjs` keeps the same packages out of the
 runtime apps are built against, and `src/apps/appRuntimeHostModules.test.ts`
 checks that their native code matches these names.
 */
final class SandboxModulesProvider: ModulesProvider {
  private static let hostOnlySwiftModules: Set<String> = ["ExpoWidgets", "EXDevMenu", "EXDevLauncher"]

  private let base = AppContext.modulesProvider()

  static func isHostOnly(_ type: AnyModule.Type) -> Bool {
    let swiftModule = String(reflecting: type).split(separator: ".").first.map(String.init) ?? ""
    return swiftModule.hasPrefix("OpenCuria") || hostOnlySwiftModules.contains(swiftModule)
  }

  override func getModuleClasses() -> [ExpoModuleTupleType] {
    base.getModuleClasses().filter { !SandboxModulesProvider.isHostOnly($0.module) }
  }

  override func getAppCodeSignEntitlements() -> AppCodeSignEntitlements {
    base.getAppCodeSignEntitlements()
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
