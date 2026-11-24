package expo.modules.opencuriaappruntime

import android.content.Context
import android.util.Log
import com.facebook.react.bridge.JSBundleLoader
import com.facebook.react.bridge.JSBundleLoaderDelegate
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.bridge.ReactContextBaseJavaModule
import com.facebook.react.bridge.ReactMethod
import java.io.File

private const val TAG = "OpenCuriaAppRuntime"

/**
 * Receives what an installed app's inspector script reports (console output,
 * uncaught errors, requests) for [AppRecorder]. It only takes reports; it
 * gives the app nothing of OpenCuria.
 */
internal class AppInspectorModule(context: ReactApplicationContext) :
  ReactContextBaseJavaModule(context) {
  override fun getName() = NAME

  @ReactMethod
  fun report(json: String) {
    AppRecorder.report(json)
  }

  companion object {
    const val NAME = "OpenCuriaAppInspector"
  }
}

/**
 * Loads an app's bundle between two runs of the inspector script
 * (`inspector/app-inspector.js`, packaged as an asset): the first catches
 * console output while the bundle initializes, the second hooks requests
 * and errors, which only exist once the bundle ran.
 */
internal object AppInspector {
  private const val ASSET = "app-inspector.js"

  fun bundleLoader(context: Context, bundlePath: String): JSBundleLoader {
    val script = scriptFile(context)
    return object : JSBundleLoader() {
      override fun loadScript(delegate: JSBundleLoaderDelegate): String {
        script?.let { delegate.loadSplitBundleFromFile(it.absolutePath, "opencuria-app-inspector.js") }
        delegate.loadScriptFromFile(bundlePath, bundlePath, false)
        script?.let { delegate.loadSplitBundleFromFile(it.absolutePath, "opencuria-app-inspector.js") }
        return bundlePath
      }
    }
  }

  /** The script as a file, copied once per OpenCuria build; null if unavailable. */
  private fun scriptFile(context: Context): File? = try {
    val bytes = context.assets.open(ASSET).use { it.readBytes() }
    val file = File(File(context.noBackupFilesDir, "opencuria-app-inspector"), "${bytes.contentHashCode()}.js")
    if (!file.isFile || file.length() != bytes.size.toLong()) {
      file.parentFile?.apply {
        deleteRecursively()
        mkdirs()
      }
      file.writeBytes(bytes)
    }
    file
  } catch (error: Exception) {
    Log.w(TAG, "App inspector unavailable", error)
    null
  }
}
