package expo.modules.opencuriapush

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.GeneralSecurityException
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.SecureRandom
import java.security.interfaces.ECPrivateKey
import java.security.interfaces.ECPublicKey
import java.security.spec.ECGenParameterSpec
import java.security.spec.PKCS8EncodedKeySpec
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Per-account RFC 8291 keys, encrypted at rest with a non-exportable Android
 * Keystore key. Entries that cannot be unwrapped (for example after a backup
 * restore onto another phone) are replaced; the app then registers the new key.
 */
internal class PushKeyStore(context: Context) {
  class Entry(val privateKey: ECPrivateKey, val publicKey: ByteArray, val authSecret: ByteArray)

  private val preferences =
    context.applicationContext.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)

  @Synchronized
  fun getOrCreate(id: String): Entry {
    read(id)?.let { return it }
    val generator = KeyPairGenerator.getInstance("EC")
    generator.initialize(ECGenParameterSpec("secp256r1"))
    val pair = generator.generateKeyPair()
    val publicKey = WebPushDecryptor.uncompressed(pair.public as ECPublicKey)
    val auth = ByteArray(16).also { SecureRandom().nextBytes(it) }
    val encoded = byteArrayOf(VERSION) + auth + publicKey + pair.private.encoded
    preferences.edit().putString(id, wrap(encoded)).commit()
    return Entry(pair.private as ECPrivateKey, publicKey, auth)
  }

  @Synchronized
  fun delete(id: String) {
    preferences.edit().remove(id).commit()
  }

  @Synchronized
  fun all(): List<Entry> = preferences.all.keys.mapNotNull(::read)

  private fun read(id: String): Entry? {
    val bytes = preferences.getString(id, null)?.let(::unwrap) ?: return null
    if (bytes.size <= 82 || bytes[0] != VERSION) return null
    return try {
      val privateKey = KeyFactory.getInstance("EC")
        .generatePrivate(PKCS8EncodedKeySpec(bytes.copyOfRange(82, bytes.size))) as ECPrivateKey
      Entry(privateKey, bytes.copyOfRange(17, 82), bytes.copyOfRange(1, 17))
    } catch (_: GeneralSecurityException) {
      null
    }
  }

  private fun wrappingKey(): SecretKey {
    val keyStore = KeyStore.getInstance(KEYSTORE).apply { load(null) }
    (keyStore.getKey(ALIAS, null) as? SecretKey)?.let { return it }
    val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE)
    generator.init(
      KeyGenParameterSpec.Builder(
        ALIAS,
        KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
      )
        .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
        .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
        .setKeySize(256)
        .build(),
    )
    return generator.generateKey()
  }

  private fun wrap(plaintext: ByteArray): String {
    val cipher = Cipher.getInstance(TRANSFORMATION)
    cipher.init(Cipher.ENCRYPT_MODE, wrappingKey())
    return Base64.encodeToString(cipher.iv + cipher.doFinal(plaintext), Base64.NO_WRAP)
  }

  private fun unwrap(value: String): ByteArray? = try {
    val bytes = Base64.decode(value, Base64.NO_WRAP)
    val cipher = Cipher.getInstance(TRANSFORMATION)
    cipher.init(
      Cipher.DECRYPT_MODE,
      wrappingKey(),
      GCMParameterSpec(128, bytes.copyOfRange(0, IV_BYTES)),
    )
    cipher.doFinal(bytes.copyOfRange(IV_BYTES, bytes.size))
  } catch (_: GeneralSecurityException) {
    null
  } catch (_: IllegalArgumentException) {
    null
  }

  private companion object {
    const val PREFERENCES = "opencuria.push.keys"
    const val KEYSTORE = "AndroidKeyStore"
    const val ALIAS = "opencuria.push.keys"
    const val TRANSFORMATION = "AES/GCM/NoPadding"
    const val IV_BYTES = 12
    const val VERSION: Byte = 1
  }
}
