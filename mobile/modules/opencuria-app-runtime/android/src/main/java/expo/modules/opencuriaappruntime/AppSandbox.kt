package expo.modules.opencuriaappruntime

import android.content.Context
import android.content.ContextWrapper
import android.content.SharedPreferences
import expo.modules.constants.ConstantsService
import expo.modules.kotlin.services.AppDirectoriesService
import expo.modules.kotlin.services.FilePermissionService
import java.io.File
import java.util.EnumSet

/**
 * Storage of one installed app, separate from OpenCuria and from other apps,
 * as Expo Go scopes each experience: files, cache and SecureStore entries.
 */
class AppSandbox(base: Context, val launch: AppLaunch) {
  val dataDir = launch.dataDir
  val cacheDir = cacheDir(base, launch.appId)
  val files = AppFileAccess(base, dataDir, cacheDir, launch.installDir)

  val context = SandboxContext(base, dataDir, cacheDir, preferencesPrefix(launch.appId))

  init {
    dataDir.mkdirs()
    cacheDir.mkdirs()
  }

  companion object {
    private const val ROOT = "opencuria-apps"
    private const val LAUNCHES = "opencuria-app-launches"
    private val SHARED_PREFERENCES = listOf("SecureStore")
    private val ID = Regex("^[A-Za-z0-9-]{1,64}$")

    fun isValidId(value: String) = ID.matches(value)

    /**
     * Where `mobile/src/apps/appInstaller.ts` installs an app (`appLayout`):
     * `opencuria-apps/<appId>` in OpenCuria's documents (`Paths.document`).
     */
    fun installDir(context: Context, appId: String): File {
      require(isValidId(appId)) { "Invalid app id" }
      return File(File(context.filesDir, ROOT), appId)
    }

    fun dataDir(installDir: File) = File(installDir, "data")

    fun bundleFile(installDir: File, versionId: String): File {
      require(isValidId(versionId)) { "Invalid version id" }
      return File(File(File(installDir, "versions"), versionId), "index.bundle")
    }

    fun iconFile(installDir: File) = File(installDir, "icon.png")

    fun cacheDir(context: Context, appId: String) = File(File(context.cacheDir, ROOT), appId)

    fun preferencesPrefix(appId: String) = "opencuria-app-$appId-"

    /**
     * The launch request of an app on its way to the runtime activity. It
     * holds the gateway token, so it lives outside every app's sandbox.
     */
    fun launchFile(context: Context, appId: String) =
      File(File(context.noBackupFilesDir, LAUNCHES), "$appId.json")

    /** Remove cache, SecureStore entries and the launch of an uninstalled app. */
    fun clear(context: Context, appId: String) {
      require(isValidId(appId)) { "Invalid app id" }
      cacheDir(context, appId).deleteRecursively()
      launchFile(context, appId).delete()
      AppSecureStore.clear(context, appId)
      // Written by `expo-secure-store` in earlier versions of the runtime.
      for (name in SHARED_PREFERENCES) {
        context.deleteSharedPreferences(preferencesPrefix(appId) + name)
      }
    }
  }
}

/** A context whose files, cache and preferences belong to one app. */
class SandboxContext(
  base: Context,
  private val files: File,
  private val cache: File,
  private val preferencesPrefix: String
) : ContextWrapper(base) {
  override fun getFilesDir(): File = files.apply { mkdirs() }

  override fun getCacheDir(): File = cache.apply { mkdirs() }

  override fun getSharedPreferences(name: String, mode: Int): SharedPreferences =
    super.getSharedPreferences(preferencesPrefix + name, mode)
}

/** `Paths.document` and `Paths.cache` of the app point into its sandbox. */
class SandboxDirectoriesService(private val sandbox: AppSandbox) :
  AppDirectoriesService(sandbox.context) {
  override val persistentFilesDirectory: File
    get() = sandbox.dataDir.apply { mkdirs() }

  override val cacheDirectory: File
    get() = sandbox.cacheDir.apply { mkdirs() }
}

/** Expo modules ask the app's [AppFileAccess] before they touch a file. */
class SandboxFilePermissionService(private val sandbox: AppSandbox) : FilePermissionService() {
  override fun getPathPermissions(context: Context, path: String): EnumSet<Permission> =
    sandbox.files.permissions(path)
}

/** `Constants.manifest` reports the app's own Expo config. */
class SandboxConstantsService(private val sandbox: AppSandbox) : ConstantsService(sandbox.context) {
  override val constants: Map<String, Any?>
    get() = super.constants + mapOf(
      "manifest" to sandbox.launch.expoConfig.toString(),
      "appOwnership" to null
    )

  override val appScopeKey: String
    get() = "opencuria-app-${sandbox.launch.appId}"
}
