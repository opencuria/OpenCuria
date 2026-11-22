package expo.modules.opencuriaappruntime

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import expo.modules.kotlin.exception.CodedException
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import expo.modules.kotlin.records.Field
import expo.modules.kotlin.records.Record
import org.json.JSONObject
import java.security.GeneralSecurityException
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

private class SecureStoreException(message: String, cause: Throwable? = null) :
  CodedException("ERR_SECURESTORE", message, cause)

class SandboxSecureStoreOptions : Record {
  @Field var keychainService: String = AppSecureStore.DEFAULT_SERVICE

  @Field var requireAuthentication: Boolean = false
}

/**
 * `expo-secure-store` for one installed app, as on iOS. Values are encrypted
 * with Android Keystore keys of the app's own (`opencuria-app:<appId>:<service>`)
 * and kept in a preferences file of its own. `expo-secure-store` derives key
 * aliases from the service name the caller picks, which would let an app use
 * OpenCuria's keys or another app's. Biometric protection is not available to
 * installed apps.
 */
class SandboxSecureStoreModule(private val sandbox: AppSandbox) : Module() {
  private val store by lazy { AppSecureStore(sandbox.context, sandbox.launch.appId) }

  override fun definition() = ModuleDefinition {
    Name("ExpoSecureStore")

    AsyncFunction("getValueWithKeyAsync") { key: String, options: SandboxSecureStoreOptions ->
      store.get(key, options.keychainService)
    }

    Function("getValueWithKeySync") { key: String, options: SandboxSecureStoreOptions ->
      store.get(key, options.keychainService)
    }

    AsyncFunction("setValueWithKeyAsync") { value: String?, key: String, options: SandboxSecureStoreOptions ->
      store.set(key, value, options)
    }

    Function("setValueWithKeySync") { value: String?, key: String, options: SandboxSecureStoreOptions ->
      store.set(key, value, options)
    }

    AsyncFunction("deleteValueWithKeyAsync") { key: String, options: SandboxSecureStoreOptions ->
      store.delete(key, options.keychainService)
    }

    Function("canUseBiometricAuthentication") { false }
  }
}

/** The SecureStore entries of one installed app. */
internal class AppSecureStore(context: Context, private val appId: String) {
  private val preferences = context.applicationContext
    .getSharedPreferences(preferencesName(appId), Context.MODE_PRIVATE)

  fun get(key: String, service: String): String? {
    val entry = preferences.getString(entryKey(validKey(key), service), null) ?: return null
    val secret = keyStore().getKey(alias(appId, service), null) as? SecretKey
    if (secret == null) {
      delete(key, service) // The entry outlived its key and can never be read.
      return null
    }
    return try {
      val json = JSONObject(entry)
      val cipher = Cipher.getInstance(TRANSFORMATION)
      cipher.init(Cipher.DECRYPT_MODE, secret, GCMParameterSpec(TAG_BITS, decode(json.getString("iv"))))
      cipher.updateAAD(entryKey(key, service).toByteArray())
      String(cipher.doFinal(decode(json.getString("ct"))), Charsets.UTF_8)
    } catch (e: Exception) {
      throw SecureStoreException("Could not read $key from SecureStore.", e)
    }
  }

  fun set(key: String, value: String?, options: SandboxSecureStoreOptions): Boolean {
    if (options.requireAuthentication) {
      throw SecureStoreException("requireAuthentication is not available to apps in OpenCuria.")
    }
    value ?: throw SecureStoreException("SecureStore cannot store null; delete the key instead.")
    val entryKey = entryKey(validKey(key), options.keychainService)
    try {
      val cipher = Cipher.getInstance(TRANSFORMATION)
      cipher.init(Cipher.ENCRYPT_MODE, secretKey(options.keychainService))
      cipher.updateAAD(entryKey.toByteArray())
      val encrypted = cipher.doFinal(value.toByteArray(Charsets.UTF_8))
      val entry = JSONObject().put("iv", encode(cipher.iv)).put("ct", encode(encrypted))
      if (!preferences.edit().putString(entryKey, entry.toString()).commit()) {
        throw SecureStoreException("Could not write $key to SecureStore.")
      }
    } catch (e: GeneralSecurityException) {
      throw SecureStoreException("Could not encrypt $key for SecureStore.", e)
    }
    return true
  }

  fun delete(key: String, service: String) {
    preferences.edit().remove(entryKey(validKey(key), service)).commit()
  }

  private fun secretKey(service: String): SecretKey {
    val alias = alias(appId, service)
    (keyStore().getKey(alias, null) as? SecretKey)?.let { return it }
    val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE)
    generator.init(
      KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
        .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
        .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
        .setKeySize(KEY_BITS)
        .build()
    )
    return generator.generateKey()
  }

  private fun validKey(key: String): String {
    if (!KEY.matches(key)) throw SecureStoreException("Invalid key $key")
    return key
  }

  companion object {
    const val DEFAULT_SERVICE = "app"
    private const val KEYSTORE = "AndroidKeyStore"
    private const val TRANSFORMATION = "AES/GCM/NoPadding"
    private const val KEY_BITS = 256
    private const val TAG_BITS = 128
    private val KEY = Regex("^[A-Za-z0-9._-]+$")

    private fun preferencesName(appId: String) = "opencuria-app-$appId-secure-store"

    private fun aliasPrefix(appId: String) = "opencuria-app:$appId:"

    private fun alias(appId: String, service: String) = aliasPrefix(appId) + service

    /** Unambiguous for any service name. */
    private fun entryKey(key: String, service: String) = "${service.length}:$service:$key"

    private fun keyStore() = KeyStore.getInstance(KEYSTORE).apply { load(null) }

    private fun encode(bytes: ByteArray) = Base64.encodeToString(bytes, Base64.NO_WRAP)

    private fun decode(text: String) = Base64.decode(text, Base64.NO_WRAP)

    /** Delete every entry and key of an uninstalled app. */
    fun clear(context: Context, appId: String) {
      context.deleteSharedPreferences(preferencesName(appId))
      val keyStore = keyStore()
      val prefix = aliasPrefix(appId)
      for (alias in keyStore.aliases().toList().filter { it.startsWith(prefix) }) {
        keyStore.deleteEntry(alias)
      }
    }
  }
}
