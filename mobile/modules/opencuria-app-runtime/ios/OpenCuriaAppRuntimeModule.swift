// Copyright OpenCuria contributors. AGPL-3.0-only.

import Expo
import ExpoModulesCore

/// Opens installed apps and removes their native-side data on uninstall.
public final class OpenCuriaAppRuntimeModule: Module {
  public func definition() -> ModuleDefinition {
    Name("OpenCuriaAppRuntime")

    OnCreate {
      // Hooks are global and idempotent; OpenCuria's own instance installs
      // them before any app runs.
      AppGateway.install()
      Task { @MainActor in AppGatewayFetch.install() }
    }

    AsyncFunction("open") { (request: String) in
      let launch = try AppLaunch.parse(request)
      guard let presenter = self.appContext?.utilities?.currentViewController() else {
        throw MissingCurrentViewControllerException()
      }
      // One app at a time: an open app closes before the next one starts.
      AppRuntimeViewController.current?.close()
      presenter.present(AppRuntimeViewController(launch: launch), animated: true)
    }.runOnQueue(.main)

    AsyncFunction("clearData") { (appId: String) in
      try AppSandbox.clear(appId: appId)
    }
  }
}

/// Routes `expo/fetch` requests for app backends through `AppGatewayURLProtocol`.
@MainActor
enum AppGatewayFetch {
  private static var installed = false

  static func install() {
    guard !installed else { return }
    installed = true
    ExpoFetchCustomExtension.setCustomURLSessionConfigurationProvider(configuration)
  }

  /// Expo's default fetch configuration plus the gateway protocol (any queue).
  nonisolated static func configuration() -> URLSessionConfiguration? {
    let config = URLSessionConfiguration.default
    config.httpShouldSetCookies = true
    config.httpCookieAcceptPolicy = .always
    config.httpCookieStorage = HTTPCookieStorage.shared
    if Bundle.main.infoDictionary?["ReactNetworkForceWifiOnly"] as? Bool == true {
      config.allowsCellularAccess = false
    }
    config.protocolClasses = [AppGatewayURLProtocol.self] + (config.protocolClasses ?? [])
    return config
  }
}

final class MissingCurrentViewControllerException: Exception, @unchecked Sendable {
  override var reason: String {
    "OpenCuria has no screen to present the app on."
  }
}
