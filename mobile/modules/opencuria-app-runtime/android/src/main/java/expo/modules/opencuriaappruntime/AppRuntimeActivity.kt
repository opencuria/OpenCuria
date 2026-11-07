package expo.modules.opencuriaappruntime

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.res.Configuration
import android.graphics.BitmapFactory
import android.graphics.Color
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.Gravity
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
import androidx.core.view.WindowCompat
import com.facebook.react.bridge.JSBundleLoader
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
import java.lang.ref.WeakReference

private const val TAG = "OpenCuriaAppRuntime"

/**
 * Runs one installed app in its own React instance, like an Expo Go
 * experience: its JavaScript bundle from local storage, every native module
 * from OpenCuria. The instance never touches OpenCuria's own React host and
 * ends with this activity. Back at the app's root closes it; a three-finger
 * long press opens the app menu (reload, close).
 */
class AppRuntimeActivity :
  AppCompatActivity(),
  DefaultHardwareBackBtnHandler,
  PermissionAwareActivity {

  private lateinit var launch: AppLaunch
  private lateinit var sandbox: AppSandbox
  private lateinit var container: FrameLayout
  private var host: ReactHostImpl? = null
  private var surface: ReactSurface? = null
  private var loading: View? = null
  private var permissionListener: PermissionListener? = null
  private var dialog: AlertDialog? = null
  private val handler = Handler(Looper.getMainLooper())
  private val openMenu = Runnable { showMenu() }

  override fun onCreate(savedInstanceState: Bundle?) {
    super.onCreate(null)
    launch = try {
      AppLaunch.parse(intent.getStringExtra(EXTRA_LAUNCH) ?: "")
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
    container = FrameLayout(this).apply { setBackgroundColor(backgroundColor()) }
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
    showMenuHintOnce()
  }

  private fun backgroundColor(): Int {
    launch.backgroundColor?.let { return it }
    val night = resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK
    return if (night == Configuration.UI_MODE_NIGHT_YES) Color.BLACK else Color.WHITE
  }

  @OptIn(UnstableReactNativeAPI::class)
  private fun start() {
    val delegate = DefaultReactHostDelegate(
      jsMainModulePath = "index",
      jsBundleLoader = JSBundleLoader.createFileLoader(launch.bundlePath),
      reactPackages = AppRuntimePackages.create(application, sandbox) {
        runOnUiThread { restart() }
      },
      jsRuntimeFactory = HermesInstance(),
      exceptionHandler = { error -> runOnUiThread { onAppFailed(error) } },
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
      container.addView(
        view,
        FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
      )
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
    loading?.let(container::removeView)
    loading = overlay
    container.addView(
      overlay,
      FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
    )
    surfaceView.viewTreeObserver.addOnGlobalLayoutListener(
      object : ViewTreeObserver.OnGlobalLayoutListener {
        override fun onGlobalLayout() {
          if (surfaceView.childCount == 0) return
          surfaceView.viewTreeObserver.removeOnGlobalLayoutListener(this)
          container.removeView(overlay)
          if (loading === overlay) loading = null
        }
      }
    )
  }

  private fun stop() {
    surface?.let { reactSurface ->
      reactSurface.stop()
      reactSurface.view?.let(container::removeView)
    }
    surface = null
    loading?.let(container::removeView)
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

  /** The JavaScript error message without React Native's prefix and stack. */
  private fun describe(error: Exception): String {
    val message = error.message?.takeIf { it.isNotBlank() } ?: return error.javaClass.simpleName
    return message
      .replace(Regex("^\\[[^\\]]*\\]:\\s*"), "")
      .substringBefore(", stack:")
      .take(MAX_ERROR_CHARS)
  }

  private fun showMenu() {
    if (isFinishing || dialog?.isShowing == true) return
    dialog = AlertDialog.Builder(this)
      .setTitle(launch.name)
      .setItems(
        arrayOf(getString(R.string.opencuria_app_reload), getString(R.string.opencuria_app_close))
      ) { _, which -> if (which == 0) restart() else finish() }
      .show()
  }

  private fun showMenuHintOnce() {
    val prefs = getSharedPreferences(HINT_PREFERENCES, Context.MODE_PRIVATE)
    if (prefs.getBoolean(HINT_KEY, false)) return
    prefs.edit().putBoolean(HINT_KEY, true).apply()
    Toast.makeText(this, R.string.opencuria_app_menu_hint, Toast.LENGTH_LONG).show()
  }

  /** A three-finger long press opens the app menu; touches still reach the app. */
  override fun dispatchTouchEvent(event: MotionEvent): Boolean {
    when (event.actionMasked) {
      MotionEvent.ACTION_POINTER_DOWN ->
        if (event.pointerCount == MENU_FINGERS) handler.postDelayed(openMenu, MENU_PRESS_MS)
        else handler.removeCallbacks(openMenu)
      MotionEvent.ACTION_POINTER_UP, MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL ->
        handler.removeCallbacks(openMenu)
    }
    return super.dispatchTouchEvent(event)
  }

  override fun invokeDefaultOnBackPressed() {
    finish()
  }

  override fun onResume() {
    super.onResume()
    host?.onHostResume(this, this)
  }

  override fun onPause() {
    host?.onHostPause(this)
    super.onPause()
  }

  override fun onDestroy() {
    handler.removeCallbacks(openMenu)
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
    private const val EXTRA_LAUNCH = "opencuria.app.launch"
    private const val MAIN_COMPONENT = "main"
    private const val MENU_FINGERS = 3
    private const val MENU_PRESS_MS = 600L
    private const val MAX_ERROR_CHARS = 600
    private const val HINT_PREFERENCES = "opencuria-app-runtime"
    private const val HINT_KEY = "menu-hint-shown"

    private var current: WeakReference<AppRuntimeActivity>? = null

    /** Open an app; an app that is already open closes first. */
    fun open(from: Activity, launch: AppLaunch) {
      current?.get()?.finish()
      from.startActivity(
        Intent(from, AppRuntimeActivity::class.java).putExtra(EXTRA_LAUNCH, launch.json)
      )
    }
  }
}
