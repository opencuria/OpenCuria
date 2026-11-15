package expo.modules.opencuriaappruntime

import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.TextView
import androidx.core.graphics.Insets
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat

/** A small note at the bottom of a just opened app on how to close it; it fades after a few seconds. */
internal object AppCloseHint {
  private const val VISIBLE_MS = 4_000L
  private const val FADE_MS = 300L

  fun show(container: FrameLayout, text: String) {
    val context = container.context
    val density = context.resources.displayMetrics.density
    fun dp(value: Int) = (value * density).toInt()
    val hint = TextView(context).apply {
      this.text = text
      textSize = 13f
      gravity = Gravity.CENTER
      setTextColor(Color.WHITE)
      setPadding(dp(14), dp(7), dp(14), dp(7))
      background = GradientDrawable().apply {
        cornerRadius = dp(16).toFloat()
        setColor(Color.argb(180, 0, 0, 0))
      }
      alpha = 0f
      // Touches go to the app; screen readers read the note once.
      isClickable = false
      isFocusable = false
      accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
    }
    container.addView(
      hint,
      FrameLayout.LayoutParams(
        ViewGroup.LayoutParams.WRAP_CONTENT,
        ViewGroup.LayoutParams.WRAP_CONTENT,
        Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL
      )
    )
    // Clear of the system bars; a view added this late gets no insets
    // until they are dispatched again, so start from the current ones.
    fun keepClear(insets: WindowInsetsCompat?) {
      val bars = insets?.getInsets(
        WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout()
      ) ?: Insets.NONE
      hint.layoutParams = (hint.layoutParams as FrameLayout.LayoutParams).apply {
        setMargins(bars.left + dp(24), 0, bars.right + dp(24), bars.bottom + dp(24))
      }
    }
    keepClear(ViewCompat.getRootWindowInsets(container))
    ViewCompat.setOnApplyWindowInsetsListener(hint) { _, insets ->
      keepClear(insets)
      insets
    }
    hint.animate().alpha(1f).setDuration(FADE_MS).withEndAction {
      hint.animate().alpha(0f).setStartDelay(VISIBLE_MS).setDuration(FADE_MS).withEndAction {
        container.removeView(hint)
      }
    }
  }
}
