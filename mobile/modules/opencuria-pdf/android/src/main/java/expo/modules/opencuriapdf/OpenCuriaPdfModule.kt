package expo.modules.opencuriapdf

import android.content.Context
import android.view.View
import expo.modules.kotlin.AppContext
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import expo.modules.kotlin.viewevent.EventDispatcher
import expo.modules.kotlin.views.ExpoView

/** Bridges [PdfPagesView] to React Native; it holds no app logic. */
class OpenCuriaPdfView(context: Context, appContext: AppContext) : ExpoView(context, appContext) {
  override val shouldUseAndroidLayout = true
  private val onLoad by EventDispatcher()
  private val onError by EventDispatcher()
  private val onPageChange by EventDispatcher()
  private val pages = PdfPagesView(
    context,
    onLoaded = { count -> onLoad(mapOf("pageCount" to count)) },
    onFailed = { code, message -> onError(mapOf("code" to code, "message" to message)) },
    onPage = { page, count -> onPageChange(mapOf("page" to page, "pageCount" to count)) },
  )

  init {
    addView(pages, LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.MATCH_PARENT))
  }

  fun setSource(value: String?) = pages.open(value)

  fun setContentInsetBottom(value: Double) {
    pages.bottomInset = (value * resources.displayMetrics.density).toFloat()
  }

  fun release() = pages.release()

  override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
    super.onMeasure(widthMeasureSpec, heightMeasureSpec)
    pages.measure(
      View.MeasureSpec.makeMeasureSpec(measuredWidth, View.MeasureSpec.EXACTLY),
      View.MeasureSpec.makeMeasureSpec(measuredHeight, View.MeasureSpec.EXACTLY),
    )
  }

  override fun onLayout(changed: Boolean, left: Int, top: Int, right: Int, bottom: Int) {
    pages.layout(0, 0, right - left, bottom - top)
  }
}

class OpenCuriaPdfModule : Module() {
  override fun definition() = ModuleDefinition {
    Name("OpenCuriaPdf")
    View(OpenCuriaPdfView::class) {
      Events("onLoad", "onError", "onPageChange")
      Prop("source") { view: OpenCuriaPdfView, value: String? -> view.setSource(value) }
      Prop("contentInsetBottom") { view: OpenCuriaPdfView, value: Double? ->
        view.setContentInsetBottom(value ?: 0.0)
      }
      OnViewDestroys { view -> view.release() }
    }
  }
}
