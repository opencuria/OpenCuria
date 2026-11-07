package expo.modules.opencuriaappruntime

import android.content.Context
import android.content.ContextWrapper
import android.content.SharedPreferences
import expo.modules.constants.ConstantsService
import expo.modules.kotlin.services.AppDirectoriesService
import expo.modules.kotlin.services.FilePermissionService
import expo.modules.securestore.SecureStoreModule
import java.io.File
import java.io.IOException
import java.util.EnumSet

/**
 * Storage of one installed app, separate from OpenCuria and from other apps,
 * as Expo Go scopes each experience: files, cache and SecureStore entries.
 */
class AppSandbox(base: Context, val launch: AppLaunch) {
  val dataDir = File(launch.dataDir)
  val cacheDir = cacheDir(base, launch.appId)
  val context = SandboxContext(base, dataDir, cacheDir, preferencesPrefix(launch.appId))

  init {
    dataDir.mkdirs()
    cacheDir.mkdirs()
  }

  companion object {
    private const val ROOT = "opencuria-apps"
    private val SHARED_PREFERENCES = listOf("SecureStore")

    fun cacheDir(context: Context, appId: String) = File(File(context.cacheDir, ROOT), appId)

    fun preferencesPrefix(appId: String) = "opencuria-app-$appId-"

    private val APP_ID = Regex("^[A-Za-z0-9-]{1,64}$")

    /** Remove cache and SecureStore entries of an uninstalled app. */
    fun clear(context: Context, appId: String) {
      require(APP_ID.matches(appId)) { "Invalid app id" }
      cacheDir(context, appId).deleteRecursively()
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

/**
 * The app may read and write its sandbox and read its own installed files.
 * The rest of OpenCuria's private storage stays invisible; shared storage
 * outside it keeps the platform's regular rules.
 */
class SandboxFilePermissionService(private val sandbox: AppSandbox) : FilePermissionService() {
  override fun getPathPermissions(context: Context, path: String): EnumSet<Permission> {
    val canonical = try {
      File(path).canonicalPath
    } catch (_: IOException) {
      return EnumSet.noneOf(Permission::class.java)
    }
    fun within(dir: File) = try {
      val root = dir.canonicalPath
      canonical == root || canonical.startsWith("$root/")
    } catch (_: IOException) {
      false
    }
    if (within(sandbox.dataDir) || within(sandbox.cacheDir)) {
      return EnumSet.of(Permission.READ, Permission.WRITE)
    }
    if (within(File(sandbox.launch.installDir))) {
      return EnumSet.of(Permission.READ)
    }
    val privateRoots = listOfNotNull(
      context.applicationInfo?.dataDir?.let(::File),
      context.filesDir,
      context.cacheDir,
      context.noBackupFilesDir
    )
    if (privateRoots.any(::within)) {
      return EnumSet.noneOf(Permission::class.java)
    }
    return getExternalPathPermissions(path)
  }
}

/** SecureStore entries of the app live in their own preferences file. */
class SandboxSecureStoreModule(private val sandbox: AppSandbox) : SecureStoreModule() {
  override val reactContext: Context
    get() = sandbox.context
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
