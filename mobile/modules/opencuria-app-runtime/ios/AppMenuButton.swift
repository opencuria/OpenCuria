// Copyright OpenCuria contributors. AGPL-3.0-only.

import UIKit

/**
 The floating menu button over an app, like Expo Go's tools button: drag it
 anywhere and it rests at the nearest side edge, where every app finds it
 again; it fades while untouched. A tap opens the app menu.
 */
final class AppMenuButton: UIView {
  private static let sideKey = "opencuria.app-runtime.menu-button-side"
  private static let heightKey = "opencuria.app-runtime.menu-button-height"
  private static let size: CGFloat = 44
  private static let margin: CGFloat = 10
  private static let idleDelay: TimeInterval = 5
  private static let idleAlpha: CGFloat = 0.5
  /// How far a fling carries the button, as seconds of its velocity.
  private static let momentum: CGFloat = 0.1
  /// First spot: the right edge, a little below the middle.
  private static let defaultHeight = 0.6

  private let onOpen: () -> Void
  private var dragStart: CGPoint = .zero
  private var isDragging = false
  private var idleTimer: Timer?
  /// The superview's size and safe area the button was last placed in.
  private var placedIn: (CGSize, UIEdgeInsets)?

  /// Whether the menu button is switched on.
  var isOn = false {
    didSet {
      guard isOn != oldValue else { return }
      isHidden = !isOn
      if isOn {
        alpha = 0
        wake()
      } else {
        idleTimer?.invalidate()
      }
    }
  }

  init(onOpen: @escaping () -> Void) {
    self.onOpen = onOpen
    super.init(frame: CGRect(x: 0, y: 0, width: Self.size, height: Self.size))
    isHidden = true
    // OpenCuria's mark background (#0A0A0A), slightly see-through.
    backgroundColor = UIColor(red: 10 / 255, green: 10 / 255, blue: 10 / 255, alpha: 0.92)
    layer.cornerRadius = Self.size / 2
    layer.borderWidth = 1
    layer.borderColor = UIColor(white: 1, alpha: 0.16).cgColor
    layer.shadowColor = UIColor.black.cgColor
    layer.shadowOpacity = 0.3
    layer.shadowRadius = 6
    layer.shadowOffset = CGSize(width: 0, height: 3)
    let mark = OpenCuriaMarkView(frame: bounds)
    mark.autoresizingMask = [.flexibleWidth, .flexibleHeight]
    mark.isUserInteractionEnabled = false
    addSubview(mark)
    isAccessibilityElement = true
    accessibilityLabel = "App menu"
    accessibilityTraits = .button
    addGestureRecognizer(UITapGestureRecognizer(target: self, action: #selector(tap)))
    addGestureRecognizer(UIPanGestureRecognizer(target: self, action: #selector(pan(_:))))
  }

  @available(*, unavailable)
  required init?(coder: NSCoder) {
    fatalError("init(coder:) is not supported")
  }

  deinit {
    idleTimer?.invalidate()
  }

  override func accessibilityActivate() -> Bool {
    onOpen()
    return true
  }

  /// Where the button's origin may rest: the superview's safe area, at least `margin` from each edge.
  private var area: CGRect? {
    guard let superview, superview.bounds.width > 0, superview.bounds.height > 0 else { return nil }
    let insets = superview.safeAreaInsets
    let bounds = superview.bounds
    let left = max(insets.left, Self.margin)
    let top = max(insets.top, Self.margin)
    let right = bounds.width - max(insets.right, Self.margin) - Self.size
    let bottom = bounds.height - max(insets.bottom, Self.margin) - Self.size
    return CGRect(x: left, y: top, width: max(0, right - left), height: max(0, bottom - top))
  }

  /// Call after the superview lays out; puts the button where it was left when the size or safe area changed.
  func superviewDidLayout() {
    guard let superview, !isDragging else { return }
    let layout = (superview.bounds.size, superview.safeAreaInsets)
    if let placedIn, placedIn.0 == layout.0, placedIn.1 == layout.1 {
      return
    }
    guard let area else { return }
    placedIn = layout
    let defaults = UserDefaults.standard
    let onLeft = defaults.string(forKey: Self.sideKey) == "left"
    let height = defaults.object(forKey: Self.heightKey) as? Double ?? Self.defaultHeight
    layer.removeAllAnimations()
    center = CGPoint(
      x: (onLeft ? area.minX : area.maxX) + Self.size / 2,
      y: area.minY + area.height * CGFloat(height) + Self.size / 2
    )
  }

  override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) {
    super.touchesBegan(touches, with: event)
    idleTimer?.invalidate()
    alpha = 1
    press(true)
  }

  override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent?) {
    super.touchesEnded(touches, with: event)
    press(false)
    wake()
  }

  /// Also how a recognized tap or drag ends for the view itself.
  override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent?) {
    super.touchesCancelled(touches, with: event)
    press(false)
    wake()
  }

  @objc private func tap() {
    onOpen()
  }

  @objc private func pan(_ gesture: UIPanGestureRecognizer) {
    guard let superview else { return }
    switch gesture.state {
    case .began:
      isDragging = true
      dragStart = center
      idleTimer?.invalidate()
      alpha = 1
    case .changed:
      let translation = gesture.translation(in: superview)
      center = CGPoint(
        x: min(max(dragStart.x + translation.x, 0), superview.bounds.width),
        y: min(max(dragStart.y + translation.y, 0), superview.bounds.height)
      )
    case .ended, .cancelled, .failed:
      isDragging = false
      settle(velocity: gesture.velocity(in: superview))
    default:
      break
    }
  }

  /// Rest at the side the fling heads to and remember the spot.
  private func settle(velocity: CGPoint) {
    guard let area, let superview else { return }
    let onLeft = center.x + velocity.x * Self.momentum < superview.bounds.midX
    let originY = min(max(center.y - Self.size / 2 + velocity.y * Self.momentum, area.minY), area.maxY)
    let target = CGPoint(
      x: (onLeft ? area.minX : area.maxX) + Self.size / 2,
      y: originY + Self.size / 2
    )
    UIView.animate(
      withDuration: 0.5,
      delay: 0,
      usingSpringWithDamping: 0.7,
      initialSpringVelocity: 0,
      options: [.allowUserInteraction, .beginFromCurrentState]
    ) {
      self.center = target
    }
    let defaults = UserDefaults.standard
    defaults.set(onLeft ? "left" : "right", forKey: Self.sideKey)
    defaults.set(area.height > 0 ? Double((originY - area.minY) / area.height) : 0, forKey: Self.heightKey)
    wake()
  }

  private func press(_ pressed: Bool) {
    UIView.animate(withDuration: 0.1, delay: 0, options: [.allowUserInteraction, .beginFromCurrentState]) {
      self.transform = pressed ? CGAffineTransform(scaleX: 0.9, y: 0.9) : .identity
    }
  }

  private func wake() {
    idleTimer?.invalidate()
    UIView.animate(withDuration: 0.3, delay: 0, options: [.allowUserInteraction, .beginFromCurrentState]) {
      self.alpha = 1
    }
    idleTimer = Timer.scheduledTimer(withTimeInterval: Self.idleDelay, repeats: false) { [weak self] _ in
      UIView.animate(withDuration: 0.3, delay: 0, options: [.allowUserInteraction]) {
        self?.alpha = Self.idleAlpha
      }
    }
  }
}

/// OpenCuria's mark (brand/opencuria-mark.svg) without its tile, framed like the Android menu button.
final class OpenCuriaMarkView: UIView {
  private typealias Face = (points: [CGPoint], color: UInt32)

  private static let center = CGPoint(x: 32, y: 32)
  private static let outline = [
    CGPoint(x: 18, y: 23), CGPoint(x: 32, y: 15), CGPoint(x: 46, y: 23),
    CGPoint(x: 46, y: 41), CGPoint(x: 32, y: 49), CGPoint(x: 18, y: 41)
  ]
  private static let faces: [Face] = [
    ([outline[0], outline[1], center], 0xEFEFEF),
    ([outline[1], outline[2], center], 0xBCBCBC),
    ([outline[2], center, outline[3]], 0x534AB7),
    ([center, outline[4], outline[3]], 0x433A93),
    ([outline[0], center, outline[5]], 0x1C1C1C),
    ([center, outline[4], outline[5]], 0x111111)
  ]
  private static let edge: UInt32 = 0x0A0A0A

  override init(frame: CGRect) {
    super.init(frame: frame)
    isOpaque = false
    backgroundColor = .clear
    contentMode = .redraw
  }

  @available(*, unavailable)
  required init?(coder: NSCoder) {
    fatalError("init(coder:) is not supported")
  }

  override func draw(_ rect: CGRect) {
    guard let context = UIGraphicsGetCurrentContext() else { return }
    // The mark's 64-unit artboard; the 52 units around its middle fill the view.
    let scale = min(bounds.width, bounds.height) / 52
    context.translateBy(x: bounds.midX, y: bounds.midY)
    context.scaleBy(x: scale, y: scale)
    context.translateBy(x: -Self.center.x, y: -Self.center.y)
    for face in Self.faces {
      context.setFillColor(Self.color(face.color))
      context.addLines(between: face.points)
      context.closePath()
      context.fillPath()
    }
    context.setStrokeColor(Self.color(Self.edge))
    context.setLineWidth(1.2)
    context.addLines(between: Self.outline)
    context.closePath()
    context.strokePath()
    context.setLineWidth(1)
    for corner in Self.outline {
      context.move(to: Self.center)
      context.addLine(to: corner)
    }
    context.strokePath()
  }

  private static func color(_ rgb: UInt32) -> CGColor {
    UIColor(
      red: CGFloat((rgb >> 16) & 0xFF) / 255,
      green: CGFloat((rgb >> 8) & 0xFF) / 255,
      blue: CGFloat(rgb & 0xFF) / 255,
      alpha: 1
    ).cgColor
  }
}
