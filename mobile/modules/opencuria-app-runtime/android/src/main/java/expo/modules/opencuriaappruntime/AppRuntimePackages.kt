package expo.modules.opencuriaappruntime

import android.app.Application
import android.util.Log
import com.facebook.react.ReactPackage
import com.facebook.react.bridge.NativeModule
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.uimanager.ViewManager
import expo.modules.adapters.react.ModuleRegistryAdapter
import expo.modules.core.ModulePriorities
import expo.modules.core.interfaces.Package
import expo.modules.interfaces.constants.ConstantsInterface
import expo.modules.kotlin.services.AppDirectoriesService
import expo.modules.kotlin.services.FilePermissionService

private const val TAG = "OpenCuriaAppRuntime"

/**
 * React packages of an installed app: the same autolinked native modules
 * OpenCuria ships (Expo Go's model), except that Expo modules get the app's
 * sandbox and `ExpoUpdates` shim, and development tooling stays out.
 */
object AppRuntimePackages {
  private const val EXPO_MODULES_PACKAGE = "expo.modules.ExpoModulesPackage"
  private val EXCLUDED_PREFIXES = listOf("expo.modules.devlauncher.", "expo.modules.devmenu.")

  fun create(application: Application, sandbox: AppSandbox, reload: () -> Unit): List<ReactPackage> {
    val autolinked = autolinkedPackages(application).filterNot { pkg ->
      val name = pkg.javaClass.name
      name == EXPO_MODULES_PACKAGE || EXCLUDED_PREFIXES.any(name::startsWith)
    }
    return autolinked + SandboxedExpoModulesPackage(sandbox, reload)
  }

  /** `com.facebook.react.PackageList` is generated into the app module. */
  @Suppress("UNCHECKED_CAST")
  private fun autolinkedPackages(application: Application): List<ReactPackage> {
    val packageList = Class.forName("com.facebook.react.PackageList")
      .getConstructor(Application::class.java)
      .newInstance(application)
    return packageList.javaClass.getMethod("getPackages").invoke(packageList) as List<ReactPackage>
  }

  @Suppress("UNCHECKED_CAST")
  internal fun expoPackages(): List<Package> = try {
    val list = Class.forName("expo.modules.ExpoModulesPackageList").getMethod("getPackageList")
    (list.invoke(null) as List<Package>).sortedByDescending {
      ModulePriorities.get(it::class.qualifiedName)
    }
  } catch (e: ReflectiveOperationException) {
    Log.e(TAG, "Expo package list unavailable", e)
    emptyList()
  }
}

/** Expo modules of one app instance with its sandboxed services. */
@Suppress("OVERRIDE_DEPRECATION") // The legacy ReactPackage API, as ExpoModulesPackage uses.
private class SandboxedExpoModulesPackage(
  private val sandbox: AppSandbox,
  private val reload: () -> Unit
) : ReactPackage {
  private val adapter = object : ModuleRegistryAdapter(AppRuntimePackages.expoPackages()) {
    override fun createNativeModules(reactContext: ReactApplicationContext): List<NativeModule> {
      val registry = mModuleRegistryProvider.get(reactContext)
      for (internalModule in mReactAdapterPackage.createInternalModules(reactContext)) {
        registry.registerInternalModule(internalModule)
      }
      return getNativeModulesFromModuleRegistry(reactContext, registry) { appContext ->
        with(appContext.services) {
          register(AppDirectoriesService::class.java, SandboxDirectoriesService(sandbox))
          register(FilePermissionService::class.java, SandboxFilePermissionService(sandbox))
          register(ConstantsInterface::class.java, SandboxConstantsService(sandbox))
        }
        appContext.registry.register(
          AppUpdatesModule(sandbox.launch, reload),
          SandboxSecureStoreModule(sandbox)
        )
      }
    }
  }

  override fun createNativeModules(reactContext: ReactApplicationContext): List<NativeModule> =
    adapter.createNativeModules(reactContext)

  override fun createViewManagers(reactContext: ReactApplicationContext): List<ViewManager<*, *>> =
    adapter.createViewManagers(reactContext)
}
