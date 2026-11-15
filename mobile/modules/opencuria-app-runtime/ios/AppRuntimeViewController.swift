// Copyright OpenCuria contributors. AGPL-3.0-only.

import Combine
import ExpoModulesCore
import React
import ReactAppDependencyProvider
import SwiftUI
import UIKit

/// Points the app's React instance at its installed bundle.
final class AppRuntimeDelegate: RCTDefaultReactNativeFactoryDelegate {
  private let url: URL

  init(bundleURL: URL) {
    self.url = bundleURL
    super.init()
  }

  override func bundleURL() -> URL? {
    url
  }
}

/**
 Runs one installed app full screen in its own React instance, like an Expo Go
 experience: its JavaScript bundle from local storage, every native module
 from OpenCuria. The instance ends with this controller. Shaking, a
 three-finger long press and the floating menu button open the app menu, each
 as switched on there (`AppMenuTriggers`); a note at the bottom says how when
 the app opens.
 */
final class AppRuntimeViewController: UIViewController, UIAdaptivePresentationControllerDelegate {
  /// How long the close note waits for the app's first screen.
  private static let closeHintFallback: TimeInterval = 2
  static weak var current: AppRuntimeViewController?

  private let launch: AppLaunch
  private let sandbox: AppSandbox
  private var delegate: AppRuntimeDelegate?
  private var factory: AppRuntimeFactory?
  private var rootView: UIView?
  private var loadingView: UIView?
  private var contentObserver: NSObjectProtocol?
  private var keyboardObserver: NSObjectProtocol?
  private var triggersObserver: AnyCancellable?
  private var closeHintShown = false
  /// Identifies the running instance; callbacks of a stopped one are ignored.
  private var instance = 0

  private lazy var menuGesture: UILongPressGestureRecognizer = {
    let gesture = UILongPressGestureRecognizer(target: self, action: #selector(openMenuByPress(_:)))
    gesture.numberOfTouchesRequired = 3
    gesture.minimumPressDuration = 0.6
    gesture.cancelsTouchesInView = false
    return gesture
  }()

  private lazy var menuButton = AppMenuButton { [weak self] in
    self?.showMenu()
  }

  init(launch: AppLaunch) {
    self.launch = launch
    self.sandbox = AppSandbox(launch: launch)
    super.init(nibName: nil, bundle: nil)
    modalPresentationStyle = .fullScreen
  }

  @available(*, unavailable)
  required init?(coder: NSCoder) {
    fatalError("init(coder:) is not supported")
  }

  override var supportedInterfaceOrientations: UIInterfaceOrientationMask {
    launch.supportedOrientations
  }

  deinit {
    if let keyboardObserver {
      NotificationCenter.default.removeObserver(keyboardObserver)
    }
    AppGateway.close(launch.gateway)
  }

  override func viewDidLoad() {
    super.viewDidLoad()
    AppRuntimeViewController.current = self
    AppGateway.open(launch.gateway)
    view.backgroundColor = launch.backgroundColor ?? .systemBackground
    view.addGestureRecognizer(menuGesture)
    view.addSubview(menuButton)
    triggersObserver = AppMenuTriggers.shared.$enabled.sink { [weak self] enabled in
      self?.applyTriggers(enabled)
    }
    // Shaking reaches this controller only as first responder; take that
    // role back when the app's text field lets go of the keyboard.
    keyboardObserver = NotificationCenter.default.addObserver(
      forName: UIResponder.keyboardDidHideNotification,
      object: nil,
      queue: .main
    ) { [weak self] _ in
      self?.claimShake()
    }
    start()
  }

  override func viewDidAppear(_ animated: Bool) {
    super.viewDidAppear(animated)
    claimShake()
    DispatchQueue.main.asyncAfter(deadline: .now() + Self.closeHintFallback) { [weak self] in
      self?.showCloseHint()
    }
  }

  override func viewDidLayoutSubviews() {
    super.viewDidLayoutSubviews()
    menuButton.superviewDidLayout()
  }

  /// Arm the ways that open the app menu (`enabled` is the new value).
  private func applyTriggers(_ enabled: Set<AppMenuTrigger>) {
    menuGesture.isEnabled = enabled.contains(.threeFingerPress)
    menuButton.isOn = enabled.contains(.button)
  }

  override var canBecomeFirstResponder: Bool {
    true
  }

  override func motionEnded(_ motion: UIEvent.EventSubtype, with event: UIEvent?) {
    guard motion == .motionShake, AppMenuTriggers.shared.isEnabled(.shake) else {
      super.motionEnded(motion, with: event)
      return
    }
    showMenu()
  }

  /// Become first responder for shake events unless something of the app (e.g. a text field) is.
  private func claimShake() {
    guard view.window != nil, presentedViewController == nil, !isFirstResponder else { return }
    let current = UIResponder.opencuriaFirstResponder
    if current == nil || current is UIWindow || current is UIApplication {
      becomeFirstResponder()
    }
  }

  /// Release level of OpenCuria's own instance; React Native requires a match.
  private var releaseLevel: RCTReleaseLevel {
    switch Bundle.main.object(forInfoDictionaryKey: "ReactNativeReleaseLevel") as? String {
    case "canary": return .Canary
    case "experimental": return .Experimental
    default: return .Stable
    }
  }

  private func start() {
    let delegate = AppRuntimeDelegate(bundleURL: launch.bundleURL)
    delegate.dependencyProvider = RCTAppDependencyProvider()
    let factory = AppRuntimeFactory(delegate: delegate, releaseLevel: releaseLevel)
    let sandbox = self.sandbox
    instance += 1
    let id = instance
    // A stopped instance may still report errors or reloads while React
    // Native tears it down; they must not end its successor (main thread).
    factory.makeAppContext = { [weak self] in
      sandbox.makeAppContext(reload: {
        guard let self, self.instance == id else { return }
        self.restart()
      })
    }
    factory.onFatalError = { [weak self] message in
      guard let self, self.instance == id else { return }
      self.showError(message)
    }
    let root = factory.rootViewFactory.view(
      withModuleName: "main",
      initialProperties: nil,
      launchOptions: nil
    )
    root.frame = view.bounds
    root.autoresizingMask = [.flexibleWidth, .flexibleHeight]
    root.backgroundColor = .clear
    // Beneath the menu button and the close note.
    view.insertSubview(root, at: 0)
    self.delegate = delegate
    self.factory = factory
    self.rootView = root
    showLoading(over: root)
  }

  private func stop() {
    instance += 1
    if let contentObserver {
      NotificationCenter.default.removeObserver(contentObserver)
    }
    contentObserver = nil
    loadingView?.removeFromSuperview()
    loadingView = nil
    rootView?.removeFromSuperview()
    rootView = nil
    factory?.invalidate()
    factory = nil
    delegate = nil
  }

  private func restart() {
    guard isViewLoaded, view.window != nil || presentingViewController != nil else { return }
    presentedViewController?.dismiss(animated: false)
    stop()
    start()
  }

  /// Whether this controller runs the version a launch asks for.
  func shows(_ other: AppLaunch) -> Bool {
    launch.appId == other.appId && launch.versionId == other.versionId
  }

  func close(animated: Bool = true) {
    stop()
    AppGateway.close(launch.gateway)
    if AppRuntimeViewController.current === self {
      AppRuntimeViewController.current = nil
    }
    presentingViewController?.dismiss(animated: animated)
  }

  /// The app's icon and a spinner until its first view appears, as in Expo Go.
  private func showLoading(over root: UIView) {
    let overlay = UIView(frame: view.bounds)
    overlay.autoresizingMask = [.flexibleWidth, .flexibleHeight]
    overlay.backgroundColor = view.backgroundColor
    overlay.accessibilityLabel = "Opening \(launch.name)"
    let stack = UIStackView()
    stack.axis = .vertical
    stack.alignment = .center
    stack.spacing = 24
    stack.translatesAutoresizingMaskIntoConstraints = false
    if let path = launch.iconPath, let image = UIImage(contentsOfFile: path) {
      let icon = UIImageView(image: image)
      icon.layer.cornerRadius = 22
      icon.clipsToBounds = true
      icon.widthAnchor.constraint(equalToConstant: 96).isActive = true
      icon.heightAnchor.constraint(equalToConstant: 96).isActive = true
      stack.addArrangedSubview(icon)
    }
    let spinner = UIActivityIndicatorView(style: .medium)
    spinner.startAnimating()
    stack.addArrangedSubview(spinner)
    overlay.addSubview(stack)
    NSLayoutConstraint.activate([
      stack.centerXAnchor.constraint(equalTo: overlay.centerXAnchor),
      stack.centerYAnchor.constraint(equalTo: overlay.centerYAnchor)
    ])
    view.insertSubview(overlay, aboveSubview: root)
    loadingView = overlay
    contentObserver = NotificationCenter.default.addObserver(
      forName: NSNotification.Name("RCTContentDidAppearNotification"),
      object: nil,
      queue: .main
    ) { [weak self, weak overlay] _ in
      overlay?.removeFromSuperview()
      if let self, self.loadingView === overlay {
        self.loadingView = nil
        self.showCloseHint()
      }
    }
  }

  private func showError(_ message: String) {
    guard isViewLoaded else { return }
    stop()
    // Without React Native's stack and component stack, as on Android.
    let text = message
      .components(separatedBy: ", stack:").first?
      .components(separatedBy: "\n\nThis error is located at:").first?
      .trimmingCharacters(in: .whitespacesAndNewlines) ?? message
    let alert = UIAlertController(
      title: "\(launch.name) stopped",
      message: String(text.prefix(600)),
      preferredStyle: .alert
    )
    alert.addAction(UIAlertAction(title: "Close app", style: .cancel) { [weak self] _ in
      self?.close()
    })
    alert.addAction(UIAlertAction(title: "Reload", style: .default) { [weak self] _ in
      self?.start()
    })
    // The app menu or a modal of the app would keep the alert from showing,
    // leaving a blank screen without a way to close the app.
    if let presented = presentedViewController {
      presented.dismiss(animated: false) { [weak self] in
        self?.present(alert, animated: true)
      }
    } else {
      present(alert, animated: true)
    }
  }

  @objc private func openMenuByPress(_ gesture: UILongPressGestureRecognizer) {
    guard gesture.state == .began, presentedViewController == nil else { return }
    UIImpactFeedbackGenerator(style: .light).impactOccurred()
    showMenu()
  }

  /// The app menu as a sheet: half height, pulled up to full height when needed.
  private func showMenu() {
    guard isViewLoaded, view.window != nil, presentedViewController == nil else { return }
    let menu = UIHostingController(rootView: AppMenuView(
      name: launch.name,
      icon: launch.iconPath.flatMap(UIImage.init(contentsOfFile:)),
      triggers: .shared,
      onDismiss: { [weak self] in
        self?.dismiss(animated: true) {
          self?.claimShake()
        }
      },
      onCloseApp: { [weak self] in
        self?.close()
      },
      onReload: { [weak self] in
        self?.restart()
      }
    ))
    menu.modalPresentationStyle = .pageSheet
    if let sheet = menu.sheetPresentationController {
      sheet.detents = [.medium(), .large()]
      sheet.prefersGrabberVisible = true
    }
    menu.presentationController?.delegate = self
    present(menu, animated: true)
  }

  /// The menu was swiped away.
  func presentationControllerDidDismiss(_ presentationController: UIPresentationController) {
    claimShake()
  }

  /// A small note at the bottom on how to close the app; it fades after a few seconds.
  private func showCloseHint() {
    guard !closeHintShown, isViewLoaded, view.window != nil else { return }
    closeHintShown = true
    let text = AppMenuTriggers.shared.closeHint
    let label = UILabel()
    label.text = text
    label.font = .preferredFont(forTextStyle: .footnote)
    label.adjustsFontForContentSizeCategory = true
    label.textColor = .white
    label.textAlignment = .center
    label.numberOfLines = 0
    label.translatesAutoresizingMaskIntoConstraints = false
    let hint = UIView()
    hint.backgroundColor = UIColor.black.withAlphaComponent(0.7)
    hint.layer.cornerRadius = 16
    hint.layer.cornerCurve = .continuous
    hint.isUserInteractionEnabled = false
    hint.alpha = 0
    hint.translatesAutoresizingMaskIntoConstraints = false
    hint.addSubview(label)
    view.addSubview(hint)
    NSLayoutConstraint.activate([
      label.topAnchor.constraint(equalTo: hint.topAnchor, constant: 7),
      label.bottomAnchor.constraint(equalTo: hint.bottomAnchor, constant: -7),
      label.leadingAnchor.constraint(equalTo: hint.leadingAnchor, constant: 14),
      label.trailingAnchor.constraint(equalTo: hint.trailingAnchor, constant: -14),
      hint.centerXAnchor.constraint(equalTo: view.centerXAnchor),
      hint.leadingAnchor.constraint(
        greaterThanOrEqualTo: view.safeAreaLayoutGuide.leadingAnchor,
        constant: 24
      ),
      hint.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -16)
    ])
    UIAccessibility.post(notification: .announcement, argument: text)
    UIView.animate(withDuration: 0.3) {
      hint.alpha = 1
    } completion: { _ in
      UIView.animate(withDuration: 0.3, delay: 4, options: []) {
        hint.alpha = 0
      } completion: { _ in
        hint.removeFromSuperview()
      }
    }
  }
}

private extension UIResponder {
  private static weak var reportedFirstResponder: UIResponder?

  /// The current first responder, found through the responder chain; a window or the application when there is none.
  static var opencuriaFirstResponder: UIResponder? {
    reportedFirstResponder = nil
    UIApplication.shared.sendAction(
      #selector(opencuriaReportFirstResponder),
      to: nil,
      from: nil,
      for: nil
    )
    return reportedFirstResponder
  }

  @objc func opencuriaReportFirstResponder() {
    UIResponder.reportedFirstResponder = self
  }
}
