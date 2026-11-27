package expo.modules.opencuriaappruntime

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Rect
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import com.facebook.react.R as ReactR
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.util.concurrent.Executors
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

/**
 * What the app companion records of the open app (`useAppRecording` in
 * JavaScript): the app's latest log lines, errors and requests, reported by
 * its inspector script ([AppInspectorModule]), and, while a recording
 * observes the app, its touches with the control under the finger and
 * frames of its content. Coordinates are device pixels within the content.
 */
internal object AppRecorder {
  /** Receives what a recording observes; the companion's native module. */
  interface Listener {
    fun onAppTouch(event: Map<String, Any?>)
    fun onAppEvent(event: Map<String, Any?>)
  }

  private const val MAX_EVENTS = 300
  private const val MAX_MESSAGE = 1000
  /** Longest side of a frame; enough to read text, small enough to upload. */
  private const val MAX_FRAME_SIDE = 1280f
  private const val JPEG_QUALITY = 72
  private const val MAX_TEXTS = 40
  private const val MAX_TEXT = 120
  private const val MOVE_INTERVAL_MS = 100L
  private const val THUMB_WIDTH = 72
  private const val THUMB_HEIGHT = 160
  private val INTERACTIVE_ROLES = setOf(
    "button", "link", "imagebutton", "togglebutton", "checkbox", "switch", "radio",
    "tab", "menuitem", "search", "adjustable", "combobox", "spinbutton"
  )

  private val SCROLLERS = listOf("ScrollView", "RecyclerView", "ViewPager", "ListView")

  @Volatile var listener: Listener? = null
  private val events = ArrayDeque<Map<String, Any?>>()
  private val encoder = Executors.newSingleThreadExecutor()
  private var observing: String? = null
  private var framesDir: File? = null
  private var ignoringPress = false
  private var lastMove = 0L
  private val textGetters = HashMap<Class<*>, java.lang.reflect.Method?>()

  /** An event of the app's inspector script (JSON); kept and, while observing, sent on. */
  fun report(json: String) {
    val source = try {
      JSONObject(json)
    } catch (_: Exception) {
      return
    }
    val kind = source.optString("kind").takeIf { it == "log" || it == "error" || it == "network" } ?: return
    val event = buildMap<String, Any?> {
      put("t", source.optDouble("t", System.currentTimeMillis().toDouble()))
      put("kind", kind)
      source.optString("level").takeIf(String::isNotEmpty)?.let { put("level", it) }
      source.optString("message").take(MAX_MESSAGE).takeIf(String::isNotEmpty)?.let { put("message", it) }
      if (kind == "network") {
        put("method", source.optString("method").take(12))
        put("url", source.optString("url").take(500))
        if (source.has("status")) put("status", source.optInt("status"))
        if (source.has("durationMs")) put("durationMs", source.optDouble("durationMs"))
      }
    }
    record(event)
  }

  /** A fatal JavaScript error the runtime caught. */
  fun reportFatal(message: String) {
    record(
      mapOf(
        "t" to System.currentTimeMillis().toDouble(),
        "kind" to "error",
        "level" to "error",
        "message" to "Fatal: ${message.take(MAX_MESSAGE)}"
      )
    )
  }

  private fun record(event: Map<String, Any?>) {
    val observed = synchronized(events) {
      events.addLast(event)
      while (events.size > MAX_EVENTS) events.removeFirst()
      observing != null
    }
    if (observed) listener?.onAppEvent(event)
  }

  /** The kept events since [since] (epoch milliseconds). */
  fun recentEvents(since: Double): List<Map<String, Any?>> =
    synchronized(events) { events.filter { (it["t"] as? Double ?: 0.0) >= since } }

  /** A new app opened: earlier apps' events are not its own. */
  fun reset() {
    synchronized(events) {
      events.clear()
      observing = null
    }
  }

  /** Start observing for recording [id]; frames go to its own cache folder. */
  fun start(cacheDir: File, id: String) {
    require(AppSandbox.isValidId(id)) { "Invalid recording" }
    val directory = File(cacheDir, "opencuria-app-recordings/$id/frames").apply { mkdirs() }
    synchronized(events) {
      observing = id
      framesDir = directory
    }
    ignoringPress = false
  }

  fun stop() {
    synchronized(events) {
      observing = null
      framesDir = null
    }
  }

  /**
   * A touch on the activity's window, before any view handles it. Touches on
   * the companion's own controls (the recording bar) are not the app's.
   */
  fun onTouch(event: MotionEvent, content: View, layer: AppCompanionLayer?) {
    if (observing == null || content.width == 0) return
    val phase = when (event.actionMasked) {
      MotionEvent.ACTION_DOWN -> "down"
      MotionEvent.ACTION_MOVE -> "move"
      MotionEvent.ACTION_UP -> "up"
      MotionEvent.ACTION_CANCEL -> "cancel"
      else -> return
    }
    val origin = IntArray(2).also(content::getLocationOnScreen)
    val x = event.rawX - origin[0]
    val y = event.rawY - origin[1]
    if (phase == "down") {
      val inLayer = IntArray(2).also { layer?.getLocationOnScreen(it) }
      ignoringPress = layer?.isOwnControl(event.rawX - inLayer[0], event.rawY - inLayer[1]) == true
    }
    if (ignoringPress) {
      if (phase == "up" || phase == "cancel") ignoringPress = false
      return
    }
    val now = System.currentTimeMillis()
    if (phase == "move") {
      if (now - lastMove < MOVE_INTERVAL_MS) return
      lastMove = now
    }
    val pointing = layer?.touchMode == AppCompanionLayer.TouchMode.ALL
    listener?.onAppTouch(
      buildMap {
        put("t", now.toDouble())
        put("phase", phase)
        put("x", x.toDouble())
        put("y", y.toDouble())
        put("mode", if (pointing) "point" else "use")
        if (phase == "down") describe(content, event.rawX, event.rawY)?.let { put("element", it) }
      }
    )
  }

  /** The control under a point (screen pixels): role, texts, testID and bounds. */
  private fun describe(root: View, rawX: Float, rawY: Float): Map<String, Any?>? {
    val hit = deepest(root, rawX.roundToInt(), rawY.roundToInt()) ?: return null
    var testId: String? = null
    var label: String? = null
    var interactive: View? = null
    var view: View? = hit
    while (view != null) {
      if (testId == null) testId = (view.getTag(ReactR.id.react_test_id) as? String)?.takeIf(String::isNotBlank)
      if (label == null) label = view.contentDescription?.toString()?.takeIf(String::isNotBlank)
      if (interactive == null && isInteractive(view)) interactive = view
      if (view === root) break
      view = view.parent as? View
    }
    val target = interactive ?: hit
    val text = (textOf(target) ?: textsUnder(target).joinToString(" ")).take(MAX_TEXT)
    val origin = IntArray(2).also(root::getLocationOnScreen)
    val location = IntArray(2).also(target::getLocationOnScreen)
    return buildMap {
      (roleOf(target) ?: if (textOf(target) != null) "text" else null)?.let { put("role", it) }
      label?.let { put("label", it.take(MAX_TEXT)) }
      text.takeIf(String::isNotBlank)?.let { put("text", it) }
      testId?.let { put("test_id", it.take(MAX_TEXT)) }
      put("type", target.javaClass.simpleName)
      put("interactive", interactive != null)
      put(
        "bounds",
        listOf(
          (location[0] - origin[0]).toDouble(),
          (location[1] - origin[1]).toDouble(),
          target.width.toDouble(),
          target.height.toDouble()
        )
      )
    }
  }

  private fun deepest(view: View, x: Int, y: Int): View? {
    if (!view.isShown) return null
    val bounds = Rect()
    if (!view.getGlobalVisibleRect(bounds) || !bounds.contains(x, y)) return null
    if (view is ViewGroup) {
      for (index in view.childCount - 1 downTo 0) {
        deepest(view.getChildAt(index), x, y)?.let { return it }
      }
    }
    return view
  }

  private fun roleOf(view: View): String? {
    val tagged = (view.getTag(ReactR.id.accessibility_role) ?: view.getTag(ReactR.id.role))
      ?.toString()?.lowercase()?.replace("_", "")
    if (!tagged.isNullOrBlank() && tagged != "none") return tagged
    val className = view.accessibilityClassName?.toString() ?: return null
    return when {
      className.endsWith("Button") -> "button"
      className.endsWith("EditText") -> "textfield"
      className.endsWith("ImageView") -> "image"
      else -> null
    }
  }

  /**
   * React Native views that take taps are focusable (`accessible`) or have a
   * tap role. Scroll containers are focusable too, but only scroll.
   */
  private fun isInteractive(view: View): Boolean {
    if (roleOf(view) in INTERACTIVE_ROLES) return true
    val name = view.javaClass.simpleName
    if (SCROLLERS.any(name::contains)) return false
    if (view.isClickable) return true
    return view is ViewGroup && view.isFocusable && textOf(view) == null
  }

  /** Text of a text view; React Native's prepared-layout text exposes `getText()`. */
  private fun textOf(view: View): String? {
    if (view is TextView) return view.text?.toString()?.trim()?.takeIf(String::isNotEmpty)
    if (!view.javaClass.name.contains("Text")) return null
    val getter = synchronized(textGetters) {
      textGetters.getOrPut(view.javaClass) {
        try {
          view.javaClass.getMethod("getText").takeIf {
            CharSequence::class.java.isAssignableFrom(it.returnType)
          }
        } catch (_: NoSuchMethodException) {
          null
        }
      }
    }
    return try {
      (getter?.invoke(view) as? CharSequence)?.toString()?.trim()?.takeIf(String::isNotEmpty)
    } catch (_: Exception) {
      null
    }
  }

  private fun textsUnder(view: View, into: MutableList<String> = mutableListOf()): List<String> {
    if (into.size >= 8 || !view.isShown) return into
    val text = textOf(view)
    if (text != null) {
      into += text
      return into
    }
    if (view is ViewGroup) for (index in 0 until view.childCount) textsUnder(view.getChildAt(index), into)
    return into
  }

  /** Visible texts of the content, top to bottom, left to right. */
  private fun visibleTexts(root: View): List<String> {
    val found = mutableListOf<Triple<Int, Int, String>>()
    val bounds = Rect()
    fun walk(view: View) {
      if (found.size >= MAX_TEXTS * 2 || !view.isShown) return
      if (!view.getGlobalVisibleRect(bounds)) return
      val text = textOf(view)
      if (text != null) {
        found += Triple(bounds.top, bounds.left, text.replace(Regex("\\s+"), " ").take(MAX_TEXT))
        return
      }
      if (view is ViewGroup) for (index in 0 until view.childCount) walk(view.getChildAt(index))
    }
    walk(root)
    return found.sortedWith(compareBy({ it.first / 8 }, { it.second }))
      .map { it.third }
      .distinct()
      .take(MAX_TEXTS)
  }

  /**
   * Capture the content (main thread) as frame [index]: unchanged when its
   * pixels match [previousFrameId], else a JPEG in the recording's folder.
   */
  fun capture(
    content: View,
    background: Int,
    index: Int,
    previousFrameId: String?,
    resolve: (Map<String, Any?>) -> Unit,
    reject: (Throwable) -> Unit
  ) {
    val directory = synchronized(events) { framesDir }
    if (directory == null || content.width == 0 || content.height == 0) {
      reject(IllegalStateException("The app is not being recorded."))
      return
    }
    val capturedAt = System.currentTimeMillis().toDouble()
    val scale = min(1f, MAX_FRAME_SIDE / max(content.width, content.height))
    val width = max(1, (content.width * scale).roundToInt())
    val height = max(1, (content.height * scale).roundToInt())
    val bitmap = try {
      Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888).also { image ->
        val canvas = Canvas(image)
        canvas.drawColor(background)
        canvas.scale(scale, scale)
        content.draw(canvas)
      }
    } catch (error: Throwable) {
      reject(error)
      return
    }
    // Texts count too: a small label that changes barely moves the pixels.
    val texts = visibleTexts(content)
    val frameId = "${fingerprint(bitmap)}-${Integer.toHexString(texts.hashCode())}"
    if (frameId == previousFrameId) {
      bitmap.recycle()
      resolve(mapOf("unchanged" to true, "capturedAt" to capturedAt))
      return
    }
    encoder.execute {
      try {
        val file = File(directory, "%06d.jpg".format(index))
        FileOutputStream(file).use { bitmap.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, it) }
        resolve(
          mapOf(
            "uri" to "file://${file.absolutePath}",
            "width" to width,
            "height" to height,
            "frameId" to frameId,
            "capturedAt" to capturedAt,
            "texts" to texts
          )
        )
      } catch (error: Throwable) {
        reject(error)
      } finally {
        bitmap.recycle()
      }
    }
  }

  /** Coarse pixels of a frame, so anti-aliasing noise counts as unchanged. */
  private fun fingerprint(bitmap: Bitmap): String {
    val small = Bitmap.createScaledBitmap(bitmap, THUMB_WIDTH, THUMB_HEIGHT, true)
    val pixels = IntArray(THUMB_WIDTH * THUMB_HEIGHT)
    small.getPixels(pixels, 0, THUMB_WIDTH, 0, 0, THUMB_WIDTH, THUMB_HEIGHT)
    if (small !== bitmap) small.recycle()
    var hash = 1125899906842597L
    for (pixel in pixels) hash = 31 * hash + (pixel and 0xF8F8F8)
    return "${bitmap.width}x${bitmap.height}-${java.lang.Long.toHexString(hash)}"
  }

  /** Delete what recording [id] left in the cache, e.g. when it was discarded. */
  fun discard(cacheDir: File, id: String) {
    if (AppSandbox.isValidId(id)) File(cacheDir, "opencuria-app-recordings/$id").deleteRecursively()
  }
}
