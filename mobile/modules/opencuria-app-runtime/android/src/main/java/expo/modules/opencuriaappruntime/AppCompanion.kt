package expo.modules.opencuriaappruntime

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.graphics.RectF
import android.os.Bundle
import android.view.MotionEvent
import android.view.ViewGroup
import android.widget.FrameLayout
import com.facebook.react.ReactApplication
import com.facebook.react.ReactHost
import com.facebook.react.interfaces.fabric.ReactSurface
import com.facebook.react.modules.core.DefaultHardwareBackBtnHandler
import java.lang.ref.WeakReference

/**
 * The layer above an open app that holds the app companion. Its touch mode
 * decides which touches it takes: none (hidden; the app gets everything),
 * all (the sheet, pointer mode), or only those that start inside one of its
 * regions (the recording bar); every other touch goes on to the app below.
 */
@SuppressLint("ViewConstructor")
internal class AppCompanionLayer(context: Context) : FrameLayout(context) {
  enum class TouchMode { NONE, ALL, REGIONS }

  var touchMode = TouchMode.NONE
    private set
  /** The companion's own controls, in layer pixels. */
  private var regions: List<RectF> = emptyList()
  private var capturing = false

  init {
    visibility = INVISIBLE
    // Never clip the app's surface below; the companion draws its own.
    clipChildren = false
  }

  /** [regions] are `[x, y, width, height]` groups in layout points. */
  fun setTouchMode(mode: TouchMode, regions: List<Double> = emptyList()) {
    val density = resources.displayMetrics.density
    this.regions = regions.chunked(4).filter { it.size == 4 }.map { (x, y, width, height) ->
      RectF(
        (x * density).toFloat(),
        (y * density).toFloat(),
        ((x + width) * density).toFloat(),
        ((y + height) * density).toFloat()
      )
    }
    touchMode = mode
    visibility = if (mode == TouchMode.NONE) INVISIBLE else VISIBLE
  }

  /** Whether a point (layer pixels) is on one of the companion's controls. */
  fun isOwnControl(x: Float, y: Float) = regions.any { it.contains(x, y) }

  override fun dispatchTouchEvent(event: MotionEvent): Boolean {
    if (event.actionMasked == MotionEvent.ACTION_DOWN) {
      capturing = when (touchMode) {
        TouchMode.NONE -> false
        TouchMode.ALL -> true
        TouchMode.REGIONS -> isOwnControl(event.x, event.y)
      }
    }
    // Declining the press hands it, and its whole gesture, to the app below.
    return capturing && super.dispatchTouchEvent(event)
  }
}

/**
 * The app companion over an open app: a surface of OpenCuria's own React
 * instance (component `OpenCuriaAppCompanion`, `src/apps/companion`) in an
 * [AppCompanionLayer] of the app's activity. While it exists, OpenCuria's
 * host takes the activity's lifecycle like its own, so its dialogs, pickers,
 * permissions and keyboard work over the app.
 */
internal class AppCompanionHost(
  private val activity: AppRuntimeActivity,
  private val launch: AppLaunch,
  val layer: AppCompanionLayer
) {
  private val reactHost: ReactHost? = (activity.application as? ReactApplication)?.reactHost
  private var surface: ReactSurface? = null
  private var resumed = false
  /** Back that the companion's JavaScript did not handle closes it. */
  private val back = DefaultHardwareBackBtnHandler { layer.setTouchMode(AppCompanionLayer.TouchMode.NONE) }

  /** Whether this build's OpenCuria instance can draw the companion. */
  val isAvailable: Boolean
    get() = reactHost != null

  val exists: Boolean
    get() = surface != null

  /** Show the companion: create it on first use, else ask it to open. */
  fun open(): Boolean {
    val host = reactHost ?: return false
    layer.setTouchMode(AppCompanionLayer.TouchMode.ALL)
    if (surface == null) {
      val created = host.createSurface(activity, COMPONENT, initialProps())
      created.view?.let { view ->
        layer.addView(
          view,
          FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
        )
      }
      surface = created
      if (activity.isResumedNow) resume()
      created.start()
    } else {
      events?.get()?.onCompanionOpen()
    }
    return true
  }

  private fun initialProps() = Bundle().apply {
    putString("appId", launch.appId)
    putString("appName", launch.name)
    putString("versionId", launch.versionId)
    launch.companion?.let { companion ->
      putString("scope", companion.scope)
      companion.workspaceId?.let { putString("workspaceId", it) }
      putString("versionName", companion.versionName)
    }
    launch.iconPath?.let { putString("iconUri", "file://$it") }
    putBoolean("open", true)
  }

  fun resume() {
    if (surface == null || resumed) return
    reactHost?.onHostResume(activity, back)
    resumed = true
  }

  fun pause() {
    if (!resumed) return
    resumed = false
    reactHost?.onHostPause(activity)
  }

  /** Back goes to the companion while it takes every touch. */
  fun onBackPressed(): Boolean {
    if (surface == null || layer.touchMode != AppCompanionLayer.TouchMode.ALL) return false
    return reactHost?.onBackPressed() == true
  }

  fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
    if (surface != null) reactHost?.onActivityResult(activity, requestCode, resultCode, data)
  }

  fun onConfigurationChanged() {
    if (surface != null) reactHost?.onConfigurationChanged(activity)
  }

  fun onWindowFocusChange(hasFocus: Boolean) {
    if (resumed) reactHost?.onWindowFocusChange(hasFocus)
  }

  fun destroy() {
    pause()
    surface?.let { current ->
      current.stop()
      current.view?.let(layer::removeView)
      current.detach()
    }
    surface = null
    reactHost?.onHostDestroy(activity)
    layer.setTouchMode(AppCompanionLayer.TouchMode.NONE)
  }

  /** Events the companion's JavaScript listens to (the runtime's native module). */
  interface Events {
    fun onCompanionOpen()
  }

  companion object {
    const val COMPONENT = "OpenCuriaAppCompanion"

    @Volatile var events: WeakReference<Events>? = null
  }
}
