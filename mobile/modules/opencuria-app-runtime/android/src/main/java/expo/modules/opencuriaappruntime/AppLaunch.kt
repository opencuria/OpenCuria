package expo.modules.opencuriaappruntime

import android.content.pm.ActivityInfo
import android.graphics.Color
import org.json.JSONObject
import java.io.File

/**
 * Everything needed to run one installed app version, as sent by JavaScript.
 *
 * Paths are absolute file system paths inside the OpenCuria app's storage.
 */
class AppLaunch(
  /** The original request, handed to the runtime activity unchanged. */
  val json: String,
  val appId: String,
  val versionId: String,
  val name: String,
  /** Hermes bytecode or JavaScript bundle produced by `expo export`. */
  val bundlePath: String,
  /** Installed version directory; the app may read it but never write. */
  val installDir: String,
  /** The app's persistent data directory (kept across versions). */
  val dataDir: String,
  /** Updates-style manifest whose `extra.expoClient` is the app's Expo config. */
  val manifest: JSONObject,
  /** Metro asset hash -> `file://` URI of the downloaded asset. */
  val localAssets: Map<String, String>,
  val orientation: String,
  val backgroundColor: Int?,
  /** App icon shown while the app starts, if it has one. */
  val iconPath: String?
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
    private val ID = Regex("^[A-Za-z0-9-]{1,64}$")

    /** Parse and validate the JSON launch request. */
    fun parse(json: String): AppLaunch {
      val root = JSONObject(json)
      val appId = root.getString("appId")
      val versionId = root.getString("versionId")
      require(ID.matches(appId) && ID.matches(versionId)) { "Invalid app id" }
      val bundle = File(root.getString("bundlePath"))
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
        installDir = File(root.getString("installDir")).absolutePath,
        dataDir = File(root.getString("dataDir")).absolutePath,
        manifest = root.getJSONObject("manifest"),
        localAssets = localAssets,
        orientation = root.optString("orientation", "default"),
        backgroundColor = color,
        iconPath = root.optString("iconPath").takeIf { it.isNotBlank() && File(it).isFile }
      )
    }
  }
}
