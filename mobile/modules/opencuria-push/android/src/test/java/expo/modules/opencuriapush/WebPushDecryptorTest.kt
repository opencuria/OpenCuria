package expo.modules.opencuriapush

import java.math.BigInteger
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.interfaces.ECPrivateKey
import java.security.interfaces.ECPublicKey
import java.security.spec.ECGenParameterSpec
import java.security.spec.ECPrivateKeySpec
import java.util.Base64
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** RFC 8291 section 5 / appendix A: the receiver side of the example. */
class WebPushDecryptorTest {
  private fun b64(value: String) = Base64.getUrlDecoder().decode(value)

  private val params = (KeyPairGenerator.getInstance("EC")
    .apply { initialize(ECGenParameterSpec("secp256r1")) }
    .generateKeyPair().public as ECPublicKey).params

  private val receiverPrivate = KeyFactory.getInstance("EC").generatePrivate(
    ECPrivateKeySpec(BigInteger(1, b64("q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94")), params),
  ) as ECPrivateKey
  private val receiverPublic = b64(
    "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
  )
  private val auth = b64("BTBZMqHH6r4Tts7J_aSIgg")
  private val body = b64(
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIg" +
      "Dll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVB" +
      "St2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN",
  )

  @Test
  fun decryptsTheRfcExample() {
    val plaintext = WebPushDecryptor.decrypt(body, receiverPrivate, receiverPublic, auth)
    assertEquals("When I grow up, I want to be a watermelon", plaintext?.let { String(it) })
  }

  @Test
  fun rejectsTamperedOrForeignMessages() {
    val tampered = body.copyOf().also { it[it.size - 1] = (it[it.size - 1].toInt() xor 1).toByte() }
    assertNull(WebPushDecryptor.decrypt(tampered, receiverPrivate, receiverPublic, auth))
    assertNull(WebPushDecryptor.decrypt(body, receiverPrivate, receiverPublic, ByteArray(16)))
    assertNull(WebPushDecryptor.decrypt(body.copyOf(100), receiverPrivate, receiverPublic, auth))
  }

  @Test
  fun rejectsSenderKeysOffTheCurve() {
    val offCurve = body.copyOf().also { it[85] = (it[85].toInt() xor 1).toByte() }
    assertNull(WebPushDecryptor.decrypt(offCurve, receiverPrivate, receiverPublic, auth))
    assertNull(WebPushDecryptor.publicKey(receiverPublic.copyOf(64), params))
  }

  @Test
  fun hkdfMatchesTheRfcIntermediateValues() {
    val ikm = b64("S4lYMb_L0FxCeq0WhDx813KgSYqU26kOyzWUdsXYyrg")
    val salt = b64("DGv6ra1nlYgDCS1FRnbzlw")
    val key = WebPushDecryptor.hkdf(salt, ikm, "Content-Encoding: aes128gcm\u0000".toByteArray(), 16)
    assertArrayEquals(b64("oIhVW04MRdy2XN9CiKLxTg"), key)
  }

  @Test
  fun encodesUncompressedPoints() {
    val key = WebPushDecryptor.publicKey(receiverPublic, params)!!
    assertArrayEquals(receiverPublic, WebPushDecryptor.uncompressed(key))
  }
}
