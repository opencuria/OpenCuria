package expo.modules.opencuriaappruntime;

import android.net.Uri;

import com.facebook.react.bridge.ReadableArray;
import com.facebook.react.bridge.ReadableMap;
import com.facebook.react.bridge.ReadableType;
import com.facebook.react.bridge.WritableMap;
import com.facebook.react.modules.network.NetworkingModule;

import java.io.IOException;
import java.lang.reflect.Method;

import kotlin.Pair;
import okhttp3.RequestBody;

/**
 * Confines React Native's networking module of an app instance to the files
 * the app may read ({@link AppFileAccess}). It fails `fetch` and
 * XMLHttpRequest requests for other `file:` and `content:` URIs, and request
 * bodies that would upload such files, before React Native's own handlers
 * (`BlobModule`, `RequestBodyUtil`) open them.
 *
 * <p>Java, because Kotlin cannot implement React Native's internal handler
 * interfaces. Their registration methods are internal too, and Kotlin
 * mangles their names per build variant, so they are found by prefix. A
 * React Native without them fails the app's networking instead of leaving
 * it unconfined.
 */
final class NetworkingSandbox {
  private NetworkingSandbox() {}

  /** Call right after creating the module, before `BlobModule` adds its handlers. */
  static void install(NetworkingModule module, AppFileAccess files) {
    add(module, "addUriHandler$", NetworkingModule.UriHandler.class, new UriGate(files));
    add(module, "addRequestBodyHandler$", NetworkingModule.RequestBodyHandler.class, new BodyGate(files));
  }

  private static void add(NetworkingModule module, String prefix, Class<?> type, Object handler) {
    for (Method method : NetworkingModule.class.getMethods()) {
      Class<?>[] parameters = method.getParameterTypes();
      if (method.getName().startsWith(prefix) && parameters.length == 1 && parameters[0] == type) {
        try {
          method.invoke(module, handler);
          return;
        } catch (ReflectiveOperationException e) {
          throw new IllegalStateException("Could not confine React Native's networking.", e);
        }
      }
    }
    throw new IllegalStateException(
        "React Native's networking module has no " + prefix + "…; update NetworkingSandbox.");
  }

  private static String denied(String uri) {
    return "This app may not read " + uri + ".";
  }

  /** Claims the URIs the app may not read, so no later handler opens them. */
  private static final class UriGate implements NetworkingModule.UriHandler {
    private final AppFileAccess files;

    UriGate(AppFileAccess files) {
      this.files = files;
    }

    @Override
    public boolean supports(Uri uri, String responseType) {
      return !files.canRead(uri);
    }

    @Override
    public Pair<WritableMap, byte[]> fetch(Uri uri) throws IOException {
      throw new IOException(denied(uri.toString()));
    }
  }

  /** Claims request bodies (`{uri}`, form data parts) that would upload such URIs. */
  private static final class BodyGate implements NetworkingModule.RequestBodyHandler {
    private final AppFileAccess files;

    BodyGate(AppFileAccess files) {
      this.files = files;
    }

    @Override
    public boolean supports(ReadableMap body) {
      return firstDeniedUri(body) != null;
    }

    @Override
    public RequestBody toRequestBody(ReadableMap body, String contentType) {
      // NetworkingModule reports this as the request's error.
      throw new SecurityException(denied(firstDeniedUri(body)));
    }

    private String firstDeniedUri(ReadableMap body) {
      String uri = deniedUri(body);
      if (uri != null || !body.hasKey("formData") || body.getType("formData") != ReadableType.Array) {
        return uri;
      }
      ReadableArray parts = body.getArray("formData");
      for (int i = 0; parts != null && i < parts.size(); i++) {
        if (parts.getType(i) == ReadableType.Map && (uri = deniedUri(parts.getMap(i))) != null) {
          return uri;
        }
      }
      return null;
    }

    /** The body's `uri` when `RequestBodyUtil.getFileInputStream` would open it unconfined. */
    private String deniedUri(ReadableMap body) {
      if (body == null || !body.hasKey("uri") || body.getType("uri") != ReadableType.String) {
        return null;
      }
      String uri = body.getString("uri");
      if (uri == null || uri.startsWith("data:")) {
        return null;
      }
      Uri parsed = Uri.parse(uri);
      String scheme = parsed.getScheme();
      if (scheme != null && scheme.startsWith("http")) {
        return null;
      }
      return files.canRead(parsed) ? null : uri;
    }
  }
}
