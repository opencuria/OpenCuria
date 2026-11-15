package expo.modules.opencuriaappruntime

import android.webkit.CookieManager
import com.facebook.react.modules.network.CustomClientBuilder
import com.facebook.react.modules.network.NetworkingModule
import com.facebook.react.modules.network.OkHttpClientFactory
import com.facebook.react.modules.network.OkHttpClientProvider
import com.facebook.react.modules.websocket.WebSocketModule
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.Interceptor
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Routes requests of the open app to processes of its workspace.
 *
 * Apps call `http://<process>.opencuria.local[:port]/<path>`. Every OkHttp
 * client React Native and Expo create (fetch, XMLHttpRequest, WebSocket,
 * expo/fetch, images) runs [interceptor], which rewrites only those hosts to
 * `<gateway>/<process>/<port>/<path>` on the OpenCuria server and adds the
 * app session token. Other hosts go out unchanged. The token stays in native
 * code; the app's JavaScript never sees it.
 */
object AppGateway {
  /** Where the backend requests of one open app go. */
  class Session(val url: HttpUrl, val token: String, val domain: String)

  private const val SESSION_HEADER = "X-OpenCuria-App-Session"
  private val LABEL = Regex("^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
  private val DOMAIN = Regex("^[a-z0-9.-]{1,253}$")

  @Volatile private var current: Session? = null
  private val installed = AtomicBoolean(false)

  /** The session of a launch request, or null when the app has none. */
  fun parse(json: JSONObject?): Session? {
    if (json == null) return null
    val url = json.optString("url").toHttpUrlOrNull() ?: return null
    val token = json.optString("token")
    val domain = json.optString("domain").lowercase()
    if (token.isBlank() || !DOMAIN.matches(domain)) return null
    return Session(url, token, domain)
  }

  /** Route backend requests to [session], the app that is open now. */
  fun open(session: Session?) {
    current = session
  }

  /** Stop routing for [session]; a session opened after it stays. */
  fun close(session: Session?) {
    if (session != null && current === session) current = null
  }

  /**
   * Hook into every OkHttp client the app's React instance uses. Runs when
   * the application starts, before React Native creates its first client.
   */
  fun install() {
    if (!installed.compareAndSet(false, true)) return
    // Provider clients carry no HTTP response cache: React Native only adds
    // one to its networking module, and gateway or API responses are not
    // cacheable anyway.
    OkHttpClientProvider.setOkHttpClientFactory(
      OkHttpClientFactory {
        OkHttpClientProvider.createClientBuilder().addInterceptor(interceptor).build()
      }
    )
    // Per request too, for clients created before this factory existed.
    val perRequest = CustomClientBuilder { builder -> builder.addInterceptor(interceptor) }
    NetworkingModule.setCustomClientBuilder(perRequest)
    WebSocketModule.setCustomClientBuilder(perRequest)
  }

  /** The process label of [host] when it is `<label>.<domain>`, else null. */
  internal fun label(host: String, domain: String): String? {
    val suffix = ".$domain"
    if (!host.endsWith(suffix)) return null
    return host.removeSuffix(suffix).takeIf(LABEL::matches)
  }

  /** The gateway URL of [url] (OkHttp has already mapped ws/wss to http/s). */
  internal fun route(url: HttpUrl, label: String, session: Session): HttpUrl =
    session.url.newBuilder()
      .addPathSegment(label)
      .addPathSegment(url.port.toString())
      .addEncodedPathSegments(url.encodedPath.removePrefix("/"))
      .encodedQuery(url.encodedQuery)
      .build()

  internal val interceptor = Interceptor { chain ->
    val request = chain.request()
    val session = current
    val domain = session?.domain ?: "opencuria.local"
    val label = label(request.url.host, domain) ?: return@Interceptor chain.proceed(request)
    if (session == null) {
      throw IOException(
        "${request.url.host} is only reachable from an app opened in OpenCuria while online."
      )
    }
    val routed = route(request.url, label, session)
    val builder = request.newBuilder().url(routed).header(SESSION_HEADER, session.token)
    // React Native's WebSocket client skips the cookie jar and only sends
    // cookies of the original URL; the backend's cookies belong to the gateway.
    if (request.header("Upgrade").equals("websocket", ignoreCase = true) &&
      request.header("Cookie") == null
    ) {
      gatewayCookies(routed)?.let { builder.header("Cookie", it) }
    }
    chain.proceed(builder.build())
  }

  private fun gatewayCookies(url: HttpUrl): String? = try {
    CookieManager.getInstance().getCookie(url.toString())?.takeIf(String::isNotBlank)
  } catch (_: Exception) {
    null // No WebView provider: no cookies, as for any other client.
  }
}
