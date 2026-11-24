package expo.modules.opencuriapush

import android.util.Base64
import expo.modules.kotlin.exception.CodedException
import expo.modules.kotlin.exception.Exceptions
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import java.util.UUID

/**
 * Creates and deletes this installation's push decryption keys. JavaScript
 * only ever receives the public half, which it registers with the server.
 */
class OpenCuriaPushModule : Module() {
  override fun definition() = ModuleDefinition {
    Name("OpenCuriaPush")

    AsyncFunction("getKeys") { id: String ->
      val entry = store().getOrCreate(accountId(id))
      mapOf("publicKey" to base64url(entry.publicKey), "authSecret" to base64url(entry.authSecret))
    }

    AsyncFunction("deleteKeys") { id: String ->
      store().delete(accountId(id))
    }
  }

  private fun store() = PushKeyStore(appContext.reactContext ?: throw Exceptions.ReactContextLost())

  private fun accountId(id: String): String = try {
    UUID.fromString(id).toString()
  } catch (_: IllegalArgumentException) {
    throw CodedException("ERR_INVALID_ID", "Push keys are stored per installation ID (a UUID).", null)
  }

  private fun base64url(bytes: ByteArray): String =
    Base64.encodeToString(bytes, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)
}
