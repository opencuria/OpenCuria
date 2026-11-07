package expo.modules.opencuriaappruntime

import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.os.Bundle

/**
 * The Apps home screen widget: the apps OpenCuria runs, as home screen
 * icons in a group. OpenCuria's JavaScript writes its content
 * (`updateAppsWidget`); the launcher asks for views on placement and resize.
 */
class AppsWidgetProvider : AppWidgetProvider() {
  override fun onUpdate(context: Context, manager: AppWidgetManager, appWidgetIds: IntArray) {
    update(context, manager, appWidgetIds, AppsWidgetContent.load(context))
  }

  override fun onAppWidgetOptionsChanged(
    context: Context,
    manager: AppWidgetManager,
    appWidgetId: Int,
    newOptions: Bundle
  ) {
    update(context, manager, intArrayOf(appWidgetId), AppsWidgetContent.load(context))
  }

  companion object {
    /** Show new content in every placed Apps widget. */
    internal fun updateAll(context: Context, content: AppsWidgetContent) {
      val manager = AppWidgetManager.getInstance(context) ?: return
      val ids = manager.getAppWidgetIds(ComponentName(context, AppsWidgetProvider::class.java))
      update(context, manager, ids, content)
    }

    private fun update(
      context: Context,
      manager: AppWidgetManager,
      ids: IntArray,
      content: AppsWidgetContent?
    ) {
      if (ids.isEmpty()) return
      val views = AppsWidgetViews(context, content)
      for (id in ids) {
        manager.updateAppWidget(id, views.forOptions(manager.getAppWidgetOptions(id)))
      }
    }
  }
}
