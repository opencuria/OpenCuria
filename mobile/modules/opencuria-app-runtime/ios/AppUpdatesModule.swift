// Copyright OpenCuria contributors. AGPL-3.0-only.

import ExpoModulesCore

/**
 The `ExpoUpdates` module as an installed app sees it (Expo Go does the same).

 `expo-constants` reads the app's Expo config from `manifestString`, and
 `expo-asset` resolves every bundled asset through `localAssets` to the
 downloaded files, so images and fonts work offline. Only registered in app
 instances, never in OpenCuria's own React instance.
 */
final class AppUpdatesModule: Module {
  private let launch: AppLaunch
  private let reload: () -> Void
  private let launchedAt = Date().timeIntervalSince1970 * 1000

  // swiftlint:disable:next unavailable_function
  required init(appContext: AppContext) {
    fatalError("Use init(appContext:launch:reload:)")
  }

  init(appContext: AppContext, launch: AppLaunch, reload: @escaping () -> Void) {
    self.launch = launch
    self.reload = reload
    super.init(appContext: appContext)
  }

  func definition() -> ModuleDefinition {
    Name("ExpoUpdates")

    Constants {
      [
        "isEnabled": true,
        "isEmbeddedLaunch": false,
        "isEmergencyLaunch": false,
        "isUsingEmbeddedAssets": false,
        "runtimeVersion": launch.manifest["runtimeVersion"] as? String ?? "",
        "checkAutomatically": "NEVER",
        "channel": "",
        "shouldDeferToNativeForAPIMethodAvailabilityInDevelopment": false,
        "updateId": launch.versionId,
        "commitTime": launchedAt,
        "manifestString": launch.manifestString,
        "localAssets": launch.localAssets,
        "initialContext": [
          "isUpdateAvailable": false,
          "isUpdatePending": false,
          "isChecking": false,
          "isDownloading": false,
          "isRestarting": false,
          "restartCount": 0,
          "sequenceNumber": 0,
          "downloadProgress": 0.0
        ] as [String: Any]
      ]
    }

    AsyncFunction("reload") { (_: [String: Any]?) in
      DispatchQueue.main.async { self.reload() }
    }

    AsyncFunction("checkForUpdateAsync") { () throws in
      throw NotAvailableInOpenCuriaException("checkForUpdateAsync")
    }

    AsyncFunction("fetchUpdateAsync") { () throws in
      throw NotAvailableInOpenCuriaException("fetchUpdateAsync")
    }

    AsyncFunction("getExtraParamsAsync") { () throws in
      throw NotAvailableInOpenCuriaException("getExtraParamsAsync")
    }
  }
}

final class NotAvailableInOpenCuriaException: GenericException<String>, @unchecked Sendable {
  override var reason: String {
    "\(param)() is not available in OpenCuria; the agent publishes new versions."
  }
}
