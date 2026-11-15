// Copyright OpenCuria contributors. AGPL-3.0-only.

import Combine
import Foundation

/// A way to open an installed app's menu.
enum AppMenuTrigger: String, CaseIterable {
  case shake
  case threeFingerPress = "three-finger-press"
  case button

  fileprivate var key: String {
    "opencuria.app-runtime.menu-\(rawValue)"
  }

  /// This way in the close note, e.g. "shake".
  fileprivate var hint: String {
    switch self {
    case .shake: return "shake"
    case .threeFingerPress: return "long-press with three fingers"
    case .button: return "tap the menu button"
    }
  }
}

/**
 Which ways open the app menu, shared by every app like Expo Go's developer
 menu gestures. All are on at first; the last one cannot be turned off, so an
 app can always be left. Main thread only.
 */
final class AppMenuTriggers: ObservableObject {
  static let shared = AppMenuTriggers()

  /// The ways that are on; never empty.
  @Published private(set) var enabled: Set<AppMenuTrigger>

  private init() {
    let on = AppMenuTrigger.allCases.filter {
      UserDefaults.standard.object(forKey: $0.key) as? Bool ?? true
    }
    enabled = on.isEmpty ? Set(AppMenuTrigger.allCases) : Set(on)
  }

  func isEnabled(_ trigger: AppMenuTrigger) -> Bool {
    enabled.contains(trigger)
  }

  /// Whether turning `trigger` off would leave no way to open the menu.
  func isLast(_ trigger: AppMenuTrigger) -> Bool {
    enabled == [trigger]
  }

  /// Turn a way on or off; the last one stays on.
  func set(_ trigger: AppMenuTrigger, on: Bool) {
    guard on || !isLast(trigger) else { return }
    UserDefaults.standard.set(on, forKey: trigger.key)
    if on {
      enabled.insert(trigger)
    } else {
      enabled.remove(trigger)
    }
  }

  /// How to close the app with the ways that are on, e.g. "Shake or tap the menu button to close the app".
  var closeHint: String {
    let ways = AppMenuTrigger.allCases.filter(isEnabled).map(\.hint)
    let joined = ways.count == 1
      ? ways[0]
      : "\(ways.dropLast().joined(separator: ", ")) or \(ways[ways.count - 1])"
    let text = "\(joined) to close the app"
    return text.prefix(1).uppercased() + text.dropFirst()
  }
}
