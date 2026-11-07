import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import * as Linking from "expo-linking";
import type { OpenCuriaClient } from "@opencuria/client";
import { parseAppLink, type AppLink } from "../apps/appLinks";
import { appScope } from "../apps/appPresentation";
import { closeOpenApp } from "../apps/appRuntime";
import { appInstaller, openApp } from "../apps/useApps";
import type { NativeAuthSession } from "../platform/storage";
import { useSession } from "../screens/SessionContext";
import { showToast } from "../ui/toast";
import { appsWidget } from "./appsWidget";
import { AppsWidgetSync } from "./appsWidgetSync";

interface AppsWidgetRequests {
  /** The widget asked for the Apps list (its More tile or empty state). */
  appsRequested: boolean;
  consumeAppsRequest: () => void;
}

const Context = createContext<AppsWidgetRequests>({
  appsRequested: false,
  consumeAppsRequest: () => undefined,
});

let sharedSync: AppsWidgetSync | null = null;
function widgetSync(): AppsWidgetSync {
  sharedSync ??= new AppsWidgetSync(appInstaller(), appsWidget);
  return sharedSync;
}

/**
 * The account whose apps the widget shows, as installs record it:
 * `undefined` while the saved session loads, `null` when signed out.
 */
function accountScope(
  booting: boolean,
  session: NativeAuthSession | null,
  client: OpenCuriaClient | null,
): string | null | undefined {
  if (!session) return booting ? undefined : null;
  if (!client || session.userId === undefined) return undefined;
  return appScope(client.api.baseUrl, session.organizationId, session.userId);
}

/**
 * Keeps the home screen widget current and handles what it opens: an app,
 * right away and offline too, or the Apps list, which MainScreen shows.
 */
export function AppsWidgetProvider({
  children,
}: React.PropsWithChildren): React.JSX.Element {
  const { booting, session, client, offline } = useSession();
  const scope = accountScope(booting, session, client);
  const [link, setLink] = useState<AppLink | null>(null);

  useEffect(() => widgetSync().start(), []);
  useEffect(() => widgetSync().setScope(scope), [scope]);

  useEffect(() => {
    const receive = (url: string | null) => {
      const parsed = url ? parseAppLink(url) : null;
      if (!parsed) return;
      // Handled once: OpenCuria mounting again later must not repeat it.
      Linking.clearInitialURL();
      setLink(parsed);
    };
    // The link of the activity that started now, as Expo Router reads it:
    // a recreated activity can still report its predecessor's intent to
    // React Native's `getInitialURL`.
    receive(Linking.getLinkingURL());
    const subscription = Linking.addEventListener("url", ({ url }) =>
      receive(url),
    );
    return () => subscription.remove();
  }, []);

  useEffect(() => {
    if (link?.kind !== "app" || scope === undefined) return;
    setLink(null);
    // Signed out, OpenCuria opens on the sign-in screen.
    if (scope === null) return;
    // Offline, the app opens at once instead of waiting for a route.
    void openApp(offline ? null : client, scope, link.appId)
      .then((found) => {
        if (!found)
          showToast({
            title: "App not installed",
            message: "Install it again from Apps.",
            tone: "error",
          });
      })
      .catch(() => undefined);
  }, [link, scope, client, offline]);

  useEffect(() => {
    // An open app would cover the Apps list.
    if (link?.kind === "apps") void closeOpenApp().catch(() => undefined);
  }, [link]);

  const consumeAppsRequest = useCallback(
    () => setLink((current) => (current?.kind === "apps" ? null : current)),
    [],
  );
  const value = useMemo(
    () => ({ appsRequested: link?.kind === "apps", consumeAppsRequest }),
    [link, consumeAppsRequest],
  );
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useAppsWidgetRequests(): AppsWidgetRequests {
  return useContext(Context);
}
