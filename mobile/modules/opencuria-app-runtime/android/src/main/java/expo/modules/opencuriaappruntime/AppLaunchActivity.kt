package expo.modules.opencuriaappruntime

import android.app.Activity
import android.content.Intent
import android.os.Bundle

/**
 * Opens what an Apps widget tile links to, without a window of its own.
 * An app that is already open shows as it is, as apps do when the launcher
 * opens them again: this activity joins OpenCuria's task, which brings the
 * app back, and ends. Everything else goes to OpenCuria as the link, whose
 * JavaScript opens the app. The widget cannot start OpenCuria directly:
 * its activity is `singleTask`, and starting it closes an open app.
 */
class AppLaunchActivity : Activity() {
  override fun onCreate(savedInstanceState: Bundle?) {
    super.onCreate(savedInstanceState)
    val link = intent?.data
    val appId = link?.getQueryParameter("app")
    if (link != null && (appId == null || !AppRuntimeActivity.isOpen(appId))) {
      packageManager.getLaunchIntentForPackage(packageName)?.component?.let { main ->
        startActivity(Intent(Intent.ACTION_VIEW, link).setComponent(main))
      }
    }
    finish()
  }
}
