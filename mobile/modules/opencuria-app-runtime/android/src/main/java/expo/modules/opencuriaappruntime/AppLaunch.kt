package expo.modules.opencuriaappruntime

import android.content.Context
import android.content.pm.ActivityInfo
import android.graphics.Color
import org.json.JSONObject
import java.io.File

/**
 * Everything needed to run one installed app version, as sent by JavaScript.
 *
 * Where the app's files live follows from its id alone ([AppSandbox.installDir]);
 * paths in the request are ignored, so no request can widen the app's sandbox.
 */
class AppLaunch(
  /** The original request, handed to the runtime activity unchanged. */
  val json: String,
  val appId: String,
  val versionId: String,
  val name: String,
  /** Hermes bytecode or JavaScript bundle produced by `expo export`. */
  val bundlePath: String,
  /** Installed app directory; the app may read it but never write. */
  val installDir: File,
  /** The app's persistent data directory (kept across versions). */
  val dataDir: File,
  /** Updates-style manifest whose `extra.expoClient` is the app's Expo config. */
  val manifest: JSONObject,
  /** Metro asset hash -> `file://` URI of the downloaded asset. */
  val localAssets: Map<String, String>,
  val orientation: String,
  val backgroundColor: Int?,
  /** App icon shown while the app starts, if it has one. */
  val iconPath: String?,
  /** Route to the app's workspace backends; null when opened offline. */
  val gateway: AppGateway.Session?
) {
  /** The Expo config of the app (`Constants.expoConfig`). */
  val expoConfig: JSONObject
    get() = manifest.optJSONObject("extra")?.optJSONObject("expoClient") ?: JSONObject()

  /** Android orientation for the Expo config's `orientation`. */
  val requestedOrientation: Int
    get() = when (orientation) {
      "portrait" -> ActivityInfo.SCREEN_ORIENTATION_SENSOR_PORTRAIT
      "landscape" -> ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
      else -> ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
    }

  companion object {
    /** Parse and validate the JSON launch request. */
    fun parse(context: Context, json: String): AppLaunch {
      val root = JSONObject(json)
      val appId = root.getString("appId")
      val versionId = root.getString("versionId")
      require(AppSandbox.isValidId(appId) && AppSandbox.isValidId(versionId)) { "Invalid app id" }
      val installDir = AppSandbox.installDir(context, appId)
      val bundle = AppSandbox.bundleFile(installDir, versionId)
      require(bundle.isFile) { "The app bundle is missing; reinstall the app." }
      val assets = root.optJSONObject("localAssets") ?: JSONObject()
      val localAssets = assets.keys().asSequence().associateWith { assets.getString(it) }
      val color = root.optString("backgroundColor").takeIf { it.isNotBlank() }?.let {
        try {
          Color.parseColor(it)
        } catch (_: IllegalArgumentException) {
          null
        }
      }
      return AppLaunch(
        json = json,
        appId = appId,
        versionId = versionId,
        name = root.optString("name").ifBlank { "App" },
        bundlePath = bundle.absolutePath,
        installDir = installDir,
        dataDir = AppSandbox.dataDir(installDir),
        manifest = root.getJSONObject("manifest"),
        localAssets = localAssets,
        orientation = root.optString("orientation", "default"),
        backgroundColor = color,
        iconPath = AppSandbox.iconFile(installDir).takeIf(File::isFile)?.absolutePath,
        gateway = AppGateway.parse(root.optJSONObject("gateway"))
      )
    }
  }
}
