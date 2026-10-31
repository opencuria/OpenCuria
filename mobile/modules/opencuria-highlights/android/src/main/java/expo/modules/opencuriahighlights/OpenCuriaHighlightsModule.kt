// Adapted from t3code (https://github.com/pingdotgg/t3code), MIT License,
// Copyright (c) 2026 T3 Tools Inc. See ../../../../../../../LICENSE.
package expo.modules.opencuriahighlights

import expo.modules.kotlin.jni.NativeArrayBuffer
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import java.nio.ByteBuffer
import java.nio.ByteOrder

class OpenCuriaHighlightsModule : Module() {
  override fun definition() = ModuleDefinition {
    Name("OpenCuriaHighlights")

    Function("tokenize") { source: String, language: Int ->
      HighlightsNative.tokenize(source, language)
    }
  }
}

internal object HighlightsNative {
  init {
    System.loadLibrary("opencuriahighlights")
  }

  // The records live in the lexer's memory until its next run.
  @Synchronized
  fun tokenize(source: String, language: Int): NativeArrayBuffer? {
    val count = nativeTokenize(source.toByteArray(Charsets.UTF_8), language)
    if (count < 0) return null
    val records = ByteBuffer.allocateDirect(count * Int.SIZE_BYTES).order(ByteOrder.nativeOrder())
    nativeCopyRecords(records)
    return NativeArrayBuffer(records)
  }

  @JvmStatic private external fun nativeTokenize(source: ByteArray, language: Int): Int

  @JvmStatic private external fun nativeCopyRecords(destination: ByteBuffer)
}
