// Adapted from t3code (https://github.com/pingdotgg/t3code), MIT License,
// Copyright (c) 2026 T3 Tools Inc. See ../LICENSE and ../NOTICE.md.
import ExpoModulesCore

public final class OpenCuriaHighlightsModule: Module {
  private let lock = NSLock()

  public func definition() -> ModuleDefinition {
    Name("OpenCuriaHighlights")

    Function("tokenize") { (source: String, language: Int) -> NativeArrayBuffer? in
      var source = source
      return source.withUTF8 { utf8 in
        self.lock.withLock { Self.tokenize(utf8, language: language) }
      }
    }
  }

  private static func tokenize(_ utf8: UnsafeBufferPointer<UInt8>, language: Int) -> NativeArrayBuffer? {
    guard let languageId = UInt8(exactly: language) else { return nil }
    var records: UnsafePointer<UInt32>?
    let count = opencuria_highlights_tokenize(utf8.baseAddress, utf8.count, languageId, &records)
    guard count >= 0 else { return nil }
    let byteCount = Int(count) * MemoryLayout<UInt32>.size
    guard let records, byteCount > 0 else {
      return NativeArrayBuffer.allocate(size: 0, initializeToZero: false)
    }
    // The lexer reuses its record memory on the next call, so copy it out.
    return NativeArrayBuffer.copy(of: UnsafeRawPointer(records), count: byteCount)
  }
}
