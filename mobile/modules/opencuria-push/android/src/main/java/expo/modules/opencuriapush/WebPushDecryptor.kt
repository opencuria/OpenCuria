package expo.modules.opencuriapush

import java.math.BigInteger
import java.security.GeneralSecurityException
import java.security.KeyFactory
import java.security.interfaces.ECPrivateKey
import java.security.interfaces.ECPublicKey
import java.security.spec.ECParameterSpec
import java.security.spec.ECPoint
import java.security.spec.ECPublicKeySpec
import javax.crypto.Cipher
import javax.crypto.KeyAgreement
import javax.crypto.Mac
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/** Web Push message decryption (RFC 8291 with RFC 8188 aes128gcm). */
internal object WebPushDecryptor {
  private val P = BigInteger("FFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF", 16)
  private val B = BigInteger("5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B", 16)
  private const val HEADER = 86

  /** One record from a P-256 sender, or null for anything malformed or foreign. */
  fun decrypt(
    body: ByteArray,
    privateKey: ECPrivateKey,
    receiverKey: ByteArray,
    authSecret: ByteArray,
  ): ByteArray? {
    if (body.size < HEADER + 17 || body[20].toInt() != 65 || authSecret.size != 16) return null
    val recordSize = (0 until 4).fold(0L) { size, i -> (size shl 8) or (body[16 + i].toLong() and 0xff) }
    val salt = body.copyOfRange(0, 16)
    val senderKey = body.copyOfRange(21, HEADER)
    val ciphertext = body.copyOfRange(HEADER, body.size)
    if (recordSize < 18 || ciphertext.size > recordSize) return null
    val sender = publicKey(senderKey, privateKey.params) ?: return null
    return try {
      val agreement = KeyAgreement.getInstance("ECDH")
      agreement.init(privateKey)
      agreement.doPhase(sender, true)
      val shared = agreement.generateSecret()
      val ikm = hkdf(authSecret, shared, "WebPush: info\u0000".toByteArray() + receiverKey + senderKey, 32)
      val key = hkdf(salt, ikm, "Content-Encoding: aes128gcm\u0000".toByteArray(), 16)
      val nonce = hkdf(salt, ikm, "Content-Encoding: nonce\u0000".toByteArray(), 12)
      val cipher = Cipher.getInstance("AES/GCM/NoPadding")
      cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, nonce))
      val record = cipher.doFinal(ciphertext)
      var end = record.size - 1
      while (end >= 0 && record[end].toInt() == 0) end--
      if (end < 0 || record[end].toInt() != 2) null else record.copyOfRange(0, end)
    } catch (_: GeneralSecurityException) {
      null
    }
  }

  /** HKDF-SHA256 (RFC 5869) for outputs of at most one block. */
  fun hkdf(salt: ByteArray, ikm: ByteArray, info: ByteArray, length: Int): ByteArray {
    val mac = Mac.getInstance("HmacSHA256")
    mac.init(SecretKeySpec(salt, "HmacSHA256"))
    val prk = mac.doFinal(ikm)
    mac.init(SecretKeySpec(prk, "HmacSHA256"))
    return mac.doFinal(info + byteArrayOf(1)).copyOf(length)
  }

  /** Uncompressed point on P-256; rejecting others prevents invalid-curve attacks. */
  fun publicKey(raw: ByteArray, params: ECParameterSpec): ECPublicKey? {
    if (raw.size != 65 || raw[0].toInt() != 4) return null
    val x = BigInteger(1, raw.copyOfRange(1, 33))
    val y = BigInteger(1, raw.copyOfRange(33, 65))
    if (x >= P || y >= P) return null
    val curve = (x.pow(3) - x.multiply(BigInteger.valueOf(3)) + B).mod(P)
    if (y.pow(2).mod(P) != curve) return null
    return try {
      KeyFactory.getInstance("EC").generatePublic(ECPublicKeySpec(ECPoint(x, y), params)) as ECPublicKey
    } catch (_: GeneralSecurityException) {
      null
    }
  }

  fun uncompressed(key: ECPublicKey): ByteArray =
    byteArrayOf(4) + fixed(key.w.affineX) + fixed(key.w.affineY)

  private fun fixed(value: BigInteger): ByteArray {
    val bytes = value.toByteArray()
    return when {
      bytes.size == 32 -> bytes
      bytes.size > 32 -> bytes.copyOfRange(bytes.size - 32, bytes.size)
      else -> ByteArray(32 - bytes.size) + bytes
    }
  }
}
