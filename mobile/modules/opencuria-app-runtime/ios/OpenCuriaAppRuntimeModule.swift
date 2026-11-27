// Copyright OpenCuria contributors. AGPL-3.0-only.

import Expo
import ExpoModulesCore

/**
 Opens installed apps, removes their native-side data on uninstall and
 serves the app companion drawn over an open app: its touch layer, the app
 menu's actions, and what recordings observe of the app (`AppRecorder`).
 */
public final class OpenCuriaAppRuntimeModule: Module, AppRecorderListener {
  /// The module of OpenCuria's own instance, which emits the companion's events.
  private static weak var current: OpenCuriaAppRuntimeModule?

  /// Native asks the companion to show (shake, three fingers, menu button).
  static func emitCompanionOpen() {
    current?.sendEvent("onCompanionOpen", [:])
  }

  func appRecorder(touch event: [String: Any]) {
    sendEvent("onAppTouch", event.mapValues { Optional($0) })
  }

  func appRecorder(event: [String: Any]) {
    sendEvent("onAppEvent", event.mapValues { Optional($0) })
  }

  public func definition() -> ModuleDefinition {
    Name("OpenCuriaAppRuntime")

    Events("onCompanionOpen", "onAppTouch", "onAppEvent")

    OnCreate {
      // Hooks are global and idempotent; OpenCuria's own instance installs
      // them before any app runs.
      AppGateway.install()
      Task { @MainActor in AppGatewayFetch.install() }
      OpenCuriaAppRuntimeModule.current = self
      AppRecorder.shared.listener = self
    }

    AsyncFunction("open") { (request: String) in
      let launch = try AppLaunch.parse(request)
      var host = self.appContext?.utilities?.currentViewController()
      // One app at a time. The app that is open (e.g. tapped again on the
      // home screen widget) stays as it is; another one replaces it.
      if let current = AppRuntimeViewController.current {
        if current.shows(launch) {
          return
        }
        host = current.presentingViewController ?? host
        current.close(animated: false)
      }
      guard let presenter = host else {
        throw MissingCurrentViewControllerException()
      }
      presenter.present(AppRuntimeViewController(launch: launch), animated: true)
    }.runOnQueue(.main)

    AsyncFunction("close") {
      if let current = AppRuntimeViewController.current {
        current.close()
      }
    }.runOnQueue(.main)

    AsyncFunction("clearData") { (appId: String) in
      try AppSandbox.clear(appId: appId)
    }

    // --- App companion ---

    Function("companionReady") {}

    // `regions`: `[x, y, width, height, …]` in points.
    AsyncFunction("setCompanionTouchMode") { (mode: String, regions: [Double]) in
      AppRuntimeViewController.current?.setCompanionTouchMode(mode, regions: regions)
    }.runOnQueue(.main)

    AsyncFunction("reloadOpenApp") {
      AppRuntimeViewController.current?.reloadFromCompanion()
    }.runOnQueue(.main)

    AsyncFunction("menuTriggers") { () -> [String: Bool] in
      OpenCuriaAppRuntimeModule.triggers()
    }.runOnQueue(.main)

    AsyncFunction("setMenuTrigger") { (trigger: String, on: Bool) -> [String: Bool] in
      let which: AppMenuTrigger = trigger == "shake"
        ? .shake
        : trigger == "threeFingerPress" ? .threeFingerPress : .button
      AppMenuTriggers.shared.set(which, on: on)
      return OpenCuriaAppRuntimeModule.triggers()
    }.runOnQueue(.main)

    AsyncFunction("startAppObservation") { (recordingId: String) -> [String: Any] in
      guard let controller = AppRuntimeViewController.current else {
        throw NoOpenAppException()
      }
      return try controller.startObservation(id: recordingId)
    }.runOnQueue(.main)

    AsyncFunction("captureAppFrame") { (_: String, index: Int, previousFrameId: String?, promise: Promise) in
      guard let controller = AppRuntimeViewController.current else {
        promise.reject(NoOpenAppException())
        return
      }
      controller.captureFrame(index: index, previousFrameId: previousFrameId) { result in
        switch result {
        case .success(let frame):
          promise.resolve(frame)
        case .failure(let error):
          promise.reject("ERR_APP_CAPTURE", error.localizedDescription)
        }
      }
    }.runOnQueue(.main)

    AsyncFunction("stopAppObservation") {
      if let controller = AppRuntimeViewController.current {
        controller.stopObservation()
      } else {
        AppRecorder.shared.stop()
      }
    }.runOnQueue(.main)

    AsyncFunction("recentAppEvents") { (since: Double) -> [[String: Any]] in
      AppRecorder.shared.recentEvents(since: since)
    }
  }

  private static func triggers() -> [String: Bool] {
    let triggers = AppMenuTriggers.shared
    return [
      "shake": triggers.isEnabled(.shake),
      "threeFingerPress": triggers.isEnabled(.threeFingerPress),
      "button": triggers.isEnabled(.button)
    ]
  }
}

final class NoOpenAppException: Exception, @unchecked Sendable {
  override var reason: String {
    "No app is open."
  }
}

/**
 Routes `expo/fetch` requests for app backends through `AppGatewayURLProtocol`
 and keeps it from loading files the open app may not read (`AppFileURLProtocol`).
 */
@MainActor
enum AppGatewayFetch {
  private static var installed = false

  static func install() {
    guard !installed else { return }
    installed = true
    ExpoFetchCustomExtension.setCustomURLSessionConfigurationProvider(configuration)
  }

  /// Expo's default fetch configuration plus the gateway and file protocols (any queue).
  nonisolated static func configuration() -> URLSessionConfiguration? {
    let config = URLSessionConfiguration.default
    config.httpShouldSetCookies = true
    config.httpCookieAcceptPolicy = .always
    config.httpCookieStorage = HTTPCookieStorage.shared
    if Bundle.main.infoDictionary?["ReactNetworkForceWifiOnly"] as? Bool == true {
      config.allowsCellularAccess = false
    }
    config.protocolClasses = [AppFileURLProtocol.self, AppGatewayURLProtocol.self] + (config.protocolClasses ?? [])
    return config
  }
}

final class MissingCurrentViewControllerException: Exception, @unchecked Sendable {
  override var reason: String {
    "OpenCuria has no screen to present the app on."
  }
}
