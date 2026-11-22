package expo.modules.opencuriaappruntime

import android.app.Application
import android.util.Log
import com.facebook.react.ReactPackage
import com.facebook.react.bridge.NativeModule
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.uimanager.ViewManager
import expo.modules.adapters.react.ModuleRegistryAdapter
import expo.modules.adapters.react.ReactModuleRegistryProvider
import expo.modules.core.ModulePriorities
import expo.modules.core.interfaces.Package
import expo.modules.interfaces.constants.ConstantsInterface
import expo.modules.kotlin.ExpoModulesHelper
import expo.modules.kotlin.ModulesProvider
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.services.AppDirectoriesService
import expo.modules.kotlin.services.FilePermissionService

private const val TAG = "OpenCuriaAppRuntime"

/**
 * React packages of an installed app: the autolinked native modules
 * OpenCuria ships (Expo Go's model), except OpenCuria's own modules and
 * development tooling. Expo modules get the app's sandbox and `ExpoUpdates`
 * shim; React Native's file and network access its [AppFileAccess].
 */
object AppRuntimePackages {
  private const val EXPO_MODULES_PACKAGE = "expo.modules.ExpoModulesPackage"

  /**
   * Native code only OpenCuria itself may call: its local modules
   * (`mobile/modules`, all in `expo.modules.opencuria*`), the development
   * client and the home screen widgets. `mobile/scripts/sync-app-runtime.mjs`
   * keeps the same packages out of the runtime apps are built against, and
   * `src/apps/appRuntimeHostModules.test.ts` checks that their native code
   * matches these prefixes.
   */
  private val HOST_ONLY_PREFIXES = listOf(
    "expo.modules.opencuria",
    "expo.modules.devlauncher.",
    "expo.modules.devmenu.",
    "expo.modules.widgets."
  )

  internal fun isHostOnly(type: Class<*>) = HOST_ONLY_PREFIXES.any(type.name::startsWith)

  fun create(application: Application, sandbox: AppSandbox, reload: () -> Unit): List<ReactPackage> {
    val autolinked = autolinkedPackages(application).filterNot { pkg ->
      pkg.javaClass.name == EXPO_MODULES_PACKAGE || isHostOnly(pkg.javaClass)
    }
    // After `MainReactPackage`, whose networking and ImageStore it replaces.
    return autolinked + SandboxedCorePackage(sandbox.files) + SandboxedExpoModulesPackage(sandbox, reload)
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
    (list.invoke(null) as List<Package>)
      .filterNot { isHostOnly(it.javaClass) }
      .sortedByDescending { ModulePriorities.get(it::class.qualifiedName) }
  } catch (e: ReflectiveOperationException) {
    Log.e(TAG, "Expo package list unavailable", e)
    emptyList()
  }

  /** The autolinked Expo modules without OpenCuria's own. */
  internal fun expoModules(): ModulesProvider {
    val base = checkNotNull(ExpoModulesHelper.modulesProvider) { "Expo modules unavailable" }
    return object : ModulesProvider {
      override fun getModulesMap(): Map<Class<out Module>, String?> =
        base.getModulesMap().filterKeys { !isHostOnly(it) }

      override fun getServices() = base.getServices()
    }
  }
}

/** Expo modules of one app instance with its sandboxed services. */
@Suppress("OVERRIDE_DEPRECATION") // The legacy ReactPackage API, as ExpoModulesPackage uses.
private class SandboxedExpoModulesPackage(
  private val sandbox: AppSandbox,
  private val reload: () -> Unit
) : ReactPackage {
  private val adapter = object : ModuleRegistryAdapter(
    ReactModuleRegistryProvider(AppRuntimePackages.expoPackages(), null),
    AppRuntimePackages.expoModules()
  ) {
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
