package expo.modules.opencuriaappruntime

import expo.modules.kotlin.Promise
import expo.modules.kotlin.exception.CodedException
import expo.modules.kotlin.exception.Exceptions
import expo.modules.kotlin.functions.Queues
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import java.lang.ref.WeakReference

private class InvalidLaunchException(cause: Throwable) :
  CodedException("ERR_APP_LAUNCH", cause.message ?: "The app cannot be opened.", cause)

private class InvalidWidgetContentException(cause: Throwable) :
  CodedException("ERR_APPS_WIDGET", cause.message ?: "Invalid widget content.", cause)

private class NoOpenAppException :
  CodedException("ERR_NO_OPEN_APP", "No app is open.", null)

private class CaptureException(cause: Throwable) :
  CodedException("ERR_APP_CAPTURE", cause.message ?: "The app could not be captured.", cause)

/**
 * Opens installed apps, removes their native-side data on uninstall and
 * serves the app companion drawn over an open app: its touch layer, the app
 * menu's actions, and what recordings observe of the app ([AppRecorder]).
 */
class OpenCuriaAppRuntimeModule : Module(), AppRecorder.Listener, AppCompanionHost.Events {
  override fun definition() = ModuleDefinition {
    Name("OpenCuriaAppRuntime")

    Events("onCompanionOpen", "onAppTouch", "onAppEvent")

    OnCreate {
      AppRecorder.listener = this@OpenCuriaAppRuntimeModule
      AppCompanionHost.events = WeakReference(this@OpenCuriaAppRuntimeModule)
    }

    OnDestroy {
      if (AppRecorder.listener === this@OpenCuriaAppRuntimeModule) AppRecorder.listener = null
      if (AppCompanionHost.events?.get() === this@OpenCuriaAppRuntimeModule) AppCompanionHost.events = null
    }

    // On the main queue, so a failing start rejects instead of crashing OpenCuria.
    AsyncFunction("open") { request: String ->
      val activity = appContext.currentActivity ?: throw Exceptions.MissingActivity()
      val launch = try {
        AppLaunch.parse(activity, request)
      } catch (e: Exception) {
        throw InvalidLaunchException(e)
      }
      AppRuntimeActivity.open(activity, launch)
    }.runOnQueue(Queues.MAIN)

    AsyncFunction("close") {
      AppRuntimeActivity.close()
    }.runOnQueue(Queues.MAIN)

    AsyncFunction("clearData") { appId: String ->
      val context = appContext.reactContext ?: throw Exceptions.ReactContextLost()
      AppSandbox.clear(context, appId)
    }

    // The Apps home screen widget's content: JSON `AppsWidgetSnapshot`.
    AsyncFunction("updateAppsWidget") { snapshot: String ->
      val context = appContext.reactContext ?: throw Exceptions.ReactContextLost()
      val content = try {
        AppsWidgetContent.save(context, snapshot)
      } catch (e: Exception) {
        throw InvalidWidgetContentException(e)
      }
      AppsWidgetProvider.updateAll(context, content)
    }

    // --- App companion ---

    Function("companionReady") {}

    // `regions`: `[x, y, width, height, …]` in layout points.
    AsyncFunction("setCompanionTouchMode") { mode: String, regions: List<Double> ->
      val touchMode = when (mode) {
        "all" -> AppCompanionLayer.TouchMode.ALL
        "regions" -> AppCompanionLayer.TouchMode.REGIONS
        else -> AppCompanionLayer.TouchMode.NONE
      }
      AppRuntimeActivity.open()?.setCompanionTouchMode(touchMode, regions)
    }.runOnQueue(Queues.MAIN)

    AsyncFunction("reloadOpenApp") {
      AppRuntimeActivity.open()?.reloadApp()
    }.runOnQueue(Queues.MAIN)

    Function("menuTriggers") {
      triggersMap()
    }

    Function("setMenuTrigger") { trigger: String, on: Boolean ->
      val context = appContext.reactContext ?: throw Exceptions.ReactContextLost()
      val which = when (trigger) {
        "shake" -> AppMenuTrigger.SHAKE
        "threeFingerPress" -> AppMenuTrigger.THREE_FINGER_PRESS
        else -> AppMenuTrigger.BUTTON
      }
      AppMenuTriggers(context).set(which, on)
      appContext.currentActivity?.runOnUiThread { AppRuntimeActivity.open()?.applyTriggers() }
      triggersMap()
    }

    AsyncFunction("startAppObservation") { recordingId: String ->
      val activity = AppRuntimeActivity.open() ?: throw NoOpenAppException()
      activity.startObservation(recordingId)
    }.runOnQueue(Queues.MAIN)

    AsyncFunction("captureAppFrame") { _: String, index: Int, previousFrameId: String?, promise: Promise ->
      val activity = AppRuntimeActivity.open() ?: throw NoOpenAppException()
      activity.captureFrame(
        index,
        previousFrameId,
        { frame -> promise.resolve(frame) },
        { error -> promise.reject(CaptureException(error)) }
      )
    }.runOnQueue(Queues.MAIN)

    AsyncFunction("stopAppObservation") {
      AppRuntimeActivity.open()?.stopObservation() ?: AppRecorder.stop()
    }.runOnQueue(Queues.MAIN)

    AsyncFunction("recentAppEvents") { since: Double ->
      AppRecorder.recentEvents(since)
    }
  }

  private fun triggersMap(): Map<String, Boolean> {
    val context = appContext.reactContext ?: return mapOf()
    val triggers = AppMenuTriggers(context)
    return mapOf(
      "shake" to triggers.isEnabled(AppMenuTrigger.SHAKE),
      "threeFingerPress" to triggers.isEnabled(AppMenuTrigger.THREE_FINGER_PRESS),
      "button" to triggers.isEnabled(AppMenuTrigger.BUTTON)
    )
  }

  override fun onAppTouch(event: Map<String, Any?>) {
    sendEvent("onAppTouch", event)
  }

  override fun onAppEvent(event: Map<String, Any?>) {
    sendEvent("onAppEvent", event)
  }

  override fun onCompanionOpen() {
    sendEvent("onCompanionOpen", mapOf<String, Any?>())
  }
}
