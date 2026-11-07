// Copyright OpenCuria contributors. AGPL-3.0-only.

import ExpoModulesCore

/// Opens installed apps and removes their native-side data on uninstall.
public final class OpenCuriaAppRuntimeModule: Module {
  public func definition() -> ModuleDefinition {
    Name("OpenCuriaAppRuntime")

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

final class MissingCurrentViewControllerException: Exception, @unchecked Sendable {
  override var reason: String {
    "OpenCuria has no screen to present the app on."
  }
}
