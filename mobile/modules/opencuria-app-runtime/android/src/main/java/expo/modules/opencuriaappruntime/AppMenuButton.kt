package expo.modules.opencuriaappruntime

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.view.Gravity
import android.view.MotionEvent
import android.view.VelocityTracker
import android.view.View
import android.view.ViewConfiguration
import android.view.ViewGroup
import android.view.animation.OvershootInterpolator
import android.widget.FrameLayout
import android.widget.ImageView
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import kotlin.math.hypot
import kotlin.math.max

/**
 * The floating menu button over an app, like Expo Go's tools button: drag it
 * anywhere and it rests at the nearest side edge, where every app finds it
 * again; it fades while untouched. A tap opens the app menu.
 */
@SuppressLint("ViewConstructor")
class AppMenuButton(context: Context, private val onOpen: () -> Unit) : FrameLayout(context) {
  private val prefs = context.getSharedPreferences(AppMenuTriggers.PREFERENCES, Context.MODE_PRIVATE)
  private val density = resources.displayMetrics.density
  private val size = dp(SIZE_DP)
  private val margin = dp(MARGIN_DP)
  private val touchSlop = ViewConfiguration.get(context).scaledTouchSlop
  private var velocity: VelocityTracker? = null
  private var downX = 0f
  private var downY = 0f
  private var startX = 0f
  private var startY = 0f
  private var dragging = false
  private val fade = Runnable { animate().alpha(IDLE_ALPHA).setDuration(FADE_MS) }
  private val relayout =
    View.OnLayoutChangeListener { _, left, top, right, bottom, oldLeft, oldTop, oldRight, oldBottom ->
      if (right - left != oldRight - oldLeft || bottom - top != oldBottom - oldTop) post { place() }
    }

  init {
    layoutParams = LayoutParams(size, size, Gravity.TOP or Gravity.START)
    background = GradientDrawable().apply {
      shape = GradientDrawable.OVAL
      setColor(BACKGROUND)
      setStroke(dp(1), Color.argb(40, 255, 255, 255))
    }
    elevation = dp(6).toFloat()
    contentDescription = context.getString(R.string.opencuria_app_menu_button_label)
    isClickable = true
    isFocusable = true
    visibility = INVISIBLE
    addView(
      ImageView(context).apply { setImageResource(R.drawable.opencuria_app_menu_mark) },
      LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
    )
    setOnClickListener { onOpen() }
    // Bars and cutouts arrive (or change) after the button is attached.
    ViewCompat.setOnApplyWindowInsetsListener(this) { _, insets ->
      if (!dragging) post { place() }
      insets
    }
  }

  override fun onAttachedToWindow() {
    super.onAttachedToWindow()
    (parent as? View)?.addOnLayoutChangeListener(relayout)
    post { place() }
  }

  override fun onDetachedFromWindow() {
    (parent as? View)?.removeOnLayoutChangeListener(relayout)
    removeCallbacks(fade)
    super.onDetachedFromWindow()
  }

  /** Whether the menu button is switched on; it shows once it has its place. */
  var isOn = false
    set(value) {
      field = value
      updateShown()
    }

  private fun updateShown() {
    val shown = isOn && area() != null
    if (shown == (visibility == VISIBLE)) return
    if (shown) {
      visibility = VISIBLE
      alpha = 0f
      wake()
    } else {
      removeCallbacks(fade)
      visibility = INVISIBLE
    }
  }

  /** Where the button may rest: within the parent, clear of system bars and cutouts. */
  private fun area(): Area? {
    val parent = parent as? View ?: return null
    if (parent.width == 0 || parent.height == 0) return null
    val insets = ViewCompat.getRootWindowInsets(this)
      ?.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
    val left = max(insets?.left ?: 0, margin).toFloat()
    val top = max(insets?.top ?: 0, margin).toFloat()
    val right = (parent.width - max(insets?.right ?: 0, margin) - size).toFloat()
    val bottom = (parent.height - max(insets?.bottom ?: 0, margin) - size).toFloat()
    return Area(left, top, max(left, right), max(top, bottom))
  }

  /** Put the button where it was left, e.g. after opening or turning the phone. */
  private fun place() {
    val area = area() ?: return
    animate().cancel()
    scaleX = 1f
    scaleY = 1f
    translationX = if (prefs.getInt(SIDE_KEY, RIGHT) == LEFT) area.left else area.right
    translationY = area.top + (area.bottom - area.top) * prefs.getFloat(HEIGHT_KEY, DEFAULT_HEIGHT)
    updateShown()
    if (visibility == VISIBLE) wake()
  }

  private fun wake() {
    removeCallbacks(fade)
    animate().alpha(1f).setDuration(FADE_MS)
    postDelayed(fade, IDLE_MS)
  }

  @SuppressLint("ClickableViewAccessibility")
  override fun onTouchEvent(event: MotionEvent): Boolean {
    val tracker = velocity ?: VelocityTracker.obtain().also { velocity = it }
    // Raw coordinates: the button moves under the finger.
    val raw = MotionEvent.obtain(event).apply { setLocation(event.rawX, event.rawY) }
    tracker.addMovement(raw)
    raw.recycle()
    when (event.actionMasked) {
      MotionEvent.ACTION_DOWN -> {
        removeCallbacks(fade)
        animate().cancel()
        alpha = 1f
        downX = event.rawX
        downY = event.rawY
        startX = translationX
        startY = translationY
        dragging = false
        scale(PRESSED_SCALE)
        parent?.requestDisallowInterceptTouchEvent(true)
      }
      MotionEvent.ACTION_MOVE -> {
        val dx = event.rawX - downX
        val dy = event.rawY - downY
        if (!dragging && hypot(dx, dy) > touchSlop) {
          dragging = true
          scale(1f)
        }
        if (dragging) {
          val parent = parent as? View
          translationX = (startX + dx).coerceIn(-size / 2f, (parent?.width ?: 0) - size / 2f)
          translationY = (startY + dy).coerceIn(-size / 2f, (parent?.height ?: 0) - size / 2f)
        }
      }
      MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
        scale(1f)
        tracker.computeCurrentVelocity(1000)
        val vx = tracker.xVelocity
        val vy = tracker.yVelocity
        tracker.recycle()
        velocity = null
        if (dragging) {
          settle(vx, vy)
        } else if (event.actionMasked == MotionEvent.ACTION_UP) {
          performClick()
        }
        dragging = false
        wake()
      }
    }
    return true
  }

  override fun performClick(): Boolean {
    super.performClick()
    return true
  }

  /** Rest at the side the fling heads to and remember the spot. */
  private fun settle(vx: Float, vy: Float) {
    val area = area() ?: return
    val parentWidth = (parent as View).width
    val centerX = translationX + size / 2f + vx * MOMENTUM_S
    val left = centerX < parentWidth / 2f
    val x = if (left) area.left else area.right
    val y = (translationY + vy * MOMENTUM_S).coerceIn(area.top, area.bottom)
    animate()
      .translationX(x)
      .translationY(y)
      .setInterpolator(OvershootInterpolator(1f))
      .setDuration(SETTLE_MS)
    val height = if (area.bottom > area.top) (y - area.top) / (area.bottom - area.top) else 0f
    prefs.edit().putInt(SIDE_KEY, if (left) LEFT else RIGHT).putFloat(HEIGHT_KEY, height).apply()
  }

  private fun scale(value: Float) {
    animate().scaleX(value).scaleY(value).setDuration(PRESS_MS)
  }

  private fun dp(value: Int): Int = (value * density).toInt()

  private class Area(val left: Float, val top: Float, val right: Float, val bottom: Float)

  private companion object {
    const val SIZE_DP = 48
    const val MARGIN_DP = 12
    /** OpenCuria's mark background (#0A0A0A), slightly see-through. */
    val BACKGROUND = Color.argb(235, 10, 10, 10)
    const val IDLE_ALPHA = 0.5f
    const val IDLE_MS = 5_000L
    const val FADE_MS = 300L
    const val PRESS_MS = 100L
    const val SETTLE_MS = 350L
    const val PRESSED_SCALE = 0.9f
    /** How far a fling carries the button, as seconds of its velocity. */
    const val MOMENTUM_S = 0.1f
    const val SIDE_KEY = "menu-button-side"
    const val HEIGHT_KEY = "menu-button-height"
    const val LEFT = 0
    const val RIGHT = 1
    /** First spot: the right edge, a little below the middle. */
    const val DEFAULT_HEIGHT = 0.6f
  }
}
