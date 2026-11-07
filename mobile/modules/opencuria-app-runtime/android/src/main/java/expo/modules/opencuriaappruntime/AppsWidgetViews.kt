package expo.modules.opencuriaappruntime

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.content.Context
import android.content.Intent
import android.content.res.Configuration
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.util.SizeF
import android.view.View
import android.widget.RemoteViews
import androidx.core.os.BundleCompat
import kotlin.math.min
import kotlin.math.roundToInt

/**
 * The Apps widget's views: OpenCuria's apps as home screen icons in a group.
 * The widget's size decides how many: about one launcher cell per app, the
 * most recently used first; apps that do not fit sit behind the last tile.
 */
internal class AppsWidgetViews(
  private val context: Context,
  private val content: AppsWidgetContent?
) {
  private val icons = AppIcons(context)
  private val density = context.resources.displayMetrics.density
  private val padding =
    context.resources.getDimension(R.dimen.opencuria_apps_widget_padding) / density

  /** Views for every size the launcher shows this widget at. */
  fun forOptions(options: Bundle): RemoteViews {
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
      val sizes = BundleCompat.getParcelableArrayList(
        options,
        AppWidgetManager.OPTION_APPWIDGET_SIZES,
        SizeF::class.java
      )
      if (!sizes.isNullOrEmpty()) {
        return RemoteViews(sizes.distinct().take(MAX_SIZES).associateWith { build(it.width, it.height) })
      }
    }
    // Older launchers report a range: narrow and tall in portrait, wide and low in landscape.
    val portrait =
      context.resources.configuration.orientation != Configuration.ORIENTATION_LANDSCAPE
    val width = options.getInt(
      if (portrait) AppWidgetManager.OPTION_APPWIDGET_MIN_WIDTH else AppWidgetManager.OPTION_APPWIDGET_MAX_WIDTH
    )
    val height = options.getInt(
      if (portrait) AppWidgetManager.OPTION_APPWIDGET_MAX_HEIGHT else AppWidgetManager.OPTION_APPWIDGET_MIN_HEIGHT
    )
    return build(
      width.takeIf { it > 0 }?.toFloat() ?: DEFAULT_WIDTH,
      height.takeIf { it > 0 }?.toFloat() ?: DEFAULT_HEIGHT
    )
  }

  /** Views for a widget of `width` × `height` dp. */
  fun build(width: Float, height: Float): RemoteViews {
    val small = width < MESSAGE_DETAIL_MIN_WIDTH || height < MESSAGE_DETAIL_MIN_HEIGHT
    return when {
      content == null || !content.signedIn ->
        message(R.string.opencuria_apps_widget_sign_in, null, content?.appsUrl ?: HOME)
      content.apps.isEmpty() ->
        message(
          R.string.opencuria_apps_widget_empty,
          R.string.opencuria_apps_widget_empty_detail.takeUnless { small },
          content.appsUrl
        )
      else -> grid(content, width, height)
    }
  }

  private fun grid(content: AppsWidgetContent, width: Float, height: Float): RemoteViews {
    val innerWidth = width - 2 * padding
    val innerHeight = height - 2 * padding
    val columns = (innerWidth / TILE_WIDTH).roundToInt().coerceIn(1, MAX_COLUMNS)
    val rows = (innerHeight / TILE_HEIGHT).roundToInt().coerceIn(1, MAX_ROWS)
    val tileWidth = innerWidth / columns
    val tileHeight = innerHeight / rows
    // Low tiles drop their names, like the launcher's favorites row.
    val labeled = tileHeight - LABEL_SPACE - ICON_GAP >= MIN_LABELED_ICON
    val icon = min(
      MAX_ICON,
      min(tileWidth - ICON_GAP, tileHeight - ICON_GAP - if (labeled) LABEL_SPACE else 0f)
    ).coerceAtLeast(MIN_ICON)
    val iconPixels = (icon * density).roundToInt()

    val slots = columns * rows
    val apps = content.apps
    val overflow = apps.size > slots
    val shown = apps.take(if (overflow) slots - 1 else slots).sortedBy { it.order }
    val root = RemoteViews(context.packageName, R.layout.opencuria_apps_widget)
    root.removeAllViews(android.R.id.background)
    root.setOnClickPendingIntent(android.R.id.background, launch(content.appsUrl))
    for (row in 0 until rows) {
      val line = RemoteViews(context.packageName, R.layout.opencuria_apps_widget_row)
      line.removeAllViews(R.id.opencuria_apps_widget_row)
      for (column in 0 until columns) {
        val index = row * columns + column
        line.addView(
          R.id.opencuria_apps_widget_row,
          when {
            index < shown.size -> appTile(shown[index], iconPixels, labeled)
            overflow && index == shown.size -> moreTile(apps.drop(shown.size), content.appsUrl, iconPixels, labeled)
            else -> RemoteViews(context.packageName, R.layout.opencuria_apps_widget_space)
          }
        )
      }
      root.addView(android.R.id.background, line)
    }
    return root
  }

  private fun appTile(app: WidgetApp, size: Int, labeled: Boolean): RemoteViews =
    tile(app.name, app.url, labeled).apply {
      setImageViewBitmap(R.id.opencuria_apps_widget_icon, icons.app(app, size))
    }

  /** The apps that do not fit, previewed like a launcher folder; opens the Apps list. */
  private fun moreTile(rest: List<WidgetApp>, appsUrl: Uri, size: Int, labeled: Boolean): RemoteViews =
    tile(context.getString(R.string.opencuria_apps_widget_more), appsUrl, labeled).apply {
      setViewVisibility(R.id.opencuria_apps_widget_folder, View.VISIBLE)
      setImageViewBitmap(R.id.opencuria_apps_widget_folder, icons.folder(size))
      setImageViewBitmap(R.id.opencuria_apps_widget_icon, icons.preview(rest, size))
      setContentDescription(
        R.id.opencuria_apps_widget_tile,
        context.getString(R.string.opencuria_apps_widget_more_description)
      )
    }

  private fun tile(name: String, url: Uri, labeled: Boolean): RemoteViews =
    RemoteViews(context.packageName, R.layout.opencuria_apps_widget_tile).apply {
      setTextViewText(R.id.opencuria_apps_widget_label, name)
      setViewVisibility(R.id.opencuria_apps_widget_label, if (labeled) View.VISIBLE else View.GONE)
      setContentDescription(R.id.opencuria_apps_widget_tile, name)
      setOnClickPendingIntent(R.id.opencuria_apps_widget_tile, launch(url))
    }

  private fun message(title: Int, detail: Int?, url: Uri): RemoteViews =
    RemoteViews(context.packageName, R.layout.opencuria_apps_widget_message).apply {
      setTextViewText(R.id.opencuria_apps_widget_title, context.getString(title))
      if (detail != null) {
        setTextViewText(R.id.opencuria_apps_widget_detail, context.getString(detail))
      }
      setViewVisibility(R.id.opencuria_apps_widget_detail, if (detail != null) View.VISIBLE else View.GONE)
      setOnClickPendingIntent(android.R.id.background, launch(url))
    }

  /** Opens a link through `AppLaunchActivity`; links differ by their data, so one request code serves all. */
  private fun launch(url: Uri): PendingIntent =
    PendingIntent.getActivity(
      context,
      0,
      Intent(context, AppLaunchActivity::class.java).setAction(Intent.ACTION_VIEW).setData(url),
      PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
    )

  private companion object {
    val HOME: Uri = Uri.parse("opencuria:///")
    /** About a launcher cell: an icon with its name and the space around it. */
    const val TILE_WIDTH = 80f
    const val TILE_HEIGHT = 100f
    const val MAX_COLUMNS = 8
    const val MAX_ROWS = 8
    /** Launcher icons are about 48–60 dp. */
    const val MAX_ICON = 60f
    const val MIN_ICON = 24f
    const val MIN_LABELED_ICON = 40f
    const val ICON_GAP = 12f
    /** One line of 12 sp text and its margin. */
    const val LABEL_SPACE = 20f
    const val MESSAGE_DETAIL_MIN_WIDTH = 200f
    const val MESSAGE_DETAIL_MIN_HEIGHT = 100f
    /** A 4 × 2 widget, when a launcher reports no size. */
    const val DEFAULT_WIDTH = 320f
    const val DEFAULT_HEIGHT = 180f
    /** RemoteViews accepts up to 16 sizes. */
    const val MAX_SIZES = 16
  }
}
