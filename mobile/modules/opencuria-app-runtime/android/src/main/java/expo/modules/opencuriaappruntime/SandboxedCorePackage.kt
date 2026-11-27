package expo.modules.opencuriaappruntime

import android.net.Uri
import android.util.Base64
import android.util.Base64OutputStream
import com.facebook.fbreact.specs.NativeImageStoreAndroidSpec
import com.facebook.fbreact.specs.NativeNetworkingAndroidSpec
import com.facebook.react.BaseReactPackage
import com.facebook.react.bridge.Callback
import com.facebook.react.bridge.NativeModule
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.module.model.ReactModuleInfo
import com.facebook.react.module.model.ReactModuleInfoProvider
import com.facebook.react.modules.network.NetworkingModule
import java.io.ByteArrayOutputStream
import java.util.concurrent.Executors

/**
 * React Native's core modules that open files by URI, confined to the app's
 * [AppFileAccess]. They replace the modules of the same name in
 * `MainReactPackage`, which React Native resolves first. Also the app's
 * inspector, which reports its logs and requests for the app companion.
 */
internal class SandboxedCorePackage(private val files: AppFileAccess) : BaseReactPackage() {
  override fun getModule(name: String, reactContext: ReactApplicationContext): NativeModule? =
    when (name) {
      NativeNetworkingAndroidSpec.NAME ->
        NetworkingModule(reactContext).also { NetworkingSandbox.install(it, files) }
      NativeImageStoreAndroidSpec.NAME -> SandboxImageStoreModule(reactContext, files)
      AppInspectorModule.NAME -> AppInspectorModule(reactContext)
      else -> null
    }

  override fun getReactModuleInfoProvider() = ReactModuleInfoProvider {
    listOf(
      NativeNetworkingAndroidSpec.NAME to NetworkingModule::class.java.name,
      NativeImageStoreAndroidSpec.NAME to SandboxImageStoreModule::class.java.name
    ).associate { (name, className) ->
      name to ReactModuleInfo(
        name,
        className,
        canOverrideExistingModule = true,
        needsEagerInit = false,
        isCxxModule = false,
        isTurboModule = true
      )
    } + (
      AppInspectorModule.NAME to ReactModuleInfo(
        AppInspectorModule.NAME,
        AppInspectorModule::class.java.name,
        canOverrideExistingModule = false,
        needsEagerInit = false,
        isCxxModule = false,
        isTurboModule = false
      )
    )
  }
}

/** `ImageStore.getBase64ForTag` for files and content the app may read. */
internal class SandboxImageStoreModule(
  context: ReactApplicationContext,
  private val files: AppFileAccess
) : NativeImageStoreAndroidSpec(context) {
  private val executor = Executors.newSingleThreadExecutor()

  override fun getBase64ForTag(uri: String, success: Callback, error: Callback) {
    val parsed = Uri.parse(uri)
    if (!files.canRead(parsed)) {
      error.invoke("This app may not read $uri.")
      return
    }
    executor.execute {
      try {
        val input = reactApplicationContext.contentResolver.openInputStream(parsed)
          ?: throw java.io.FileNotFoundException(uri)
        val encoded = ByteArrayOutputStream()
        input.use { source -> Base64OutputStream(encoded, Base64.NO_WRAP).use(source::copyTo) }
        success.invoke(encoded.toString())
      } catch (e: Exception) {
        error.invoke(e.message)
      }
    }
  }

  override fun invalidate() {
    executor.shutdownNow()
    super.invalidate()
  }
}
