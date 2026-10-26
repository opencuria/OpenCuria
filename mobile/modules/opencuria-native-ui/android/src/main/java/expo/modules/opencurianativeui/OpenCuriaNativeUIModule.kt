package expo.modules.opencurianativeui

import android.content.Context
import android.graphics.Color
import android.view.View
import androidx.appcompat.widget.Toolbar
import androidx.core.content.ContextCompat
import expo.modules.kotlin.AppContext
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import expo.modules.kotlin.viewevent.EventDispatcher
import expo.modules.kotlin.views.ExpoView

/** A system Toolbar that can also live inside Android's native modal dialog. */
class OpenCuriaToolbarView(context: Context, appContext: AppContext) : ExpoView(context, appContext) {
  override val shouldUseAndroidLayout = true
  private val toolbar = Toolbar(context)
  private val onClose by EventDispatcher()
  private var closeVisible = false
  private var tint = Color.BLACK

  init {
    addView(toolbar, LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.MATCH_PARENT))
    toolbar.navigationContentDescription = "Close"
    toolbar.setNavigationOnClickListener { if (closeVisible) onClose(emptyMap<String, Any>()) }
  }

  fun setTitle(value: String) { toolbar.title = value }
  fun setTitleColor(value: String) { toolbar.setTitleTextColor(Color.parseColor(value)) }
  fun setSurfaceColor(value: String) { toolbar.setBackgroundColor(Color.parseColor(value)) }
  fun setTintColor(value: String) { tint = Color.parseColor(value); updateCloseIcon() }
  fun setShowClose(value: Boolean) { closeVisible = value; updateCloseIcon() }

  private fun updateCloseIcon() {
    toolbar.navigationIcon = if (closeVisible) {
      ContextCompat.getDrawable(context, android.R.drawable.ic_menu_close_clear_cancel)?.mutate()?.apply { setTint(tint) }
    } else null
  }

  override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
    super.onMeasure(widthMeasureSpec, heightMeasureSpec)
    toolbar.measure(View.MeasureSpec.makeMeasureSpec(measuredWidth, View.MeasureSpec.EXACTLY), View.MeasureSpec.makeMeasureSpec(measuredHeight, View.MeasureSpec.EXACTLY))
  }

  override fun onLayout(changed: Boolean, left: Int, top: Int, right: Int, bottom: Int) {
    toolbar.layout(0, 0, right - left, bottom - top)
  }
}

class OpenCuriaNativeUIModule : Module() {
  override fun definition() = ModuleDefinition {
    Name("OpenCuriaNativeUI")
    View(OpenCuriaToolbarView::class) {
      Events("onClose")
      Prop("title") { view: OpenCuriaToolbarView, value: String -> view.setTitle(value) }
      Prop("titleColor") { view: OpenCuriaToolbarView, value: String -> view.setTitleColor(value) }
      Prop("surfaceColor") { view: OpenCuriaToolbarView, value: String -> view.setSurfaceColor(value) }
      Prop("tintColor") { view: OpenCuriaToolbarView, value: String -> view.setTintColor(value) }
      Prop("showClose") { view: OpenCuriaToolbarView, value: Boolean -> view.setShowClose(value) }
    }
  }
}
