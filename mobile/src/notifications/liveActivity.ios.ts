import { requireOptionalNativeModule } from "expo";
import type { AgentActivitySnapshot } from "@opencuria/client";
import type { LiveActivity } from "expo-widgets";
import type { LiveActivities, LiveActivityHandle } from "./liveActivity";
import { notificationLink } from "./target";

function wrap(
  instance: LiveActivity<AgentActivitySnapshot>,
): LiveActivityHandle {
  return {
    id: instance.getId(),
    update: (snapshot) =>
      instance.update(snapshot, new Date(Date.now() + 10 * 60_000)),
    end: (snapshot) => instance.end("immediate", snapshot, new Date()),
    token: () => instance.getPushToken(),
    onToken: (listener) => {
      const subscription = instance.addPushTokenListener(({ pushToken }) => {
        if (pushToken) listener(pushToken);
      });
      return () => subscription.remove();
    },
  };
}

export const liveActivities: LiveActivities = {
  available: requireOptionalNativeModule("ExpoWidgets") !== null,
  async instances() {
    const { AgentActivity } = await import("../widgets/AgentActivity");
    return AgentActivity.getInstances().map(wrap);
  },
  async start(snapshot) {
    const { AgentActivity } = await import("../widgets/AgentActivity");
    const row = snapshot.activities[0];
    const url = row
      ? notificationLink({
          ...row,
          organizationId: snapshot.organizationId,
          deviceId: snapshot.deviceId,
        })
      : undefined;
    return wrap(
      AgentActivity.start(snapshot, url, new Date(Date.now() + 10 * 60_000)),
    );
  },
};
