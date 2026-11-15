package expo.modules.opencuriaappruntime

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.BitmapShader
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Matrix
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Shader
import android.graphics.drawable.AdaptiveIconDrawable
import android.graphics.drawable.ColorDrawable
import android.os.Build
import androidx.core.content.ContextCompat
import kotlin.math.max
import kotlin.math.roundToInt

/**
 * Icons for the Apps widget, drawn in the shape the device gives app icons
 * (circle, squircle, …), so they match the icons around the widget. One
 * instance serves one widget update and reuses what it drew.
 */
internal class AppIcons(private val context: Context) {
  private val bitmaps = HashMap<String, Bitmap>()
  private val shapes = HashMap<Int, Path>()

  /** The app's icon, or OpenCuria's app tile for apps without one. */
  fun app(app: WidgetApp, size: Int): Bitmap =
    bitmaps.getOrPut("app:${app.iconPath}:$size") {
      app.iconPath?.let(BitmapFactory::decodeFile)?.let { masked(it, size) } ?: appTile(size)
    }

  /** The icon shape in white; the layout tints it as a folder background. */
  fun folder(size: Int): Bitmap =
    bitmaps.getOrPut("folder:$size") {
      draw(size) { canvas, paint ->
        paint.color = Color.WHITE
        canvas.drawPath(shape(size), paint)
      }
    }

  /** Up to four apps, small, as the launcher previews a folder's content. */
  fun preview(apps: List<WidgetApp>, size: Int): Bitmap = draw(size) { canvas, paint ->
    val inset = size * PREVIEW_INSET
    val gap = size * PREVIEW_GAP
    val mini = ((size - 2 * inset - gap) / 2).roundToInt().coerceAtLeast(1)
    apps.take(4).forEachIndexed { index, item ->
      val left = inset + (index % 2) * (mini + gap)
      val top = inset + (index / 2) * (mini + gap)
      canvas.drawBitmap(app(item, mini), left, top, paint)
    }
  }

  private fun masked(source: Bitmap, size: Int): Bitmap = draw(size) { canvas, paint ->
    // Full-bleed artwork, centered and cropped to the icon shape.
    val scale = max(size / source.width.toFloat(), size / source.height.toFloat())
    paint.shader = BitmapShader(source, Shader.TileMode.CLAMP, Shader.TileMode.CLAMP).apply {
      setLocalMatrix(
        Matrix().apply {
          setScale(scale, scale)
          postTranslate((size - source.width * scale) / 2, (size - source.height * scale) / 2)
        }
      )
    }
    canvas.drawPath(shape(size), paint)
  }

  /** The tile OpenCuria shows for apps without an icon: indigo with the apps symbol. */
  private fun appTile(size: Int): Bitmap = draw(size) { canvas, paint ->
    paint.color = APP_TILE_COLOR
    canvas.drawPath(shape(size), paint)
    ContextCompat.getDrawable(context, R.drawable.opencuria_apps_widget_symbol)?.mutate()?.apply {
      setTint(Color.WHITE)
      val glyph = (size * APP_TILE_GLYPH).roundToInt()
      val start = (size - glyph) / 2
      setBounds(start, start, start + glyph, start + glyph)
      draw(canvas)
    }
  }

  /** The launcher's icon shape at `size` pixels (rounded square before Android 8). */
  private fun shape(size: Int): Path = shapes.getOrPut(size) {
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
      AdaptiveIconDrawable(ColorDrawable(Color.BLACK), ColorDrawable(Color.BLACK)).run {
        setBounds(0, 0, size, size)
        Path(iconMask)
      }
    } else {
      val radius = size * LEGACY_CORNER
      Path().apply {
        addRoundRect(0f, 0f, size.toFloat(), size.toFloat(), radius, radius, Path.Direction.CW)
      }
    }
  }

  private inline fun draw(size: Int, block: (Canvas, Paint) -> Unit): Bitmap {
    val bitmap = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888)
    block(Canvas(bitmap), Paint(Paint.ANTI_ALIAS_FLAG or Paint.FILTER_BITMAP_FLAG))
    return bitmap
  }

  private companion object {
    /** `tileColor("indigo")` of OpenCuria's app tiles (UIKit system indigo). */
    val APP_TILE_COLOR = Color.rgb(0x58, 0x56, 0xD6)
    const val APP_TILE_GLYPH = 0.5f
    const val PREVIEW_INSET = 0.17f
    const val PREVIEW_GAP = 0.07f
    const val LEGACY_CORNER = 0.22f
  }
}
