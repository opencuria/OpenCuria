package expo.modules.opencuriaappruntime

import android.content.Context
import java.util.EnumSet
import java.util.Locale

/** A way to open an installed app's menu. */
enum class AppMenuTrigger(internal val key: String, internal val hint: Int) {
  SHAKE("menu-shake", R.string.opencuria_app_hint_shake),
  THREE_FINGER_PRESS("menu-three-finger-press", R.string.opencuria_app_hint_three_fingers),
  BUTTON("menu-button", R.string.opencuria_app_hint_button)
}

/**
 * Which ways open the app menu, shared by every app like Expo Go's developer
 * menu gestures. All are on at first; the last one cannot be turned off, so
 * an app can always be left.
 */
class AppMenuTriggers(private val context: Context) {
  private val prefs = context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)

  /** The ways that are on; never empty. */
  val enabled: Set<AppMenuTrigger>
    get() {
      val on = AppMenuTrigger.entries.filterTo(EnumSet.noneOf(AppMenuTrigger::class.java)) {
        prefs.getBoolean(it.key, true)
      }
      return on.ifEmpty { EnumSet.allOf(AppMenuTrigger::class.java) }
    }

  fun isEnabled(trigger: AppMenuTrigger): Boolean = trigger in enabled

  /** Whether turning [trigger] off would leave no way to open the menu. */
  fun isLast(trigger: AppMenuTrigger): Boolean = enabled == setOf(trigger)

  /** Turn a way on or off; the last one stays on. */
  fun set(trigger: AppMenuTrigger, on: Boolean) {
    if (!on && isLast(trigger)) return
    prefs.edit().putBoolean(trigger.key, on).apply()
  }

  /** How to close the app with the ways that are on, e.g. "Shake or tap the menu button to close the app". */
  fun closeHint(): String {
    val ways = enabled.map { context.getString(it.hint) }
    val joined = when (ways.size) {
      1 -> ways[0]
      else -> context.getString(
        R.string.opencuria_app_hint_or,
        ways.dropLast(1).joinToString(", "),
        ways.last()
      )
    }
    return context.getString(R.string.opencuria_app_close_hint, joined)
      .replaceFirstChar { it.titlecase(Locale.getDefault()) }
  }

  companion object {
    /** The runtime's own preferences (menu ways, menu button position). */
    const val PREFERENCES = "opencuria-app-runtime"
  }
}
