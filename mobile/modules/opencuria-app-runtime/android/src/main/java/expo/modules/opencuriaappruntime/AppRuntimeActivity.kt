package expo.modules.opencuriaappruntime

import android.app.Activity
import android.content.Intent
import android.content.res.Configuration
import android.graphics.BitmapFactory
import android.graphics.Color
import android.hardware.SensorManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.Gravity
import android.view.HapticFeedbackConstants
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.view.ViewTreeObserver
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.app.AppCompatDialog
import androidx.core.view.WindowCompat
import com.facebook.react.bridge.JSBundleLoader
import com.facebook.react.common.ShakeDetector
import com.facebook.react.common.annotations.UnstableReactNativeAPI
import com.facebook.react.defaults.DefaultComponentsRegistry
import com.facebook.react.defaults.DefaultReactHostDelegate
import com.facebook.react.defaults.DefaultTurboModuleManagerDelegate
import com.facebook.react.fabric.ComponentFactory
import com.facebook.react.interfaces.fabric.ReactSurface
import com.facebook.react.modules.core.DefaultHardwareBackBtnHandler
import com.facebook.react.modules.core.PermissionAwareActivity
import com.facebook.react.modules.core.PermissionListener
import com.facebook.react.runtime.ReactHostImpl
import com.facebook.react.runtime.hermes.HermesInstance
import java.io.File
import java.lang.ref.WeakReference

private const val TAG = "OpenCuriaAppRuntime"

/**
 * Runs one installed app in its own React instance, like an Expo Go
 * experience: its JavaScript bundle from local storage, every native module
 * from OpenCuria. The instance never touches OpenCuria's own React host and
 * ends with this activity. Back at the app's root closes it. Shaking, a
 * three-finger long press and the floating menu button open the app menu,
 * each as switched on there ([AppMenuTriggers]); a note at the bottom says
 * how when the app opens.
 */
class AppRuntimeActivity :
  AppCompatActivity(),
  DefaultHardwareBackBtnHandler,
  PermissionAwareActivity {

  private lateinit var launch: AppLaunch
  private lateinit var sandbox: AppSandbox
  private lateinit var triggers: AppMenuTriggers
  private lateinit var container: FrameLayout
  /** The app's surface and its loading screen, beneath the menu button. */
  private lateinit var content: FrameLayout
  private lateinit var menuButton: AppMenuButton
  private var host: ReactHostImpl? = null
  private var surface: ReactSurface? = null
  /** Identifies the running instance; callbacks of a stopped one are ignored. */
  private var instance = 0
  private var loading: View? = null
  private var permissionListener: PermissionListener? = null
  /** The app menu or the error of a failed app. */
  private var dialog: AppCompatDialog? = null
  private val handler = Handler(Looper.getMainLooper())
  private val openMenuByPress = Runnable {
    container.performHapticFeedback(HapticFeedbackConstants.LONG_PRESS)
    showMenu()
  }
  private val shakeDetector = ShakeDetector({ showMenu() })
  private var isShakeDetecting = false
  private var isThreeFingerPressOn = false
  private var isInForeground = false
  private var closeHintShown = false
  private val showCloseHint = Runnable {
    if (!closeHintShown && !isFinishing) {
      closeHintShown = true
      AppCloseHint.show(container, triggers.closeHint())
    }
  }

  override fun onCreate(savedInstanceState: Bundle?) {
    super.onCreate(null)
    launch = try {
      AppLaunch.parse(File(intent.getStringExtra(EXTRA_LAUNCH_FILE) ?: "").readText())
    } catch (e: Exception) {
      Log.e(TAG, "Invalid app launch", e)
      Toast.makeText(this, e.message ?: "The app cannot be opened.", Toast.LENGTH_LONG).show()
      finish()
      return
    }
    current = WeakReference(this)
    AppGateway.open(launch.gateway)
    requestedOrientation = launch.requestedOrientation
    // Apps lay out edge to edge with safe-area insets, as in Expo Go; the
    // theme keeps the system bars transparent.
    WindowCompat.setDecorFitsSystemWindows(window, false)
    sandbox = AppSandbox(applicationContext, launch)
    triggers = AppMenuTriggers(this)
    container = FrameLayout(this).apply { setBackgroundColor(backgroundColor()) }
    content = FrameLayout(this)
    container.addView(content, matchParent())
    menuButton = AppMenuButton(this, ::showMenu)
    container.addView(menuButton)
    setContentView(container)
    onBackPressedDispatcher.addCallback(
      this,
      object : OnBackPressedCallback(true) {
        override fun handleOnBackPressed() {
          if (host?.onBackPressed() != true) finish()
        }
      }
    )
    start()
    applyTriggers()
    // The note shows with the app's first screen, or now if that is slow.
    handler.postDelayed(showCloseHint, CLOSE_HINT_FALLBACK_MS)
  }

  private fun matchParent() =
    FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)

  /** Arm the ways that open the app menu, at start and whenever they change. */
  private fun applyTriggers() {
    isThreeFingerPressOn = triggers.isEnabled(AppMenuTrigger.THREE_FINGER_PRESS)
    menuButton.isOn = triggers.isEnabled(AppMenuTrigger.BUTTON)
    updateShakeDetector()
  }

  /** Shaking opens the menu while the app is in the foreground. */
  private fun updateShakeDetector() {
    val detect = isInForeground && triggers.isEnabled(AppMenuTrigger.SHAKE)
    if (detect == isShakeDetecting) return
    isShakeDetecting = detect
    if (detect) {
      shakeDetector.start(getSystemService(SensorManager::class.java))
    } else {
      shakeDetector.stop()
    }
  }

  private fun backgroundColor(): Int {
    launch.backgroundColor?.let { return it }
    val night = resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK
    return if (night == Configuration.UI_MODE_NIGHT_YES) Color.BLACK else Color.WHITE
  }

  @OptIn(UnstableReactNativeAPI::class)
  private fun start() {
    val id = ++instance
    // A stopped instance may still report errors or reloads while React
    // Native tears it down; they must not end its successor.
    fun whileCurrent(action: () -> Unit) = runOnUiThread { if (id == instance) action() }
    val delegate = DefaultReactHostDelegate(
      jsMainModulePath = "index",
      jsBundleLoader = JSBundleLoader.createFileLoader(launch.bundlePath),
      reactPackages = AppRuntimePackages.create(application, sandbox) {
        whileCurrent(::restart)
      },
      jsRuntimeFactory = HermesInstance(),
      exceptionHandler = { error -> whileCurrent { onAppFailed(error) } },
      turboModuleManagerDelegateBuilder = DefaultTurboModuleManagerDelegate.Builder()
    )
    val componentFactory = ComponentFactory()
    DefaultComponentsRegistry.register(componentFactory)
    val reactHost = ReactHostImpl(
      applicationContext,
      delegate,
      componentFactory,
      false,
      false
    )
    host = reactHost
    val reactSurface = reactHost.createSurface(this, MAIN_COMPONENT, null)
    surface = reactSurface
    reactSurface.view?.let { view ->
      content.addView(view, matchParent())
      showLoadingUntilContent(view as ViewGroup)
    }
    reactSurface.start()
    if (lifecycle.currentState.isAtLeast(androidx.lifecycle.Lifecycle.State.RESUMED)) {
      reactHost.onHostResume(this, this)
    }
  }

  /** The app's icon and a spinner until its first view mounts, as in Expo Go. */
  private fun showLoadingUntilContent(surfaceView: ViewGroup) {
    val overlay = LinearLayout(this).apply {
      orientation = LinearLayout.VERTICAL
      gravity = Gravity.CENTER
      setBackgroundColor(backgroundColor())
      isClickable = true
      contentDescription = getString(R.string.opencuria_app_loading, launch.name)
      launch.iconPath?.let(BitmapFactory::decodeFile)?.let { bitmap ->
        val size = (96 * resources.displayMetrics.density).toInt()
        addView(
          ImageView(context).apply { setImageBitmap(bitmap) },
          LinearLayout.LayoutParams(size, size).apply { bottomMargin = size / 3 }
        )
      }
      addView(ProgressBar(context))
    }
    loading?.let(content::removeView)
    loading = overlay
    content.addView(overlay, matchParent())
    surfaceView.viewTreeObserver.addOnGlobalLayoutListener(
      object : ViewTreeObserver.OnGlobalLayoutListener {
        override fun onGlobalLayout() {
          if (surfaceView.childCount == 0) return
          surfaceView.viewTreeObserver.removeOnGlobalLayoutListener(this)
          content.removeView(overlay)
          if (loading === overlay) loading = null
          handler.removeCallbacks(showCloseHint)
          showCloseHint.run()
        }
      }
    )
  }

  /**
   * End the instance. The host's own teardown stops the surface before it
   * destroys the instance; stopping the surface here first is not safe: after
   * a JavaScript error React Native is already destroying the instance, so
   * the surface would be detached but its stop dropped, and the instance
   * destroyed with a running surface (an assertion that aborts debug builds).
   */
  private fun stop() {
    instance++
    surface?.view?.let(content::removeView)
    surface = null
    loading?.let(content::removeView)
    loading = null
    host?.let { reactHost ->
      reactHost.onHostDestroy(this)
      reactHost.invalidate()
    }
    host = null
  }

  /** Start the app again from its bundle, e.g. after an error or `reloadAsync()`. */
  private fun restart() {
    if (isFinishing || isDestroyed) return
    dialog?.dismiss()
    stop()
    start()
  }

  private fun onAppFailed(error: Exception) {
    Log.e(TAG, "App ${launch.appId} failed", error)
    if (isFinishing || isDestroyed) return
    stop()
    dialog?.dismiss()
    dialog = AlertDialog.Builder(this)
      .setTitle(getString(R.string.opencuria_app_failed, launch.name))
      .setMessage(describe(error))
      .setCancelable(false)
      .setPositiveButton(R.string.opencuria_app_reload) { _, _ -> restart() }
      .setNegativeButton(R.string.opencuria_app_close) { _, _ -> finish() }
      .show()
  }

  /**
   * The JavaScript error message without React Native's prefix, stack and
   * component stack (iOS shows the same `originalMessage`).
   */
  private fun describe(error: Exception): String {
    val message = error.message?.takeIf { it.isNotBlank() } ?: return error.javaClass.simpleName
    return message
      .replace(Regex("^\\[[^\\]]*\\]:\\s*"), "")
      .substringBefore(", stack:")
      .substringBefore(COMPONENT_STACK)
      .trim()
      .take(MAX_ERROR_CHARS)
  }

  private fun showMenu() {
    if (isFinishing || isDestroyed || dialog?.isShowing == true) return
    dialog = AppMenuSheet.show(
      activity = this,
      launch = launch,
      triggers = triggers,
      onClose = ::finish,
      onReload = ::restart,
      onTriggersChanged = ::applyTriggers
    )
  }

  /** A three-finger long press opens the app menu if on; touches still reach the app. */
  override fun dispatchTouchEvent(event: MotionEvent): Boolean {
    when (event.actionMasked) {
      MotionEvent.ACTION_POINTER_DOWN ->
        if (isThreeFingerPressOn && event.pointerCount == MENU_FINGERS) {
          handler.postDelayed(openMenuByPress, MENU_PRESS_MS)
        } else {
          handler.removeCallbacks(openMenuByPress)
        }
      MotionEvent.ACTION_POINTER_UP, MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL ->
        handler.removeCallbacks(openMenuByPress)
    }
    return super.dispatchTouchEvent(event)
  }

  override fun invokeDefaultOnBackPressed() {
    finish()
  }

  override fun onResume() {
    super.onResume()
    host?.onHostResume(this, this)
    isInForeground = true
    if (::triggers.isInitialized) updateShakeDetector()
  }

  override fun onPause() {
    host?.onHostPause(this)
    isInForeground = false
    if (::triggers.isInitialized) updateShakeDetector()
    super.onPause()
  }

  override fun onDestroy() {
    handler.removeCallbacks(openMenuByPress)
    handler.removeCallbacks(showCloseHint)
    shakeDetector.stop()
    dialog?.dismiss()
    if (::launch.isInitialized) {
      stop()
      AppGateway.close(launch.gateway)
    }
    if (current?.get() === this) current = null
    super.onDestroy()
  }

  override fun onNewIntent(intent: Intent) {
    super.onNewIntent(intent)
    host?.onNewIntent(intent)
  }

  @Deprecated("Forwarded to the app's React instance")
  override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
    super.onActivityResult(requestCode, resultCode, data)
    host?.onActivityResult(this, requestCode, resultCode, data)
  }

  override fun onWindowFocusChanged(hasFocus: Boolean) {
    super.onWindowFocusChanged(hasFocus)
    host?.onWindowFocusChange(hasFocus)
  }

  override fun onConfigurationChanged(newConfig: Configuration) {
    super.onConfigurationChanged(newConfig)
    host?.onConfigurationChanged(this)
  }

  override fun requestPermissions(
    permissions: Array<String>,
    requestCode: Int,
    listener: PermissionListener?
  ) {
    permissionListener = listener
    requestPermissions(permissions, requestCode)
  }

  @Deprecated("Forwarded to React Native's permission listener")
  override fun onRequestPermissionsResult(
    requestCode: Int,
    permissions: Array<String>,
    grantResults: IntArray
  ) {
    super.onRequestPermissionsResult(requestCode, permissions, grantResults)
    if (permissionListener?.onRequestPermissionsResult(requestCode, permissions, grantResults) == true) {
      permissionListener = null
    }
  }

  companion object {
    private const val EXTRA_LAUNCH_FILE = "opencuria.app.launch-file"
    private const val MAIN_COMPONENT = "main"
    private const val MENU_FINGERS = 3
    private const val MENU_PRESS_MS = 600L
    private const val MAX_ERROR_CHARS = 600
    /** What React Native's ExceptionsManager appends to render errors. */
    private const val COMPONENT_STACK = "\n\nThis error is located at:"
    /** How long the close note waits for the app's first screen. */
    private const val CLOSE_HINT_FALLBACK_MS = 2_000L

    private var current: WeakReference<AppRuntimeActivity>? = null

    /** Whether this app is open, so showing OpenCuria's task shows it as it is. */
    fun isOpen(appId: String): Boolean {
      val activity = current?.get() ?: return false
      return !activity.isFinishing && !activity.isDestroyed && activity.launch.appId == appId
    }

    /** Close the open app, if any. */
    fun close() {
      current?.get()?.finish()
    }

    /**
     * Open an app; an app that is already open closes first. The request
     * goes by file: with many assets it exceeds what an Intent may carry,
     * and the file lets Android restore the app after process death.
     */
    fun open(from: Activity, launch: AppLaunch) {
      current?.get()?.finish()
      val file = AppSandbox.launchFile(from, launch.appId)
      file.parentFile?.mkdirs()
      file.writeText(launch.json)
      from.startActivity(
        Intent(from, AppRuntimeActivity::class.java)
          .putExtra(EXTRA_LAUNCH_FILE, file.absolutePath)
      )
    }
  }
}
