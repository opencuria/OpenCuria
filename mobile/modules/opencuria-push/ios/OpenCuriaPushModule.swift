import ExpoModulesCore

/// Creates and deletes this installation's push decryption keys. JavaScript
/// only ever receives the public half, which it registers with the server.
public final class OpenCuriaPushModule: Module {
  public func definition() -> ModuleDefinition {
    Name("OpenCuriaPush")

    AsyncFunction("getKeys") { (id: String) -> [String: String] in
      let keys = try OpenCuriaPushKeys.publicKeys(for: try Self.accountId(id))
      return [
        "publicKey": Self.base64url(keys.publicKey),
        "authSecret": Self.base64url(keys.authSecret),
      ]
    }

    AsyncFunction("deleteKeys") { (id: String) in
      try OpenCuriaPushKeys.delete(id: try Self.accountId(id))
    }
  }

  private static func accountId(_ id: String) throws -> String {
    guard UUID(uuidString: id) != nil else { throw InvalidIdException() }
    return id.lowercased()
  }

  private static func base64url(_ data: Data) -> String {
    data.base64EncodedString()
      .replacingOccurrences(of: "+", with: "-")
      .replacingOccurrences(of: "/", with: "_")
      .replacingOccurrences(of: "=", with: "")
  }
}

final class InvalidIdException: Exception {
  override var reason: String { "Push keys are stored per installation ID (a UUID)." }
}
