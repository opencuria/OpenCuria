package expo.modules.opencuriaappruntime

import expo.modules.kotlin.exception.CodedException
import expo.modules.kotlin.exception.Exceptions
import expo.modules.kotlin.functions.Queues
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition

private class InvalidLaunchException(cause: Throwable) :
  CodedException("ERR_APP_LAUNCH", cause.message ?: "The app cannot be opened.", cause)

/** Opens installed apps and removes their native-side data on uninstall. */
class OpenCuriaAppRuntimeModule : Module() {
  override fun definition() = ModuleDefinition {
    Name("OpenCuriaAppRuntime")

    // On the main queue, so a failing start rejects instead of crashing OpenCuria.
    AsyncFunction("open") { request: String ->
      val activity = appContext.currentActivity ?: throw Exceptions.MissingActivity()
      val launch = try {
        AppLaunch.parse(request)
      } catch (e: Exception) {
        throw InvalidLaunchException(e)
      }
      AppRuntimeActivity.open(activity, launch)
    }.runOnQueue(Queues.MAIN)

    AsyncFunction("clearData") { appId: String ->
      val context = appContext.reactContext ?: throw Exceptions.ReactContextLost()
      AppSandbox.clear(context, appId)
    }
  }
}
