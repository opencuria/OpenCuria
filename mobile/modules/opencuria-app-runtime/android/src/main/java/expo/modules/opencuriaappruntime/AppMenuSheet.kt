package expo.modules.opencuriaappruntime

import android.app.Activity
import android.graphics.BitmapFactory
import android.graphics.Outline
import android.view.ContextThemeWrapper
import android.view.LayoutInflater
import android.view.View
import android.view.ViewOutlineProvider
import android.widget.ImageView
import android.widget.TextView
import com.google.android.material.bottomsheet.BottomSheetBehavior
import com.google.android.material.bottomsheet.BottomSheetDialog
import com.google.android.material.materialswitch.MaterialSwitch

/**
 * The app menu: a bottom sheet like Expo Go's developer menu, reduced to the
 * app, Close app and Reload, and switches for the ways that open the menu.
 */
internal object AppMenuSheet {
  fun show(
    activity: Activity,
    launch: AppLaunch,
    triggers: AppMenuTriggers,
    onClose: () -> Unit,
    onReload: () -> Unit,
    onTriggersChanged: () -> Unit
  ): BottomSheetDialog {
    val dialog = BottomSheetDialog(ContextThemeWrapper(activity, R.style.Theme_OpenCuria_AppMenu))
    val view = LayoutInflater.from(dialog.context).inflate(R.layout.opencuria_app_menu, null)

    view.findViewById<TextView>(R.id.opencuria_app_menu_name).text = launch.name
    val icon = view.findViewById<ImageView>(R.id.opencuria_app_menu_icon)
    val bitmap = launch.iconPath?.let(BitmapFactory::decodeFile)
    if (bitmap != null) {
      icon.setImageBitmap(bitmap)
      icon.clipToOutline = true
      icon.outlineProvider = object : ViewOutlineProvider() {
        override fun getOutline(view: View, outline: Outline) {
          outline.setRoundRect(0, 0, view.width, view.height, view.width * ICON_CORNER)
        }
      }
    } else {
      icon.visibility = View.GONE
    }
    view.findViewById<View>(R.id.opencuria_app_menu_close).setOnClickListener {
      dialog.dismiss()
      onClose()
    }
    view.findViewById<View>(R.id.opencuria_app_menu_reload).setOnClickListener {
      dialog.dismiss()
      onReload()
    }

    val switches = mapOf(
      AppMenuTrigger.SHAKE to R.id.opencuria_app_menu_shake,
      AppMenuTrigger.THREE_FINGER_PRESS to R.id.opencuria_app_menu_three_fingers,
      AppMenuTrigger.BUTTON to R.id.opencuria_app_menu_button
    ).mapValues { (_, id) -> view.findViewById<MaterialSwitch>(id) }
    // The last way that is on stays on: its switch is disabled.
    fun update() {
      for ((trigger, switch) in switches) {
        switch.setOnCheckedChangeListener(null)
        switch.isChecked = triggers.isEnabled(trigger)
        switch.isEnabled = !triggers.isLast(trigger)
        switch.setOnCheckedChangeListener { _, on ->
          triggers.set(trigger, on)
          update()
          onTriggersChanged()
        }
      }
    }
    update()

    dialog.setContentView(view)
    dialog.behavior.skipCollapsed = true
    dialog.behavior.state = BottomSheetBehavior.STATE_EXPANDED
    dialog.show()
    return dialog
  }

  /** Corner radius of the app icon, as a share of its width (like the loading screen's). */
  private const val ICON_CORNER = 0.225f
}
