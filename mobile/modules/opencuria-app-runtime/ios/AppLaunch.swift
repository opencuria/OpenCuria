// Copyright OpenCuria contributors. AGPL-3.0-only.

import Foundation
import UIKit

enum AppLaunchError: LocalizedError {
  case invalid(String)

  var errorDescription: String? {
    switch self {
    case .invalid(let message): return message
    }
  }
}

/**
 Everything needed to run one installed app version, as sent by JavaScript.

 Where the app's files live follows from its id alone
 (`AppSandbox.installDirectory`); paths in the request are ignored, so no
 request can widen the app's sandbox.
 */
struct AppLaunch {
  let appId: String
  let versionId: String
  let name: String
  /// Hermes bytecode or JavaScript bundle produced by `expo export`.
  let bundleURL: URL
  /// Installed app directory; the app may read it but never write.
  let installDir: URL
  /// The app's persistent data directory (kept across versions).
  let dataDir: URL
  /// Updates-style manifest whose `extra.expoClient` is the app's Expo config.
  let manifest: [String: Any]
  let manifestString: String
  /// Metro asset hash -> `file://` URI of the downloaded asset.
  let localAssets: [String: String]
  let orientation: String
  let backgroundColor: UIColor?
  let iconPath: String?
  /// Route to the app's workspace backends; nil when opened offline.
  let gateway: AppGatewaySession?
  /// What the app companion needs: the app's account and workspace.
  let companion: CompanionInfo?

  /// The account an app was installed for and the workspace it comes from.
  struct CompanionInfo {
    let scope: String
    let workspaceId: String?
    let versionName: String
  }

  /// The app's Expo config (`Constants.expoConfig`).
  var expoConfig: [String: Any] {
    (manifest["extra"] as? [String: Any])?["expoClient"] as? [String: Any] ?? [:]
  }

  var supportedOrientations: UIInterfaceOrientationMask {
    switch orientation {
    case "portrait": return .portrait
    case "landscape": return .landscape
    default: return .all
    }
  }

  private static let idPattern = try! NSRegularExpression(pattern: "^[A-Za-z0-9-]{1,64}$")

  static func isValidId(_ value: String) -> Bool {
    idPattern.firstMatch(in: value, range: NSRange(value.startIndex..., in: value)) != nil
  }

  /// Parse and validate the JSON launch request.
  static func parse(_ json: String) throws -> AppLaunch {
    guard
      let data = json.data(using: .utf8),
      let root = try JSONSerialization.jsonObject(with: data) as? [String: Any],
      let appId = root["appId"] as? String, isValidId(appId),
      let versionId = root["versionId"] as? String, isValidId(versionId),
      let manifest = root["manifest"] as? [String: Any]
    else {
      throw AppLaunchError.invalid("The app cannot be opened; reinstall it.")
    }
    let installDir = AppSandbox.installDirectory(appId: appId)
    let bundleURL = installDir
      .appendingPathComponent("versions", isDirectory: true)
      .appendingPathComponent(versionId, isDirectory: true)
      .appendingPathComponent("index.bundle")
    guard FileManager.default.fileExists(atPath: bundleURL.path) else {
      throw AppLaunchError.invalid("The app bundle is missing; reinstall the app.")
    }
    let manifestData = try JSONSerialization.data(withJSONObject: manifest)
    let iconPath = installDir.appendingPathComponent("icon.png").path
    return AppLaunch(
      appId: appId,
      versionId: versionId,
      name: (root["name"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "App",
      bundleURL: bundleURL,
      installDir: installDir,
      dataDir: installDir.appendingPathComponent("data", isDirectory: true),
      manifest: manifest,
      manifestString: String(data: manifestData, encoding: .utf8) ?? "{}",
      localAssets: root["localAssets"] as? [String: String] ?? [:],
      orientation: root["orientation"] as? String ?? "default",
      backgroundColor: (root["backgroundColor"] as? String).flatMap(UIColor.init(hex:)),
      iconPath: FileManager.default.fileExists(atPath: iconPath) ? iconPath : nil,
      gateway: AppGateway.session(fromJSON: root["gateway"] as? [String: Any]),
      companion: (root["companion"] as? [String: Any]).map {
        CompanionInfo(
          scope: $0["scope"] as? String ?? "",
          workspaceId: ($0["workspaceId"] as? String).flatMap { $0.isEmpty ? nil : $0 },
          versionName: $0["versionName"] as? String ?? ""
        )
      }
    )
  }
}

extension UIColor {
  /// `#RRGGBB` or `#RRGGBBAA`.
  convenience init?(hex: String) {
    var value = hex.trimmingCharacters(in: .whitespaces)
    guard value.hasPrefix("#") else { return nil }
    value.removeFirst()
    guard value.count == 6 || value.count == 8, let number = UInt64(value, radix: 16) else {
      return nil
    }
    let hasAlpha = value.count == 8
    let red = CGFloat((number >> (hasAlpha ? 24 : 16)) & 0xFF) / 255
    let green = CGFloat((number >> (hasAlpha ? 16 : 8)) & 0xFF) / 255
    let blue = CGFloat((number >> (hasAlpha ? 8 : 0)) & 0xFF) / 255
    let alpha = hasAlpha ? CGFloat(number & 0xFF) / 255 : 1
    self.init(red: red, green: green, blue: blue, alpha: alpha)
  }
}
