package expo.modules.opencuriaappruntime

import android.content.Context
import android.net.Uri
import android.os.Environment
import expo.modules.kotlin.services.FilePermissionService.Permission
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import java.io.File
import java.io.IOException
import java.util.EnumSet

/**
 * The files one installed app may use: it reads and writes its data and
 * cache, reads its installed version and uses shared storage under the
 * platform's rules. Everything else stays closed: OpenCuria's private
 * storage (also through OpenCuria's content providers), other apps, and
 * system paths such as `/proc/self`.
 *
 * Expo modules ([SandboxFilePermissionService]), React Native's networking
 * and ImageStore ([SandboxedCorePackage]) and `expo/fetch` ([fileUrlGuard])
 * all ask this one policy.
 */
class AppFileAccess(
  context: Context,
  dataDir: File,
  cacheDir: File,
  installDir: File
) {
  private val packageName = context.packageName
  private val packageManager = context.packageManager
  private val writable = listOf(dataDir, cacheDir).mapNotNull(::canonical)
  private val readable = listOfNotNull(canonical(installDir))
  private val closed = privateRoots(context).mapNotNull(::canonical)
  private val shared = listOfNotNull(
    canonical(File(SHARED_STORAGE)),
    @Suppress("DEPRECATION") canonical(Environment.getExternalStorageDirectory())
  )

  /** What the app may do with a file system path. */
  fun permissions(path: String): EnumSet<Permission> {
    val file = canonical(File(path)) ?: return EnumSet.noneOf(Permission::class.java)
    return when {
      writable.any(file::isWithin) -> EnumSet.of(Permission.READ, Permission.WRITE)
      readable.any(file::isWithin) -> EnumSet.of(Permission.READ)
      closed.any(file::isWithin) -> EnumSet.noneOf(Permission::class.java)
      shared.any(file::isWithin) -> EnumSet.noneOf(Permission::class.java).apply {
        val target = File(file)
        if (target.canRead()) add(Permission.READ)
        if (target.canWrite()) add(Permission.WRITE)
      }
      else -> EnumSet.noneOf(Permission::class.java)
    }
  }

  /**
   * Whether the app may read [uri] as React Native's `ContentResolver`
   * reads it: files by this policy, content of other apps' providers by the
   * platform's rules, but never content OpenCuria itself serves.
   */
  fun canRead(uri: Uri): Boolean = when (uri.scheme?.lowercase()) {
    null, "file" -> uri.path?.let { Permission.READ in permissions(it) } == true
    "content" -> uri.authority?.let { !isOpenCuriaProvider(it) } == true
    "http", "https", "data", "blob", "android.resource" -> true
    else -> false
  }

  /**
   * Whether OpenCuria serves [authority], also when it carries a user
   * (`0@<authority>`). Unknown providers fail to open anyway.
   */
  private fun isOpenCuriaProvider(authority: String): Boolean =
    packageManager.resolveContentProvider(authority.substringAfterLast('@'), 0)
      ?.packageName == packageName

  companion object {
    private const val SHARED_STORAGE = "/storage"

    /** `expo/fetch` loads `file://` URLs as `http://filesystem.local/…` (`OkHttpFileUrlInterceptor`). */
    private const val EXPO_FETCH_FILE_PREFIX = "http://filesystem.local"
    private const val FILE_SCHEME = "file://"
    private const val ANDROID_ASSET = "/android_asset/"
    private const val HTTP_FORBIDDEN = 403

    /** The open app's policy; OpenCuria itself never loads files through `expo/fetch`. */
    @Volatile private var current: AppFileAccess? = null

    /** Apply [access] to `expo/fetch` while its app is open. */
    fun open(access: AppFileAccess) {
      current = access
    }

    /** Stop applying [access]; an app opened after it stays. */
    fun close(access: AppFileAccess) {
      if (current === access) current = null
    }

    /**
     * Answers `expo/fetch` requests for files the open app may not read with
     * 403, as `expo/fetch` answers missing files with 404 (an early failure
     * would leave its promise pending). It runs before `expo/fetch`'s own file
     * interceptor and resolves the path exactly as that interceptor does. APK
     * assets stay readable.
     */
    val fileUrlGuard = Interceptor { chain ->
      val request = chain.request()
      val access = current
      val url = request.url.toString().replaceFirst(EXPO_FETCH_FILE_PREFIX, FILE_SCHEME)
      if (access != null && url.startsWith(FILE_SCHEME)) {
        val path = url.substring(FILE_SCHEME.length)
        if (!path.startsWith(ANDROID_ASSET) && Permission.READ !in access.permissions(path)) {
          return@Interceptor Response.Builder()
            .request(request)
            .protocol(Protocol.HTTP_1_1)
            .code(HTTP_FORBIDDEN)
            .message("Forbidden")
            .body("This app may not read $path.".toResponseBody("text/plain".toMediaType()))
            .build()
        }
      }
      chain.proceed(request)
    }

    /**
     * Every directory of OpenCuria's own: its data directories and its
     * app-specific directories on shared storage.
     */
    private fun privateRoots(context: Context): List<File> {
      val deviceProtected = context.createDeviceProtectedStorageContext()
      val external = context.getExternalFilesDirs(null).filterNotNull().mapNotNull(File::getParentFile) +
        context.externalCacheDirs.filterNotNull().mapNotNull(File::getParentFile) +
        context.obbDirs.filterNotNull() +
        @Suppress("DEPRECATION") context.externalMediaDirs.filterNotNull()
      return listOfNotNull(
        context.applicationInfo?.dataDir?.let(::File),
        context.dataDir,
        deviceProtected.dataDir
      ) + external
    }

    private fun canonical(file: File): String? = try {
      file.canonicalPath
    } catch (_: IOException) {
      null
    }
  }
}

private fun String.isWithin(root: String) = this == root || startsWith("$root/")
