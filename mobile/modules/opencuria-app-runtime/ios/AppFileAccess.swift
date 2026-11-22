// Copyright OpenCuria contributors. AGPL-3.0-only.

import ExpoModulesCore
import Foundation

/**
 The files one installed app may use: it reads and writes its data and cache
 and reads its installed version and OpenCuria's bundle. Everything else in
 OpenCuria's container stays closed, other apps' files included.

 Expo modules (`FileSystemManager.pathPermissionsOverride`, see
 `patches/expo-modules-core+*.patch`), React Native's networking
 (`OCAppFileRequestHandler`) and `expo/fetch` (`AppFileURLProtocol`) all ask
 this one policy.
 */
final class AppFileAccess: @unchecked Sendable {
  private let writable: [String]
  private let readable: [String]

  init(dataDir: URL, cacheDir: URL, installDir: URL) {
    writable = [dataDir, cacheDir].compactMap(AppFileAccess.canonicalPath)
    readable = [installDir, Bundle.main.bundleURL].compactMap(AppFileAccess.canonicalPath)
  }

  /// What the app may do with a `file:` URL.
  func permissions(for url: URL) -> EXFileSystemPermissionFlags {
    guard let path = AppFileAccess.canonicalPath(url) else {
      return []
    }
    if writable.contains(where: { path.isWithin($0) }) {
      return [.read, .write]
    }
    if readable.contains(where: { path.isWithin($0) }) {
      return .read
    }
    return []
  }

  func canRead(_ url: URL) -> Bool {
    permissions(for: url).contains(.read)
  }

  // MARK: - The open app

  private static let lock = NSLock()
  private static var openAccess: AppFileAccess?

  /// The open app's policy, for `expo/fetch`; OpenCuria itself never loads files through it.
  static var current: AppFileAccess? {
    lock.lock()
    defer { lock.unlock() }
    return openAccess
  }

  static func open(_ access: AppFileAccess) {
    lock.lock()
    defer { lock.unlock() }
    openAccess = access
  }

  /// Stop applying `access`; an app opened after it stays.
  static func close(_ access: AppFileAccess) {
    lock.lock()
    defer { lock.unlock() }
    if openAccess === access {
      openAccess = nil
    }
  }

  // MARK: - Paths

  /**
   The real path of a file URL with every symbolic link resolved, also for
   files that do not exist yet (`/var` becomes `/private/var`). Paths with
   `..` are refused rather than resolved.
   */
  static func canonicalPath(_ url: URL) -> String? {
    guard url.scheme?.lowercased() == "file", !url.pathComponents.contains("..") else {
      return nil
    }
    var existing = url.standardizedFileURL
    var missing: [String] = []
    while !FileManager.default.fileExists(atPath: existing.path) {
      let parent = existing.deletingLastPathComponent()
      if parent.path == existing.path {
        return nil
      }
      missing.insert(existing.lastPathComponent, at: 0)
      existing = parent
    }
    guard let resolved = realpath(existing.path, nil) else {
      return nil
    }
    defer { free(resolved) }
    return missing.reduce(String(cString: resolved)) { ($0 as NSString).appendingPathComponent($1) }
  }
}

private extension String {
  func isWithin(_ root: String) -> Bool {
    self == root || hasPrefix(root.hasSuffix("/") ? root : root + "/")
  }
}

/**
 Answers `expo/fetch` requests for files the open app may not read with 403,
 as `expo/fetch` answers missing files with 404; other file URLs load as usual.
 */
final class AppFileURLProtocol: URLProtocol {
  override class func canInit(with request: URLRequest) -> Bool {
    guard let url = request.url, url.scheme?.lowercased() == "file", let access = AppFileAccess.current else {
      return false
    }
    return !access.canRead(url)
  }

  override class func canonicalRequest(for request: URLRequest) -> URLRequest {
    request
  }

  override func startLoading() {
    guard
      let url = request.url,
      let response = HTTPURLResponse(
        url: url,
        statusCode: 403,
        httpVersion: "HTTP/1.1",
        headerFields: ["Content-Type": "text/plain"]
      )
    else {
      client?.urlProtocol(self, didFailWithError: URLError(.noPermissionsToReadFile))
      return
    }
    client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
    client?.urlProtocol(self, didLoad: Data("This app may not read this file.".utf8))
    client?.urlProtocolDidFinishLoading(self)
  }

  override func stopLoading() {}
}
