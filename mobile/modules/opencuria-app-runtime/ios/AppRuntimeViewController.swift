// Copyright OpenCuria contributors. AGPL-3.0-only.

import ExpoModulesCore
import React
import ReactAppDependencyProvider
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
 from OpenCuria. The instance ends with this controller. A three-finger long
 press opens the app menu (reload, close).
 */
final class AppRuntimeViewController: UIViewController {
  private static let hintKey = "opencuria.app-runtime.menu-hint-shown"
  static weak var current: AppRuntimeViewController?

  private let launch: AppLaunch
  private let sandbox: AppSandbox
  private var delegate: AppRuntimeDelegate?
  private var factory: AppRuntimeFactory?
  private var rootView: UIView?
  private var loadingView: UIView?
  private var contentObserver: NSObjectProtocol?
  /// Identifies the running instance; callbacks of a stopped one are ignored.
  private var instance = 0

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
    AppGateway.close(launch.gateway)
  }

  override func viewDidLoad() {
    super.viewDidLoad()
    AppRuntimeViewController.current = self
    AppGateway.open(launch.gateway)
    view.backgroundColor = launch.backgroundColor ?? .systemBackground
    let menuGesture = UILongPressGestureRecognizer(target: self, action: #selector(openMenu(_:)))
    menuGesture.numberOfTouchesRequired = 3
    menuGesture.minimumPressDuration = 0.6
    menuGesture.cancelsTouchesInView = false
    view.addGestureRecognizer(menuGesture)
    start()
  }

  override func viewDidAppear(_ animated: Bool) {
    super.viewDidAppear(animated)
    showMenuHintOnce()
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
    view.addSubview(root)
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

  func close() {
    stop()
    AppGateway.close(launch.gateway)
    if AppRuntimeViewController.current === self {
      AppRuntimeViewController.current = nil
    }
    presentingViewController?.dismiss(animated: true)
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
    view.addSubview(overlay)
    loadingView = overlay
    contentObserver = NotificationCenter.default.addObserver(
      forName: NSNotification.Name("RCTContentDidAppearNotification"),
      object: nil,
      queue: .main
    ) { [weak self, weak overlay] _ in
      overlay?.removeFromSuperview()
      if let self, self.loadingView === overlay {
        self.loadingView = nil
      }
    }
  }

  private func showError(_ message: String) {
    guard isViewLoaded else { return }
    stop()
    let text = message.components(separatedBy: ", stack:").first ?? message
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

  @objc private func openMenu(_ gesture: UILongPressGestureRecognizer) {
    guard gesture.state == .began, presentedViewController == nil else { return }
    let menu = UIAlertController(title: launch.name, message: nil, preferredStyle: .actionSheet)
    menu.addAction(UIAlertAction(title: "Reload", style: .default) { [weak self] _ in
      self?.restart()
    })
    menu.addAction(UIAlertAction(title: "Close app", style: .destructive) { [weak self] _ in
      self?.close()
    })
    menu.addAction(UIAlertAction(title: "Cancel", style: .cancel))
    if let popover = menu.popoverPresentationController {
      popover.sourceView = view
      popover.sourceRect = CGRect(origin: gesture.location(in: view), size: .zero)
    }
    present(menu, animated: true)
  }

  private func showMenuHintOnce() {
    let defaults = UserDefaults.standard
    guard !defaults.bool(forKey: AppRuntimeViewController.hintKey) else { return }
    defaults.set(true, forKey: AppRuntimeViewController.hintKey)
    let hint = UILabel()
    hint.text = "  Long-press with three fingers for the app menu  "
    hint.font = .preferredFont(forTextStyle: .footnote)
    hint.textColor = .white
    hint.backgroundColor = UIColor.black.withAlphaComponent(0.75)
    hint.layer.cornerRadius = 14
    hint.clipsToBounds = true
    hint.translatesAutoresizingMaskIntoConstraints = false
    view.addSubview(hint)
    NSLayoutConstraint.activate([
      hint.centerXAnchor.constraint(equalTo: view.centerXAnchor),
      hint.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -24),
      hint.heightAnchor.constraint(equalToConstant: 32)
    ])
    UIView.animate(withDuration: 0.3, delay: 3.5, options: []) {
      hint.alpha = 0
    } completion: { _ in
      hint.removeFromSuperview()
    }
  }
}
