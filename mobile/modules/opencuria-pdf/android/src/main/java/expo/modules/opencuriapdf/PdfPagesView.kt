package expo.modules.opencuriapdf

import android.animation.Animator
import android.animation.AnimatorListenerAdapter
import android.animation.ValueAnimator
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Matrix
import android.graphics.Paint
import android.graphics.RectF
import android.graphics.pdf.PdfRenderer
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.os.ParcelFileDescriptor
import android.util.LruCache
import android.view.GestureDetector
import android.view.MotionEvent
import android.view.ScaleGestureDetector
import android.view.View
import android.view.animation.DecelerateInterpolator
import android.widget.OverScroller
import java.io.File
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sqrt

private const val MAX_ZOOM = 5f
private const val DOUBLE_TAP_ZOOM = 2.5f
private const val SHARPEN_DELAY_MS = 120L

/**
 * Continuous vertical pages rendered with the platform [PdfRenderer]: fling
 * scrolling, pinch and double-tap zoom, and sharp re-rendering of the visible
 * area once a zoom settles. All renderer access runs on one worker thread.
 */
internal class PdfPagesView(
  context: Context,
  private val onLoaded: (pageCount: Int) -> Unit,
  private val onFailed: (code: String, message: String) -> Unit,
  private val onPage: (page: Int, pageCount: Int) -> Unit,
) : View(context) {
  private class Tile(val bitmap: Bitmap, val left: Float, val top: Float, val zoom: Float, val scrollX: Float, val scrollY: Float)
  private class Load(val sizes: FloatArray?, val code: String, val message: String)

  private val density = resources.displayMetrics.density
  private val gap = 8 * density
  private val main = Handler(Looper.getMainLooper())
  private val worker: ExecutorService = Executors.newSingleThreadExecutor { task ->
    Thread(task, "OpenCuriaPdf").apply { isDaemon = true }
  }

  // Worker thread only.
  private var renderer: PdfRenderer? = null
  private var descriptor: ParcelFileDescriptor? = null
  @Volatile private var rendererGeneration = 0
  @Volatile private var wantedFirst = 0
  @Volatile private var wantedLast = -1

  // Main thread only.
  private var released = false
  private var source: String? = null
  private var generation = 0
  private var layoutVersion = 0
  /** Page sizes in PDF points: width, height, width, height, … */
  private var sizes = FloatArray(0)
  private var lefts = FloatArray(0)
  private var tops = FloatArray(0)
  private var widths = FloatArray(0)
  private var heights = FloatArray(0)
  private var contentHeight = 0f
  private var zoom = 1f
  private var offsetX = 0f
  private var offsetY = 0f
  private var currentPage = 0
  private var scaling = false
  private var tiles = emptyList<Tile>()
  private var tileToken = 0
  private val pending = HashSet<Int>()
  private val pages = object : LruCache<Int, Bitmap>(cacheBytes()) {
    override fun sizeOf(key: Int, value: Bitmap) = value.byteCount
  }
  private val bitmapPaint = Paint(Paint.FILTER_BITMAP_FLAG or Paint.ANTI_ALIAS_FLAG)
  private val placeholderPaint = Paint().apply { color = Color.WHITE }
  private val edgePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
    style = Paint.Style.STROKE
    strokeWidth = max(1f, density / 2)
    color = 0x24000000
  }
  private val pageRect = RectF()
  private val scroller = OverScroller(context)
  private var zoomAnimator: ValueAnimator? = null
  private val sharpen = Runnable { renderTiles() }

  var bottomInset = 0f
    set(value) {
      if (field == value) return
      field = value
      moveTo(offsetX, offsetY)
    }

  private val pageCount get() = sizes.size / 2

  private val scaleDetector = ScaleGestureDetector(
    context,
    object : ScaleGestureDetector.SimpleOnScaleGestureListener() {
      override fun onScaleBegin(detector: ScaleGestureDetector): Boolean {
        scaling = true
        scroller.forceFinished(true)
        return true
      }

      override fun onScale(detector: ScaleGestureDetector): Boolean {
        zoomTo(zoom * detector.scaleFactor, detector.focusX, detector.focusY)
        return true
      }

      override fun onScaleEnd(detector: ScaleGestureDetector) {
        scaling = false
        scheduleSharpen()
      }
    },
  )

  private val gestureDetector = GestureDetector(
    context,
    object : GestureDetector.SimpleOnGestureListener() {
      // The second tap of a double tap also arrives here: keep its zoom running.
      override fun onDown(e: MotionEvent): Boolean {
        scroller.forceFinished(true)
        return true
      }

      override fun onScroll(e1: MotionEvent?, e2: MotionEvent, distanceX: Float, distanceY: Float): Boolean {
        if (scaling) return false
        moveTo(offsetX + distanceX, offsetY + distanceY)
        return true
      }

      override fun onFling(e1: MotionEvent?, e2: MotionEvent, velocityX: Float, velocityY: Float): Boolean {
        if (scaling) return false
        scroller.fling(
          offsetX.roundToInt(), offsetY.roundToInt(),
          -velocityX.roundToInt(), -velocityY.roundToInt(),
          0, maxOffsetX().roundToInt(), 0, maxOffsetY().roundToInt(),
        )
        postInvalidateOnAnimation()
        return true
      }

      override fun onDoubleTap(e: MotionEvent): Boolean {
        animateZoom(if (zoom > 1.05f) 1f else DOUBLE_TAP_ZOOM, e.x, e.y)
        return true
      }
    },
  )

  init {
    importantForAccessibility = IMPORTANT_FOR_ACCESSIBILITY_YES
    // Double tap zooms; double-tap-and-drag would compete with it.
    scaleDetector.isQuickScaleEnabled = false
  }

  /** Open a local `file://` PDF; null or a new URI closes the current one. */
  fun open(value: String?) {
    if (released || value == source) return
    source = value
    generation += 1
    val requested = generation
    sizes = FloatArray(0)
    reset()
    worker.execute { closeRenderer() }
    if (value.isNullOrEmpty()) return
    worker.execute {
      val load = openRenderer(value, requested)
      main.post {
        if (released || requested != generation) return@post
        if (load.sizes == null) {
          onFailed(load.code, load.message)
          return@post
        }
        sizes = load.sizes
        relayout()
        onLoaded(pageCount)
        updateCurrentPage()
      }
    }
  }

  /** Stop rendering and close the document; the view cannot be reused. */
  fun release() {
    if (released) return
    released = true
    generation += 1
    reset()
    worker.execute { closeRenderer() }
    worker.shutdown()
  }

  private fun reset() {
    removeCallbacks(sharpen)
    zoomAnimator?.cancel()
    scroller.forceFinished(true)
    layoutVersion += 1
    pages.evictAll()
    pending.clear()
    tiles = emptyList()
    zoom = 1f
    offsetX = 0f
    offsetY = 0f
    currentPage = 0
    contentDescription = null
    lefts = FloatArray(0)
    tops = FloatArray(0)
    widths = FloatArray(0)
    heights = FloatArray(0)
    contentHeight = 0f
    invalidate()
  }

  private fun openRenderer(value: String, requested: Int): Load {
    var file: ParcelFileDescriptor? = null
    return try {
      val uri = Uri.parse(value)
      val path = uri.path
      if (uri.scheme != "file" || path.isNullOrEmpty()) {
        return Load(null, "invalid_source", "Only local PDF files can be shown.")
      }
      file = ParcelFileDescriptor.open(File(path), ParcelFileDescriptor.MODE_READ_ONLY)
      val opened = PdfRenderer(file)
      val count = opened.pageCount
      val result = FloatArray(count * 2)
      for (index in 0 until count) {
        val page = opened.openPage(index)
        result[index * 2] = page.width.toFloat()
        result[index * 2 + 1] = page.height.toFloat()
        page.close()
      }
      if (count == 0) {
        opened.close()
        file.close()
        return Load(null, "invalid_pdf", "This PDF has no pages.")
      }
      renderer = opened
      descriptor = file
      rendererGeneration = requested
      Load(result, "", "")
    } catch (error: SecurityException) {
      file?.close()
      Load(null, "locked", "This PDF is password-protected.")
    } catch (error: Exception) {
      file?.close()
      Load(null, "invalid_pdf", "This PDF can't be opened.")
    }
  }

  private fun closeRenderer() {
    rendererGeneration = 0
    try {
      renderer?.close()
      descriptor?.close()
    } catch (_: Exception) {
    }
    renderer = null
    descriptor = null
  }

  /** Worker thread: render one page (or a region of it, via [transform]). */
  private fun renderPage(requested: Int, index: Int, width: Int, height: Int, transform: Matrix?): Bitmap? {
    val current = renderer ?: return null
    if (rendererGeneration != requested || width <= 0 || height <= 0) return null
    return try {
      val page = current.openPage(index)
      try {
        Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888).apply {
          eraseColor(Color.WHITE)
          page.render(this, null, transform, PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY)
        }
      } finally {
        page.close()
      }
    } catch (_: Exception) {
      null
    } catch (_: OutOfMemoryError) {
      null
    }
  }

  override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
    super.onSizeChanged(w, h, oldw, oldh)
    if (w != oldw) relayout() else moveTo(offsetX, offsetY)
  }

  /** Fit the widest page to the view width; keep the current page in view. */
  private fun relayout() {
    val count = pageCount
    if (width == 0 || count == 0) return
    // Rotation keeps the content at the center of the view in place.
    val atTop = offsetY <= 0f || heights.size != count
    val anchor = currentPage
    val anchorFraction = if (atTop) 0f else {
      ((offsetY + height / 2f - originY(zoom)) / zoom - tops[anchor]) / heights[anchor]
    }
    var widest = 0f
    for (index in 0 until count) widest = max(widest, sizes[index * 2])
    val scale = (width - 2 * gap) / max(widest, 1f)
    lefts = FloatArray(count)
    tops = FloatArray(count)
    widths = FloatArray(count)
    heights = FloatArray(count)
    var y = gap
    for (index in 0 until count) {
      widths[index] = sizes[index * 2] * scale
      heights[index] = sizes[index * 2 + 1] * scale
      lefts[index] = (width - widths[index]) / 2
      tops[index] = y
      y += heights[index] + gap
    }
    contentHeight = y
    layoutVersion += 1
    pages.evictAll()
    pending.clear()
    tiles = emptyList()
    val anchorY = (tops[anchor] + anchorFraction.coerceIn(0f, 1f) * heights[anchor]) * zoom
    moveTo(offsetX, if (atTop) 0f else anchorY - height / 2f + originY(zoom))
  }

  /** Short documents, such as a single card, sit vertically centered. */
  private fun originY(scale: Float) = max(0f, (height - bottomInset - contentHeight * scale) / 2)

  private fun maxOffsetX() = max(0f, width * zoom - width)

  private fun maxOffsetY() = max(0f, contentHeight * zoom + bottomInset - height)

  private fun moveTo(x: Float, y: Float) {
    offsetX = x.coerceIn(0f, maxOffsetX())
    offsetY = y.coerceIn(0f, maxOffsetY())
    updateCurrentPage()
    invalidate()
  }

  private fun zoomTo(value: Float, focusX: Float, focusY: Float) {
    val next = value.coerceIn(1f, MAX_ZOOM)
    val contentX = (offsetX + focusX) / zoom
    val contentY = (offsetY + focusY - originY(zoom)) / zoom
    zoom = next
    moveTo(contentX * next - focusX, contentY * next + originY(next) - focusY)
  }

  private fun animateZoom(target: Float, focusX: Float, focusY: Float) {
    zoomAnimator?.cancel()
    zoomAnimator = ValueAnimator.ofFloat(zoom, target).apply {
      duration = 220
      interpolator = DecelerateInterpolator()
      addUpdateListener { zoomTo(it.animatedValue as Float, focusX, focusY) }
      addListener(object : AnimatorListenerAdapter() {
        private var cancelled = false

        override fun onAnimationCancel(animation: Animator) {
          cancelled = true
        }

        override fun onAnimationEnd(animation: Animator) {
          // With system animations off, no update carries the final value.
          if (!cancelled) zoomTo(target, focusX, focusY)
          scheduleSharpen()
        }
      })
      start()
    }
  }

  override fun onTouchEvent(event: MotionEvent): Boolean {
    if (event.actionMasked == MotionEvent.ACTION_DOWN) parent?.requestDisallowInterceptTouchEvent(true)
    scaleDetector.onTouchEvent(event)
    gestureDetector.onTouchEvent(event)
    if (event.actionMasked == MotionEvent.ACTION_UP && scroller.isFinished) scheduleSharpen()
    return true
  }

  override fun computeScroll() {
    if (!scroller.computeScrollOffset()) return
    moveTo(scroller.currX.toFloat(), scroller.currY.toFloat())
    if (scroller.isFinished) scheduleSharpen() else postInvalidateOnAnimation()
  }

  private fun pageAt(contentY: Float): Int {
    var low = 0
    var high = pageCount - 1
    while (low < high) {
      val mid = (low + high + 1) / 2
      if (tops[mid] - gap / 2 <= contentY) low = mid else high = mid - 1
    }
    return low
  }

  private fun updateCurrentPage() {
    if (pageCount == 0 || tops.size != pageCount) return
    val page = pageAt((offsetY + (height - bottomInset) / 2 - originY(zoom)) / zoom)
    if (page == currentPage && contentDescription != null) return
    currentPage = page
    contentDescription = "PDF, page ${page + 1} of $pageCount"
    onPage(page + 1, pageCount)
  }

  override fun onDraw(canvas: Canvas) {
    super.onDraw(canvas)
    val count = pageCount
    if (count == 0 || tops.size != count) return
    val top = originY(zoom) - offsetY
    val first = pageAt(-top / zoom)
    val last = pageAt((height - top) / zoom)
    wantedFirst = max(0, first - 1)
    wantedLast = min(count - 1, last + 1)
    for (index in first..last) {
      pageRect.set(
        lefts[index] * zoom - offsetX,
        top + tops[index] * zoom,
        (lefts[index] + widths[index]) * zoom - offsetX,
        top + (tops[index] + heights[index]) * zoom,
      )
      val bitmap = pages.get(index)
      if (bitmap != null) canvas.drawBitmap(bitmap, null, pageRect, bitmapPaint)
      else {
        canvas.drawRect(pageRect, placeholderPaint)
        requestPage(index)
      }
      canvas.drawRect(pageRect, edgePaint)
    }
    if (first > 0) requestPage(first - 1)
    if (last < count - 1) requestPage(last + 1)
    for (tile in tiles) {
      if (abs(tile.zoom - zoom) > 0.0001f) continue
      canvas.drawBitmap(tile.bitmap, tile.left + tile.scrollX - offsetX, tile.top + tile.scrollY - offsetY, bitmapPaint)
    }
  }

  /** Render a page at the fitted width; zoomed views add sharp [tiles]. */
  private fun requestPage(index: Int) {
    if (released || !pending.add(index)) return
    val requested = generation
    val version = layoutVersion
    // Very tall pages render at a lower resolution than fits the cache.
    val bytes = widths[index] * heights[index] * 4
    val shrink = min(1f, sqrt(pages.maxSize() / 4f / max(bytes, 1f)))
    val pageWidth = (widths[index] * shrink).roundToInt()
    val pageHeight = (heights[index] * shrink).roundToInt()
    worker.execute {
      val bitmap = if (index in wantedFirst..wantedLast) renderPage(requested, index, pageWidth, pageHeight, null) else null
      main.post {
        if (released || requested != generation || version != layoutVersion) return@post
        pending.remove(index)
        if (bitmap != null) {
          pages.put(index, bitmap)
          invalidate()
        }
      }
    }
  }

  private fun scheduleSharpen() {
    removeCallbacks(sharpen)
    if (zoom > 1.01f) postDelayed(sharpen, SHARPEN_DELAY_MS) else if (tiles.isNotEmpty()) {
      tiles = emptyList()
      invalidate()
    }
  }

  /** Render the visible part of each page at the current zoom. */
  private fun renderTiles() {
    val count = pageCount
    if (released || zoom <= 1.01f || scaling || !scroller.isFinished || count == 0) return
    val requested = generation
    val version = layoutVersion
    val scale = zoom
    val scrollX = offsetX
    val scrollY = offsetY
    val top = originY(scale) - scrollY
    val jobs = mutableListOf<Triple<Int, RectF, Matrix>>()
    for (index in pageAt(-top / scale)..pageAt((height - top) / scale)) {
      val left = lefts[index] * scale - scrollX
      val pageTop = top + tops[index] * scale
      val visible = RectF(
        max(0f, left),
        max(0f, pageTop),
        min(width.toFloat(), left + widths[index] * scale),
        min(height.toFloat(), pageTop + heights[index] * scale),
      )
      if (visible.width() < 1 || visible.height() < 1) continue
      val pointScale = widths[index] * scale / sizes[index * 2]
      val transform = Matrix().apply {
        setScale(pointScale, pointScale)
        postTranslate(left - visible.left, pageTop - visible.top)
      }
      jobs += Triple(index, visible, transform)
    }
    val token = ++tileToken
    worker.execute {
      val rendered = jobs.mapNotNull { (index, visible, transform) ->
        renderPage(requested, index, ceil(visible.width()).toInt(), ceil(visible.height()).toInt(), transform)
          ?.let { Tile(it, visible.left, visible.top, scale, scrollX, scrollY) }
      }
      main.post {
        if (released || token != tileToken || requested != generation || version != layoutVersion) return@post
        tiles = rendered
        invalidate()
      }
    }
  }

  private fun cacheBytes(): Int =
    min(Runtime.getRuntime().maxMemory() / 6, 96L * 1024 * 1024).toInt()
}
