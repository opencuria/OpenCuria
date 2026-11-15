package expo.modules.opencuriaappruntime

import expo.modules.kotlin.Promise
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition

/**
 * The `ExpoUpdates` module as an installed app sees it (Expo Go does the same).
 *
 * `expo-constants` reads the app's Expo config from `manifestString`, and
 * `expo-asset` resolves every bundled asset through `localAssets` to the
 * downloaded files, so images and fonts work offline. Only registered in app
 * instances, never in OpenCuria's own React instance.
 */
class AppUpdatesModule(
  private val launch: AppLaunch,
  private val reload: () -> Unit
) : Module() {
  private val launchedAt = System.currentTimeMillis()

  override fun definition() = ModuleDefinition {
    Name("ExpoUpdates")

    Constant("isEnabled") { true }
    Constant("isEmbeddedLaunch") { false }
    Constant("isEmergencyLaunch") { false }
    Constant("isUsingEmbeddedAssets") { false }
    Constant("runtimeVersion") { launch.manifest.optString("runtimeVersion") }
    Constant("checkAutomatically") { "NEVER" }
    Constant("channel") { "" }
    Constant("shouldDeferToNativeForAPIMethodAvailabilityInDevelopment") { false }
    Constant("updateId") { launch.versionId }
    Constant("commitTime") { launchedAt }
    Constant("manifestString") { launch.manifest.toString() }
    Constant("localAssets") { launch.localAssets }
    Constant("initialContext") {
      mapOf<String, Any>(
        "isUpdateAvailable" to false,
        "isUpdatePending" to false,
        "isChecking" to false,
        "isDownloading" to false,
        "isRestarting" to false,
        "restartCount" to 0,
        "sequenceNumber" to 0,
        "downloadProgress" to 0.0
      )
    }

    AsyncFunction("reload") { _: Map<String, Any?>?, promise: Promise ->
      reload()
      promise.resolve(null)
    }

    for (name in UNSUPPORTED) {
      AsyncFunction(name) { promise: Promise ->
        promise.reject(
          "ERR_NOT_AVAILABLE_IN_OPENCURIA",
          "$name() is not available in OpenCuria; the agent publishes new versions.",
          null
        )
      }
    }
  }

  private companion object {
    val UNSUPPORTED = listOf(
      "checkForUpdateAsync",
      "fetchUpdateAsync",
      "getExtraParamsAsync",
      "readLogEntriesAsync",
      "clearLogEntriesAsync"
    )
  }
}
