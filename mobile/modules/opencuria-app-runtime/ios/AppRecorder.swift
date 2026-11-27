// Copyright OpenCuria contributors. AGPL-3.0-only.

import UIKit

/// Receives what a recording observes; the runtime's native module.
protocol AppRecorderListener: AnyObject {
  func appRecorder(touch event: [String: Any])
  func appRecorder(event: [String: Any])
}

/**
 What the app companion records of the open app (`useAppRecording` in
 JavaScript): the app's latest log lines, errors and requests, reported by
 its inspector script (`inspector/app-inspector.js`), and, while a recording
 observes the app, its touches with the control under the finger and frames
 of its content. Coordinates are device pixels within the app's root view.
 Same behavior as Android's `AppRecorder`.
 */
final class AppRecorder {
  static let shared = AppRecorder()

  private static let maxEvents = 300
  private static let maxMessage = 1000
  /// Longest side of a frame in pixels; enough to read text, small enough to upload.
  private static let maxFrameSide: CGFloat = 1280
  private static let jpegQuality: CGFloat = 0.72
  private static let maxTexts = 40
  private static let maxText = 120
  private static let moveInterval: TimeInterval = 0.1
  private static let thumbnail = CGSize(width: 72, height: 160)
  private static let interactiveTraits: UIAccessibilityTraits = [.button, .link, .adjustable, .searchField, .keyboardKey]

  weak var listener: AppRecorderListener?
  private let lock = NSLock()
  private var events: [[String: Any]] = []
  private var observing: String?
  private var framesDirectory: URL?
  private var ignoringPress = false
  private var lastMove: TimeInterval = 0
  private let encoder = DispatchQueue(label: "opencuria.app-recorder", qos: .userInitiated)

  private static func now() -> Double {
    Date().timeIntervalSince1970 * 1000
  }

  // MARK: Events of the app

  /// An event of the app's inspector script (JSON); kept and, while observing, sent on.
  func report(_ json: String) {
    guard
      let data = json.data(using: .utf8),
      let source = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
      let kind = source["kind"] as? String, ["log", "error", "network"].contains(kind)
    else { return }
    var event: [String: Any] = [
      "t": (source["t"] as? NSNumber)?.doubleValue ?? Self.now(),
      "kind": kind
    ]
    if let level = source["level"] as? String, !level.isEmpty {
      event["level"] = level
    }
    if let message = source["message"] as? String, !message.isEmpty {
      event["message"] = String(message.prefix(Self.maxMessage))
    }
    if kind == "network" {
      event["method"] = String((source["method"] as? String ?? "GET").prefix(12))
      event["url"] = String((source["url"] as? String ?? "").prefix(500))
      if let status = source["status"] as? NSNumber {
        event["status"] = status.intValue
      }
      if let duration = source["durationMs"] as? NSNumber {
        event["durationMs"] = duration.doubleValue
      }
    }
    record(event)
  }

  /// A fatal JavaScript error the runtime caught.
  func reportFatal(_ message: String) {
    record([
      "t": Self.now(),
      "kind": "error",
      "level": "error",
      "message": "Fatal: \(message.prefix(Self.maxMessage))"
    ])
  }

  private func record(_ event: [String: Any]) {
    lock.lock()
    events.append(event)
    if events.count > Self.maxEvents {
      events.removeFirst(events.count - Self.maxEvents)
    }
    let observed = observing != nil
    lock.unlock()
    if observed {
      listener?.appRecorder(event: event)
    }
  }

  /// The kept events since `since` (epoch milliseconds).
  func recentEvents(since: Double) -> [[String: Any]] {
    lock.lock()
    defer { lock.unlock() }
    return events.filter { (($0["t"] as? Double) ?? 0) >= since }
  }

  /// A new app opened: earlier apps' events are not its own.
  func reset() {
    lock.lock()
    events.removeAll()
    observing = nil
    framesDirectory = nil
    lock.unlock()
  }

  // MARK: Observation

  /// Start observing for recording `id`; frames go to its own cache folder.
  func start(id: String) throws {
    guard AppLaunch.isValidId(id) else {
      throw AppLaunchError.invalid("Invalid recording.")
    }
    let caches = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
    let directory = caches
      .appendingPathComponent("opencuria-app-recordings", isDirectory: true)
      .appendingPathComponent(id, isDirectory: true)
      .appendingPathComponent("frames", isDirectory: true)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    lock.lock()
    observing = id
    framesDirectory = directory
    lock.unlock()
    ignoringPress = false
  }

  func stop() {
    lock.lock()
    observing = nil
    framesDirectory = nil
    lock.unlock()
  }

  private var isObserving: Bool {
    lock.lock()
    defer { lock.unlock() }
    return observing != nil
  }

  /**
   A touch on the app's screen (main thread). Touches on the companion's own
   controls (the recording bar) are not the app's.
   */
  func touch(_ touch: UITouch, phase: String, content: UIView, layer: AppCompanionLayer) {
    guard isObserving, content.bounds.width > 0 else { return }
    if phase == "down" {
      ignoringPress = layer.touchMode != .none && layer.isOwnControl(touch.location(in: layer))
    }
    if ignoringPress {
      if phase == "up" || phase == "cancel" {
        ignoringPress = false
      }
      return
    }
    let time = Date().timeIntervalSince1970
    if phase == "move" {
      guard time - lastMove >= Self.moveInterval else { return }
      lastMove = time
    }
    let scale = content.window?.screen.scale ?? UIScreen.main.scale
    let point = touch.location(in: content)
    var event: [String: Any] = [
      "t": time * 1000,
      "phase": phase,
      "x": Double(point.x * scale),
      "y": Double(point.y * scale),
      "mode": layer.touchMode == .all ? "point" : "use"
    ]
    if phase == "down", let element = describe(content: content, point: point, scale: scale) {
      event["element"] = element
    }
    listener?.appRecorder(touch: event)
  }

  /// The control under a point (content points): role, texts, testID and bounds.
  private func describe(content: UIView, point: CGPoint, scale: CGFloat) -> [String: Any]? {
    guard let hit = content.hitTest(point, with: nil) else { return nil }
    var testId: String?
    var label: String?
    var interactive: UIView?
    var view: UIView? = hit
    while let current = view {
      if testId == nil, let id = current.accessibilityIdentifier, !id.isEmpty {
        testId = id
      }
      if label == nil, !isText(current), let text = current.accessibilityLabel, !text.isEmpty {
        label = text
      }
      if interactive == nil, isInteractive(current) {
        interactive = current
      }
      if current === content { break }
      view = current.superview
    }
    let target = interactive ?? hit
    let text = String((text(of: target) ?? texts(under: target).joined(separator: " ")).prefix(Self.maxText))
    let frame = target.convert(target.bounds, to: content)
    var element: [String: Any] = [
      "type": String(describing: type(of: target)),
      "interactive": interactive != nil,
      "bounds": [
        Double(frame.minX * scale),
        Double(frame.minY * scale),
        Double(frame.width * scale),
        Double(frame.height * scale)
      ]
    ]
    if let role = role(of: target) { element["role"] = role }
    if let label, label != text { element["label"] = String(label.prefix(Self.maxText)) }
    if !text.isEmpty { element["text"] = text }
    if let testId { element["test_id"] = String(testId.prefix(Self.maxText)) }
    return element
  }

  private func isText(_ view: UIView) -> Bool {
    view is UILabel || String(describing: type(of: view)).contains("Paragraph")
  }

  private func role(of view: UIView) -> String? {
    let traits = view.accessibilityTraits
    if traits.contains(.button) { return "button" }
    if traits.contains(.link) { return "link" }
    if view is UITextField || view is UITextView { return "textfield" }
    if traits.contains(.header) { return "header" }
    if traits.contains(.image) || view is UIImageView { return "image" }
    if isText(view) || traits.contains(.staticText) { return "text" }
    return nil
  }

  /// React Native views that take taps are accessibility elements or have a tap trait.
  private func isInteractive(_ view: UIView) -> Bool {
    if !view.accessibilityTraits.intersection(Self.interactiveTraits).isEmpty { return true }
    if view is UIScrollView || isText(view) { return false }
    return view is UIControl || (view.isAccessibilityElement && text(of: view) == nil)
  }

  private func text(of view: UIView) -> String? {
    let value: String?
    if let label = view as? UILabel {
      value = label.text
    } else if let field = view as? UITextField {
      value = field.text?.isEmpty == false ? field.text : field.placeholder
    } else if let textView = view as? UITextView {
      value = textView.text
    } else if isText(view) {
      value = view.accessibilityLabel
    } else {
      value = nil
    }
    let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines)
    return trimmed?.isEmpty == false ? trimmed : nil
  }

  private func texts(under view: UIView, into found: inout [String]) {
    guard found.count < 8, !view.isHidden, view.alpha > 0 else { return }
    if let text = text(of: view) {
      found.append(text)
      return
    }
    for child in view.subviews {
      texts(under: child, into: &found)
    }
  }

  private func texts(under view: UIView) -> [String] {
    var found: [String] = []
    texts(under: view, into: &found)
    return found
  }

  /// Visible texts of the content, top to bottom, left to right.
  private func visibleTexts(_ content: UIView) -> [String] {
    var found: [(CGFloat, CGFloat, String)] = []
    let visible = content.bounds
    func walk(_ view: UIView) {
      guard found.count < Self.maxTexts * 2, !view.isHidden, view.alpha > 0.01 else { return }
      let frame = view.convert(view.bounds, to: content)
      guard frame.intersects(visible) else { return }
      if let text = text(of: view) {
        let flat = text.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        found.append((frame.minY, frame.minX, String(flat.prefix(Self.maxText))))
        return
      }
      for child in view.subviews {
        walk(child)
      }
    }
    walk(content)
    var seen = Set<String>()
    return found
      .sorted { (Int($0.0 / 8), $0.1) < (Int($1.0 / 8), $1.1) }
      .map { $0.2 }
      .filter { seen.insert($0).inserted }
      .prefix(Self.maxTexts)
      .map { $0 }
  }

  // MARK: Frames

  /**
   Capture the content (main thread) as frame `index`: unchanged when its
   pixels and texts match `previousFrameId`, else a JPEG in the recording's folder.
   */
  func capture(
    content: UIView,
    background: UIColor,
    index: Int,
    previousFrameId: String?,
    completion: @escaping (Result<[String: Any], Error>) -> Void
  ) {
    lock.lock()
    let directory = framesDirectory
    lock.unlock()
    let bounds = content.bounds
    guard let directory, bounds.width > 0, bounds.height > 0 else {
      completion(.failure(AppLaunchError.invalid("The app is not being recorded.")))
      return
    }
    let capturedAt = Self.now()
    let screenScale = content.window?.screen.scale ?? UIScreen.main.scale
    let scale = min(screenScale, Self.maxFrameSide / max(bounds.width, bounds.height))
    let format = UIGraphicsImageRendererFormat()
    format.scale = scale
    format.opaque = true
    let image = UIGraphicsImageRenderer(bounds: bounds, format: format).image { context in
      background.setFill()
      context.fill(bounds)
      content.drawHierarchy(in: bounds, afterScreenUpdates: false)
    }
    let width = Int((bounds.width * scale).rounded())
    let height = Int((bounds.height * scale).rounded())
    // Texts count too: a small label that changes barely moves the pixels.
    let texts = visibleTexts(content)
    let frameId = "\(width)x\(height)-\(fingerprint(image))-\(texts.joined(separator: "\n").hashValue)"
    if frameId == previousFrameId {
      completion(.success(["unchanged": true, "capturedAt": capturedAt]))
      return
    }
    encoder.async {
      do {
        guard let data = image.jpegData(compressionQuality: Self.jpegQuality) else {
          throw AppLaunchError.invalid("The frame could not be encoded.")
        }
        let file = directory.appendingPathComponent(String(format: "%06d.jpg", index))
        try data.write(to: file, options: .atomic)
        completion(.success([
          "uri": file.absoluteString,
          "width": width,
          "height": height,
          "frameId": frameId,
          "capturedAt": capturedAt,
          "texts": texts
        ]))
      } catch {
        completion(.failure(error))
      }
    }
  }

  /// Coarse pixels of a frame, so anti-aliasing noise counts as unchanged.
  private func fingerprint(_ image: UIImage) -> String {
    let format = UIGraphicsImageRendererFormat()
    format.scale = 1
    format.opaque = true
    let small = UIGraphicsImageRenderer(size: Self.thumbnail, format: format).image { _ in
      image.draw(in: CGRect(origin: .zero, size: Self.thumbnail))
    }
    guard
      let cgImage = small.cgImage,
      let data = cgImage.dataProvider?.data,
      let bytes = CFDataGetBytePtr(data)
    else { return "0" }
    var hash: UInt64 = 1_125_899_906_842_597
    for index in 0..<CFDataGetLength(data) {
      hash = hash &* 31 &+ UInt64(bytes[index] & 0xF8)
    }
    return String(hash, radix: 16)
  }
}
