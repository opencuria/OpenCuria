import React, { useState } from "react";
import type { OpenCuriaClient } from "@opencuria/client";
import type { NativeAuthSession } from "../platform/storage";
import { Button } from "../ui/primitives";
import { AppsSheet } from "./AppsSheet";
import { appScope } from "./appPresentation";
import { useApps } from "./useApps";

/**
 * Installed apps while the server cannot be reached: they run offline, so
 * the connection error must not lock them away. Shows nothing when this
 * account has no installed app.
 */
export function OfflineAppsButton({
  client,
  session,
}: {
  client: OpenCuriaClient | null;
  session: NativeAuthSession;
}): React.JSX.Element | null {
  const apps = useApps(
    client,
    appScope(client?.api.baseUrl ?? "", session.organizationId, session.userId),
  );
  const [visible, setVisible] = useState(false);
  if (!client || session.userId === undefined) return null;
  if (!apps.rows.some((row) => row.installed)) return null;
  return (
    <>
      <Button
        title="Installed apps"
        variant="outline"
        onPress={() => setVisible(true)}
        testID="session-apps"
      />
      <AppsSheet
        visible={visible}
        onClose={() => setVisible(false)}
        apps={apps}
      />
    </>
  );
}
