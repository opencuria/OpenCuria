package expo.modules.opencuriaappruntime

import android.app.Application
import android.content.Context
import expo.modules.core.interfaces.ApplicationLifecycleListener
import expo.modules.core.interfaces.Package

/**
 * Installs [AppGateway] when OpenCuria starts, before React Native or any
 * Expo module creates an OkHttp client, so every client routes app backends.
 */
class OpenCuriaAppRuntimePackage : Package {
  override fun createApplicationLifecycleListeners(
    context: Context
  ): List<ApplicationLifecycleListener> = listOf(
    object : ApplicationLifecycleListener {
      override fun onCreate(application: Application) {
        AppGateway.install()
      }
    }
  )
}
