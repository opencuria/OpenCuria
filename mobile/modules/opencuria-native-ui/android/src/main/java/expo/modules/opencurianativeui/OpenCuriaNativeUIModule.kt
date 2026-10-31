package expo.modules.opencurianativeui

import android.content.Context
import android.graphics.Color
import android.view.Menu
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
  private val onMenuAction by EventDispatcher()
  private var closeVisible = false
  private var tint = Color.BLACK
  private var menuIds = emptyList<String>()

  init {
    addView(toolbar, LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.MATCH_PARENT))
    toolbar.navigationContentDescription = "Close"
    toolbar.setNavigationOnClickListener { if (closeVisible) onClose(emptyMap<String, Any>()) }
    toolbar.setOnMenuItemClickListener { item ->
      menuIds.getOrNull(item.itemId)?.let { onMenuAction(mapOf("id" to it)) }
      true
    }
  }

  fun setTitle(value: String) { toolbar.title = value }
  fun setSubtitle(value: String) { toolbar.subtitle = value.ifEmpty { null } }
  fun setTitleColor(value: String) { toolbar.setTitleTextColor(Color.parseColor(value)) }
  fun setSubtitleColor(value: String) { toolbar.setSubtitleTextColor(Color.parseColor(value)) }
  fun setSurfaceColor(value: String) { toolbar.setBackgroundColor(Color.parseColor(value)) }
  fun setTintColor(value: String) {
    tint = Color.parseColor(value)
    updateCloseIcon()
    toolbar.overflowIcon?.mutate()?.setTint(tint)
  }
  fun setShowClose(value: Boolean) { closeVisible = value; updateCloseIcon() }

  /** Overflow menu entries: `{ id, title, enabled, checked }`. */
  fun setMenuItems(items: List<Map<String, Any?>>) {
    toolbar.menu.clear()
    menuIds = items.map { it["id"]?.toString() ?: "" }
    items.forEachIndexed { index, item ->
      toolbar.menu.add(Menu.NONE, index, index, item["title"]?.toString() ?: "").apply {
        isEnabled = item["enabled"] as? Boolean ?: true
        val checked = item["checked"] as? Boolean ?: false
        isCheckable = checked
        isChecked = checked
      }
    }
    toolbar.overflowIcon?.mutate()?.setTint(tint)
  }

  private fun updateCloseIcon() {
    toolbar.navigationIcon = if (closeVisible) {
      ContextCompat.getDrawable(context, androidx.appcompat.R.drawable.abc_ic_clear_material)?.mutate()?.apply { setTint(tint) }
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
      Events("onClose", "onMenuAction")
      Prop("title") { view: OpenCuriaToolbarView, value: String -> view.setTitle(value) }
      Prop("subtitle") { view: OpenCuriaToolbarView, value: String -> view.setSubtitle(value) }
      Prop("titleColor") { view: OpenCuriaToolbarView, value: String -> view.setTitleColor(value) }
      Prop("subtitleColor") { view: OpenCuriaToolbarView, value: String -> view.setSubtitleColor(value) }
      Prop("menuItems") { view: OpenCuriaToolbarView, value: List<Map<String, Any?>> -> view.setMenuItems(value) }
      Prop("surfaceColor") { view: OpenCuriaToolbarView, value: String -> view.setSurfaceColor(value) }
      Prop("tintColor") { view: OpenCuriaToolbarView, value: String -> view.setTintColor(value) }
      Prop("showClose") { view: OpenCuriaToolbarView, value: Boolean -> view.setShowClose(value) }
    }
  }
}
