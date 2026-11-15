package expo.modules.opencuriaappruntime

import android.content.Context
import android.net.Uri
import org.json.JSONObject

/** One app tile of the home screen widget. */
internal class WidgetApp(
  val id: String,
  val name: String,
  /** Opens the app in OpenCuria (`opencuria:///?app=<id>`). */
  val url: Uri,
  /** Path of a small copy of the app's icon, if it has one. */
  val iconPath: String?,
  /** Position among the shown tiles: alphabetical, so tiles stay put. */
  val order: Int
)

/**
 * What the Apps widget shows, as OpenCuria's JavaScript last wrote it
 * (`AppsWidgetSnapshot` in `src/widgets/appsWidgetSnapshot.ts`).
 */
internal class AppsWidgetContent(
  val signedIn: Boolean,
  /** Most recently used first. */
  val apps: List<WidgetApp>,
  /** Opens the Apps list in OpenCuria. */
  val appsUrl: Uri
) {
  companion object {
    private const val PREFERENCES = "opencuria-apps-widget"
    private const val SNAPSHOT = "snapshot"
    private const val SCHEME = "opencuria"

    /** Store a snapshot for later widget updates; throws when it is invalid. */
    fun save(context: Context, snapshot: String): AppsWidgetContent {
      val content = parse(snapshot)
      context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)
        .edit()
        .putString(SNAPSHOT, snapshot)
        .apply()
      return content
    }

    /** The last snapshot; `null` before OpenCuria first wrote one. */
    fun load(context: Context): AppsWidgetContent? {
      val snapshot = context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)
        .getString(SNAPSHOT, null) ?: return null
      return try {
        parse(snapshot)
      } catch (e: Exception) {
        null
      }
    }

    fun parse(snapshot: String): AppsWidgetContent {
      val root = JSONObject(snapshot)
      val apps = root.optJSONArray("apps")
      return AppsWidgetContent(
        signedIn = root.getString("status") == "ready",
        apps = (0 until (apps?.length() ?: 0)).mapNotNull { index ->
          val app = apps!!.getJSONObject(index)
          val url = link(app.getString("url")) ?: return@mapNotNull null
          WidgetApp(
            id = app.getString("id"),
            name = app.getString("name"),
            url = url,
            iconPath = if (app.isNull("icon")) null else Uri.parse(app.getString("icon")).path,
            order = app.optInt("order")
          )
        },
        appsUrl = link(root.getString("appsUrl")) ?: throw IllegalArgumentException("Invalid apps link")
      )
    }

    /** Only OpenCuria's own links: the widget never opens other apps. */
    private fun link(url: String): Uri? = Uri.parse(url).takeIf { it.scheme == SCHEME }
  }
}
