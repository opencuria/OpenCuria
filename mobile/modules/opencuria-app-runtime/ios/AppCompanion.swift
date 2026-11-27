// Copyright OpenCuria contributors. AGPL-3.0-only.

import Expo
import React
import UIKit
import UIKit.UIGestureRecognizerSubclass

/**
 The layer above an open app that holds the app companion. Its touch mode
 decides which touches it takes: none (hidden; the app gets everything), all
 (the sheet, pointer mode), or only those inside one of its regions (the
 recording bar); every other touch falls through to the app below.
 */
final class AppCompanionLayer: UIView {
  enum TouchMode: String {
    case none
    case all
    case regions
  }

  private(set) var touchMode: TouchMode = .none
  /// The companion's own controls, in layer points.
  private var regions: [CGRect] = []

  override init(frame: CGRect) {
    super.init(frame: frame)
    backgroundColor = .clear
    isHidden = true
  }

  @available(*, unavailable)
  required init?(coder: NSCoder) {
    fatalError("init(coder:) is not supported")
  }

  /// `values` are `[x, y, width, height]` groups in points.
  func setTouchMode(_ mode: TouchMode, regions values: [Double] = []) {
    var rects: [CGRect] = []
    var index = 0
    while index + 3 < values.count {
      rects.append(CGRect(x: values[index], y: values[index + 1], width: values[index + 2], height: values[index + 3]))
      index += 4
    }
    regions = rects
    touchMode = mode
    isHidden = mode == .none
  }

  /// Whether a point (layer points) is on one of the companion's controls.
  func isOwnControl(_ point: CGPoint) -> Bool {
    regions.contains { $0.contains(point) }
  }

  override func hitTest(_ point: CGPoint, with event: UIEvent?) -> UIView? {
    switch touchMode {
    case .none:
      return nil
    case .all:
      return super.hitTest(point, with: event)
    case .regions:
      // Declining the touch hands it, and its whole gesture, to the app.
      return isOwnControl(point) ? super.hitTest(point, with: event) : nil
    }
  }
}

/**
 The app companion over an open app: a surface of OpenCuria's own React
 instance (component `OpenCuriaAppCompanion`, `src/apps/companion`) in an
 `AppCompanionLayer`. OpenCuria's factory comes from the app delegate
 (`ExpoReactNativeFactoryProvider`); without it the native app menu shows.
 */
final class AppCompanionHost {
  static let component = "OpenCuriaAppCompanion"

  let layer = AppCompanionLayer()
  private let launch: AppLaunch
  private var rootView: UIView?

  init(launch: AppLaunch) {
    self.launch = launch
  }

  /// OpenCuria's own React Native factory, which draws the companion (main thread).
  private static var factory: RCTReactNativeFactory? {
    MainActor.assumeIsolated {
      guard let delegate = UIApplication.shared.delegate else { return nil }
      if let provider = delegate as? ExpoReactNativeFactoryProvider,
        let factory = provider.reactNativeFactory {
        return factory
      }
      return storedFactory(of: delegate)
    }
  }

  /**
   Expo's app delegate template keeps the factory in `reactNativeFactory`
   without declaring `ExpoReactNativeFactoryProvider`; read that property.
   */
  static func storedFactory(of delegate: Any) -> RCTReactNativeFactory? {
    var mirror: Mirror? = Mirror(reflecting: delegate)
    while let current = mirror {
      for child in current.children where child.label == "reactNativeFactory" {
        if let factory = child.value as? RCTReactNativeFactory {
          return factory
        }
      }
      mirror = current.superclassMirror
    }
    return nil
  }

  var isAvailable: Bool {
    Self.factory != nil
  }

  var exists: Bool {
    rootView != nil
  }

  /// Show the companion: create it on first use, else ask it to open.
  @discardableResult
  func open() -> Bool {
    guard let factory = Self.factory else { return false }
    layer.setTouchMode(.all)
    if rootView != nil {
      OpenCuriaAppRuntimeModule.emitCompanionOpen()
      return true
    }
    let view = factory.rootViewFactory.view(
      withModuleName: Self.component,
      initialProperties: initialProperties(),
      launchOptions: nil
    )
    // The factory paints root views with the system background.
    view.backgroundColor = .clear
    view.frame = layer.bounds
    view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
    layer.addSubview(view)
    rootView = view
    return true
  }

  func destroy() {
    rootView?.removeFromSuperview()
    rootView = nil
    layer.setTouchMode(.none)
  }

  private func initialProperties() -> [String: Any] {
    var properties: [String: Any] = [
      "appId": launch.appId,
      "appName": launch.name,
      "versionId": launch.versionId,
      "open": true
    ]
    if let companion = launch.companion {
      properties["scope"] = companion.scope
      properties["versionName"] = companion.versionName
      if let workspaceId = companion.workspaceId {
        properties["workspaceId"] = workspaceId
      }
    }
    if let iconPath = launch.iconPath {
      properties["iconUri"] = URL(fileURLWithPath: iconPath).absoluteString
    }
    return properties
  }
}

/**
 Sees every touch on the app's screen without taking part: it never
 recognizes, never delays or cancels touches and lets every other gesture
 recognize alongside. Reports the first finger of each touch sequence.
 */
final class AppTouchObserver: UIGestureRecognizer, UIGestureRecognizerDelegate {
  var onTouch: ((UITouch, String) -> Void)?
  private weak var tracked: UITouch?

  override init(target: Any?, action: Selector?) {
    super.init(target: target, action: action)
    cancelsTouchesInView = false
    delaysTouchesBegan = false
    delaysTouchesEnded = false
    delegate = self
  }

  override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent) {
    if tracked == nil, let touch = touches.first {
      tracked = touch
      onTouch?(touch, "down")
    }
  }

  override func touchesMoved(_ touches: Set<UITouch>, with event: UIEvent) {
    if let touch = tracked, touches.contains(touch) {
      onTouch?(touch, "move")
    }
  }

  override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent) {
    finish(touches, event: event, phase: "up")
  }

  override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent) {
    finish(touches, event: event, phase: "cancel")
  }

  private func finish(_ touches: Set<UITouch>, event: UIEvent, phase: String) {
    if let touch = tracked, touches.contains(touch) {
      onTouch?(touch, phase)
      tracked = nil
    }
    let remaining = event.allTouches?.filter { $0.phase != .ended && $0.phase != .cancelled } ?? []
    if remaining.isEmpty {
      state = .failed
    }
  }

  override func reset() {
    super.reset()
    tracked = nil
  }

  override func canPrevent(_ preventedGestureRecognizer: UIGestureRecognizer) -> Bool {
    false
  }

  override func canBePrevented(by preventingGestureRecognizer: UIGestureRecognizer) -> Bool {
    false
  }

  func gestureRecognizer(
    _ gestureRecognizer: UIGestureRecognizer,
    shouldRecognizeSimultaneouslyWith otherGestureRecognizer: UIGestureRecognizer
  ) -> Bool {
    true
  }
}

/// The inspector script installed apps run (`inspector/app-inspector.js`, a pod resource).
enum AppInspector {
  static let script: String? = {
    for bundle in [Bundle(for: AppRuntimeFactory.self), Bundle.main] {
      if let url = bundle.url(forResource: "app-inspector", withExtension: "js"),
        let text = try? String(contentsOf: url, encoding: .utf8) {
        return text
      }
    }
    return nil
  }()
}
